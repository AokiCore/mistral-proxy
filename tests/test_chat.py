# -*- coding: utf-8 -*-
"""/v1/chat/completions 端到端行为。上游用 MockTransport，不产生真实调用。"""
import json

import httpx

from tests.conftest import RATE_HEADERS, sse, thinking_chunk

CHAT = "/v1/chat/completions"
BODY = {"model": "mistral-small-latest", "messages": [{"role": "user", "content": "hi"}]}


def ok_json(content="hello", **extra):
    return httpx.Response(200, headers=RATE_HEADERS, json={
        "id": "x", "model": "mistral-small-latest",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content,
                                 "tool_calls": None}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                  "prompt_tokens_details": {"cached_tokens": 1}}, **extra})


def ok_sse(*deltas):
    events = deltas or ({"choices": [{"delta": {"content": "hi"}}]},)
    payload = [json.dumps(e) for e in events]
    payload.append(json.dumps({"choices": [{"delta": {"content": ""},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                                         "total_tokens": 5}}))
    return httpx.Response(200, headers={**RATE_HEADERS, "content-type": "text/event-stream"},
                          content=sse(*payload))


def sse_events(text):
    return [json.loads(line[5:]) for line in text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


# ---------- 故障转移（旧实现的头号缺陷） ----------

def test_stream_fails_over_to_next_account(make_client):
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        return ok_sse() if len(calls) > 1 else httpx.Response(429, json={})

    client = make_client(handler)
    with client:
        r = client.post(CHAT, json={**BODY, "stream": True})
        assert r.status_code == 200
        assert "hi" in r.text and "[DONE]" in r.text
    assert len(calls) == 2 and calls[0] != calls[1]


def test_nonstream_retries_on_429(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        return ok_json() if len(calls) > 1 else httpx.Response(429, json={})

    client = make_client(handler)
    with client:
        assert client.post(CHAT, json=BODY).status_code == 200
    assert len(calls) == 2


def test_client_error_is_not_retried(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"object": "error", "message": "Invalid model: x",
                                         "type": "invalid_model", "code": "1500"})

    client = make_client(handler)
    with client:
        r = client.post(CHAT, json=BODY)
        assert r.status_code == 400
        assert r.json()["error"]["message"] == "Invalid model: x"
    assert len(calls) == 1


def test_retries_are_capped(make_client):
    calls = []
    client = make_client(lambda rq: (calls.append(1), httpx.Response(429, json={}))[1],
                         emails=[f"a{i}@x.com" for i in range(10)],
                         **{"max_retry_accounts": 3})
    with client:
        assert client.post(CHAT, json=BODY).status_code == 429
    assert len(calls) == 3


def test_all_cooling_returns_retry_after(make_client):
    client = make_client(lambda rq: httpx.Response(429, json={}), emails=["a@x.com"])
    with client:
        assert client.post(CHAT, json=BODY).status_code == 429
        second = client.post(CHAT, json=BODY)
        assert second.status_code == 429
        assert int(second.headers["Retry-After"]) >= 1
        assert second.json()["error"]["type"] == "rate_limit_error"


def test_connection_error_is_retried(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return ok_json()

    client = make_client(handler)
    with client:
        assert client.post(CHAT, json=BODY).status_code == 200
    assert len(calls) == 2


# ---------- 资源归还 ----------

def test_stream_releases_semaphore_and_inflight(make_client):
    client = make_client(lambda rq: ok_sse(), **{"max_concurrency": 4})
    with client:
        assert client.post(CHAT, json={**BODY, "stream": True}).status_code == 200
        ctx = client.app.state.ctx
        assert ctx.sem._value == 4
        assert all(o.inflight == 0 for a in ctx.pool.accounts for o in a.orgs)


def test_failed_attempts_release_resources(make_client):
    client = make_client(lambda rq: httpx.Response(500, json={}), **{"max_concurrency": 4})
    with client:
        assert client.post(CHAT, json=BODY).status_code == 429
        ctx = client.app.state.ctx
        assert ctx.sem._value == 4
        assert all(o.inflight == 0 for a in ctx.pool.accounts for o in a.orgs)


def test_rejected_request_releases_resources(make_client):
    client = make_client(lambda rq: httpx.Response(400, json={}), **{"max_concurrency": 4})
    with client:
        client.post(CHAT, json=BODY)
        ctx = client.app.state.ctx
        assert ctx.sem._value == 4
        assert all(o.inflight == 0 for a in ctx.pool.accounts for o in a.orgs)


# ---------- 协议兼容 ----------

def test_openai_only_params_do_not_reach_upstream(make_client):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return ok_json()

    client = make_client(handler)
    with client:
        r = client.post(CHAT, json={**BODY, "seed": 1, "user": "u", "store": True,
                                    "logit_bias": {"1": 1}, "max_completion_tokens": 50})
        assert r.status_code == 200
    for banned in ("seed", "user", "store", "logit_bias", "max_completion_tokens"):
        assert banned not in seen
    assert seen["max_tokens"] == 50


def test_replayed_reasoning_content_stripped_before_upstream(make_client):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return ok_json()

    client = make_client(handler)
    with client:
        client.post(CHAT, json={**BODY, "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo", "reasoning_content": "思考"},
            {"role": "user", "content": "more"}]})
    assert "reasoning_content" not in seen["messages"][1]


def test_error_envelope_is_openai_shaped(make_client):
    client = make_client(lambda rq: httpx.Response(
        422, json={"detail": [{"type": "missing", "loc": ["body", "messages"],
                               "msg": "Field required"}]}))
    with client:
        body = client.post(CHAT, json=BODY).json()
    assert set(body) == {"error"}
    assert body["error"]["param"] == "messages"


def test_local_validation_errors_use_envelope(make_client):
    client = make_client(lambda rq: ok_json())
    with client:
        r = client.post(CHAT, json={"model": "m", "messages": []})
        assert r.status_code == 400
        assert r.json()["error"]["param"] == "messages"

        r = client.post(CHAT, content=b"{bad", headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"


def test_oversized_body_rejected(make_client):
    client = make_client(lambda rq: ok_json(), **{"max_body_bytes": 200})
    with client:
        r = client.post(CHAT, json={**BODY, "messages": [{"role": "user", "content": "x" * 500}]})
        assert r.status_code == 413


def test_response_is_cleaned(make_client):
    client = make_client(lambda rq: ok_json())
    with client:
        body = client.post(CHAT, json=BODY).json()
    assert body["object"] == "chat.completion"
    assert "tool_calls" not in body["choices"][0]["message"]
    assert body["usage"]["completion_tokens_details"]["reasoning_tokens"] == 0
    assert body["system_fingerprint"]


# ---------- 模型解析 ----------

def test_alias_resolves_to_upstream_model(make_client):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            seen.update(json.loads(request.content))
        return ok_json()

    client = make_client(handler)
    with client:
        client.get("/v1/models")  # 触发注册表同步
        client.app.state.ctx.registry.set_alias("gpt-4o", "mistral-large-latest")
        r = client.post(CHAT, json={**BODY, "model": "gpt-4o"})
        assert r.status_code == 200
    assert seen["model"] == "mistral-large-latest"


def test_effort_dropped_for_non_reasoning_model(make_client):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            seen.update(json.loads(request.content))
        return ok_json()

    client = make_client(handler)
    with client:
        client.get("/v1/models")
        client.post(CHAT, json={**BODY, "model": "mistral-large-latest",
                                "reasoning_effort": "high"})
    assert "reasoning_effort" not in seen


# ---------- 思考格式 ----------

THINK_CONTENT = [thinking_chunk("推理中"), {"type": "text", "text": "答案"}]


def test_nonstream_reasoning_content(make_client):
    client = make_client(lambda rq: ok_json(THINK_CONTENT))
    with client:
        body = client.post(CHAT, json=BODY).json()
    msg = body["choices"][0]["message"]
    assert msg["content"] == "答案"
    assert msg["reasoning_content"] == "推理中"
    assert msg["reasoning"] == "推理中"
    assert body["usage"]["completion_tokens_details"]["reasoning_tokens"] > 0


def test_nonstream_think_tags_mode(make_client):
    client = make_client(lambda rq: ok_json(THINK_CONTENT),
                         **{"reasoning_format": "think_tags"})
    with client:
        msg = client.post(CHAT, json=BODY).json()["choices"][0]["message"]
    assert msg["content"] == "<think>\n推理中\n</think>\n\n答案"


def test_per_request_format_header_overrides(make_client):
    client = make_client(lambda rq: ok_json(THINK_CONTENT))
    with client:
        msg = client.post(CHAT, json=BODY,
                          headers={"X-Reasoning-Format": "strip"}).json()["choices"][0]["message"]
    assert msg["content"] == "答案"
    assert "reasoning_content" not in msg


def test_reasoning_exclude_strips(make_client):
    client = make_client(lambda rq: ok_json(THINK_CONTENT))
    with client:
        msg = client.post(CHAT, json={**BODY, "reasoning": {"exclude": True}}) \
            .json()["choices"][0]["message"]
    assert "reasoning_content" not in msg


def test_stream_reasoning_split(make_client):
    client = make_client(lambda rq: ok_sse(
        {"choices": [{"delta": {"content": [thinking_chunk("想")]}}]},
        {"choices": [{"delta": {"content": "答"}}]}))
    with client:
        events = sse_events(client.post(CHAT, json={**BODY, "stream": True}).text)
    reasoning = "".join(e["choices"][0]["delta"].get("reasoning_content", "")
                        for e in events if e.get("choices"))
    content = "".join(e["choices"][0]["delta"].get("content", "")
                      for e in events if e.get("choices"))
    assert reasoning == "想" and content == "答"


def test_stream_think_tags_wraps(make_client):
    client = make_client(lambda rq: ok_sse(
        {"choices": [{"delta": {"content": [thinking_chunk("想")]}}]},
        {"choices": [{"delta": {"content": "答"}}]}),
        **{"reasoning_format": "think_tags"})
    with client:
        text = client.post(CHAT, json={**BODY, "stream": True}).text
    joined = "".join(e["choices"][0]["delta"].get("content", "")
                     for e in sse_events(text) if e.get("choices"))
    assert joined == "<think>\n想\n</think>\n\n答"


def test_stream_usage_appears_exactly_once(make_client):
    """上游把 usage 挂在最后的内容块上；不能既转发它又补发一个，否则客户端读到两份。"""
    client = make_client(lambda rq: ok_sse(
        {"choices": [{"delta": {"content": [thinking_chunk("想想想")]}}]},
        {"choices": [{"delta": {"content": "答"}}]}))
    with client:
        events = sse_events(client.post(CHAT, json={**BODY, "stream": True}).text)
    with_usage = [e for e in events if e.get("usage")]
    assert len(with_usage) == 1
    assert with_usage[0]["usage"]["completion_tokens_details"]["reasoning_tokens"] > 0
    assert with_usage[0]["choices"], "没要 include_usage 时不该出现 choices 为空的块"


def test_stream_include_usage_uses_dedicated_final_chunk(make_client):
    """客户端显式要了 include_usage 时，按 OpenAI 的样子用独立末尾块发。"""
    client = make_client(lambda rq: ok_sse(
        {"choices": [{"delta": {"content": [thinking_chunk("想想想")]}}]},
        {"choices": [{"delta": {"content": "答"}}]}))
    with client:
        events = sse_events(client.post(CHAT, json={
            **BODY, "stream": True, "stream_options": {"include_usage": True}}).text)
    with_usage = [e for e in events if e.get("usage")]
    assert len(with_usage) == 1
    assert with_usage[0]["choices"] == []
    assert with_usage[0] is events[-1]
    assert with_usage[0]["usage"]["completion_tokens_details"]["reasoning_tokens"] > 0


def test_stream_never_emits_empty_choices_without_include_usage(make_client):
    """直接取 chunk.choices[0] 的客户端很常见，不能凭空塞空 choices 的块。"""
    client = make_client(lambda rq: ok_sse({"choices": [{"delta": {"content": "x"}}]}))
    with client:
        events = sse_events(client.post(CHAT, json={**BODY, "stream": True}).text)
    assert all(e.get("choices") for e in events)


def test_stream_strips_upstream_p_field(make_client):
    client = make_client(lambda rq: ok_sse({"p": "abcdef",
                                            "choices": [{"delta": {"content": "x"}}]}))
    with client:
        text = client.post(CHAT, json={**BODY, "stream": True}).text
    assert '"p"' not in text


# ---------- 记账 ----------

def test_usage_recorded_with_ttft_and_attempts(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        return ok_sse() if len(calls) > 1 else httpx.Response(429, json={})

    client = make_client(handler)
    with client:
        client.post(CHAT, json={**BODY, "stream": True})
        store = client.app.state.ctx.store
        store.flush()
        rows = store.export_rows(1)
    success = [r for r in rows if r["status"] == 200][0]
    assert success["attempts"] == 2
    assert success["ttft_ms"] >= 0
    assert success["stream"] == 1
    assert success["finish_reason"] == "stop"


def test_cached_tokens_recorded(make_client):
    client = make_client(lambda rq: ok_json())
    with client:
        client.post(CHAT, json=BODY)
        store = client.app.state.ctx.store
        store.flush()
        assert store.export_rows(1)[0]["total_tokens"] == 5


def test_rate_limit_headers_update_pool(make_client):
    client = make_client(lambda rq: ok_json())
    with client:
        client.post(CHAT, json=BODY)
        org = next(o for a in client.app.state.ctx.pool.accounts for o in a.orgs
                   if o.last_status == "ok")
    assert org.limit_tokens == 50000 and org.remaining_req == 49
