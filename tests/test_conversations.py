# -*- coding: utf-8 -*-
"""chat/completions <-> conversations 双向转换。

GLM 系模型只能走 /v1/conversations，这里的转换错了就是整条 GLM 链路挂掉。
重点覆盖：请求方向的消息拆分与 entry 判别字段、工具调用的双向映射、
流式事件到 chat.completion.chunk 的转换（尤其 tool_calls 的 index 语义）。
"""
import json

from core.conversations import (chat_to_conversations, conversations_response_to_chat,
                                conversations_stream_events, needs_conversations)


def sse_lines(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


# ---------- 模型判定 ----------

def test_needs_conversations_matches_glm_prefixes():
    assert needs_conversations("glm-5-2")
    assert needs_conversations("zai-glm-5-2")
    assert needs_conversations("GLM-5-2")
    assert not needs_conversations("mistral-small-latest")
    assert not needs_conversations("")
    assert not needs_conversations("magistral-medium-latest")


# ---------- 请求方向 ----------

def test_chat_to_conversations_basic_split():
    body = {"model": "glm-5-2",
            "messages": [{"role": "system", "content": "be nice"},
                         {"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "yo"},
                         {"role": "user", "content": "more"}],
            "temperature": 0.3, "max_tokens": 100}
    out, stream = chat_to_conversations(body)
    assert stream is False
    assert out["instructions"] == "be nice"
    # 每条 entry 都带判别字段，缺了上游按联合类型校验会炸
    assert all(e["object"] == "entry" for e in out["inputs"])
    types = [e["type"] for e in out["inputs"]]
    assert types == ["message.input", "message.input", "message.input"]
    assert out["inputs"][0] == {"object": "entry", "type": "message.input",
                                "role": "user", "content": "hi"}
    assert out["completion_args"]["temperature"] == 0.3
    assert out["completion_args"]["max_tokens"] == 100


def test_assistant_tool_calls_become_function_call_entries():
    body = {"model": "glm-5-2", "messages": [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\":\"Paris\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "22C"},
    ]}
    out, _ = chat_to_conversations(body)
    call_entry, result_entry = out["inputs"][1], out["inputs"][2]
    assert call_entry == {"object": "entry", "type": "function.call",
                          "tool_call_id": "c1", "name": "get_weather",
                          "arguments": '{"city":"Paris"}'}
    assert result_entry == {"object": "entry", "type": "function.result",
                            "tool_call_id": "c1", "result": "22C"}


def test_tools_and_reasoning_effort_passthrough():
    tools = [{"type": "function",
              "function": {"name": "f", "description": "d", "parameters": {}}}]
    body = {"model": "glm-5-2", "messages": [{"role": "user", "content": "x"}],
            "tools": tools, "reasoning_effort": "high", "stream": True}
    out, stream = chat_to_conversations(body)
    assert stream is True
    assert out["tools"] == tools
    assert out["completion_args"]["reasoning_effort"] == "high"
    assert out["stream"] is True


# ---------- 非流式响应 ----------

def test_conversations_response_text_only():
    parsed = {"conversation_id": "conv1",
              "outputs": [{"type": "message.output", "role": "assistant", "id": "m1",
                           "content": "hello"}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    body = conversations_response_to_chat(parsed, "glm-5-2")
    choice = body["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "hello"}
    assert choice["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 3


def test_conversations_response_with_function_call():
    parsed = {"conversation_id": "conv1",
              "outputs": [{"type": "function.call", "name": "get_weather",
                           "arguments": "{\"city\": \"Paris\"}",
                           "tool_call_id": "tc9"}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    body = conversations_response_to_chat(parsed, "glm-5-2")
    msg = body["choices"][0]["message"]
    assert msg["tool_calls"] == [{"id": "tc9", "type": "function",
                                  "function": {"name": "get_weather",
                                               "arguments": '{"city": "Paris"}'}}]
    assert body["choices"][0]["finish_reason"] == "tool_calls"


def test_conversations_response_thinking_extracted():
    parsed = {"conversation_id": "c",
              "outputs": [{"type": "message.output", "role": "assistant", "id": "m",
                           "content": [
                               {"type": "thinking",
                                "thinking": [{"type": "text", "text": "想一下"}],
                                "closed": True},
                               {"type": "text", "text": "答案"}]}],
              "usage": {}}
    body = conversations_response_to_chat(parsed, "glm-5-2")
    msg = body["choices"][0]["message"]
    assert msg["content"] == "答案"
    assert msg["reasoning_content"] == "想一下"


# ---------- 流式事件转换 ----------

class _FakeLease:
    def __init__(self, events):
        self._events = events
        self.closed = False
        import asyncio

        async def aiter_lines():
            for line in sse_lines(self._events).splitlines():
                yield line

        self.response = type("R", (), {"aiter_lines": staticmethod(aiter_lines)})()

    async def aclose(self):
        self.closed = True


async def _collect(events):
    lease = _FakeLease(events)
    chunks = []
    async for c in conversations_stream_events(lease, "glm-5-2"):
        if c != b"data: [DONE]\n\n":
            chunks.append(json.loads(c[6:].strip()))
    return chunks, lease


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_stream_text_deltas_and_usage():
    events = [
        {"type": "conversation.response.started", "conversation_id": "cv"},
        {"type": "message.output.delta", "id": "m1", "content": "你"},
        {"type": "message.output.delta", "id": "m1", "content": "好"},
        {"type": "conversation.response.done",
         "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}},
    ]
    chunks, lease = run(_collect(events))
    deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert [d.get("content") for d in deltas[1:-1]] == ["你", "好"]
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["total_tokens"] == 6
    assert lease.closed, "生成器结束必须归还 Lease"


def test_stream_tool_call_fragments_share_one_index():
    """同一次调用的多个参数分片必须共用同一个 index，客户端才能拼回完整参数。"""
    events = [
        {"type": "function.call.delta", "id": "fc1", "name": "get_weather",
         "tool_call_id": "tc1", "output_index": 0,
         "arguments": ""},
        {"type": "function.call.delta", "id": "fc1", "name": "",
         "tool_call_id": "tc1", "output_index": 0,
         "arguments": "{\"city\":"},
        {"type": "function.call.delta", "id": "fc1", "name": "",
         "tool_call_id": "tc1", "output_index": 0,
         "arguments": "\"Paris\"}"},
        {"type": "conversation.response.done", "usage": {}},
    ]
    chunks, _ = run(_collect(events))
    frags = [c["choices"][0]["delta"]["tool_calls"][0]
             for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")]
    assert [f["index"] for f in frags] == [0, 0, 0], "分片 index 必须稳定"
    # id/name 只在首片出现，后续分片只带增量参数
    assert frags[0]["id"] == "tc1" and frags[0]["function"]["name"] == "get_weather"
    joined = "".join(f["function"].get("arguments", "") for f in frags)
    assert joined == '{"city":"Paris"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_parallel_tool_calls_get_distinct_indexes():
    events = [
        {"type": "function.call.delta", "id": "a", "name": "f1",
         "tool_call_id": "t1", "output_index": 0, "arguments": "{}"},
        {"type": "function.call.delta", "id": "b", "name": "f2",
         "tool_call_id": "t2", "output_index": 1, "arguments": "{}"},
        {"type": "conversation.response.done", "usage": {}},
    ]
    chunks, _ = run(_collect(events))
    frags = [c["choices"][0]["delta"]["tool_calls"][0]
             for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")]
    assert [f["index"] for f in frags] == [0, 1]
    assert [f["function"]["name"] for f in frags] == ["f1", "f2"]


def test_stream_thinking_delta_maps_to_reasoning_content():
    events = [
        {"type": "message.output.delta", "id": "m", "content":
            {"type": "thinking", "thinking": [{"type": "text", "text": "推理中"}],
             "closed": True}},
        {"type": "message.output.delta", "id": "m", "content": "答"},
        {"type": "conversation.response.done", "usage": {}},
    ]
    chunks, _ = run(_collect(events))
    deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
    assert deltas[0]["reasoning_content"] == "推理中"
    assert deltas[1]["content"] == "答"


def test_stream_error_event_does_not_crash():
    events = [
        {"type": "conversation.response.error", "code": "x", "message": "boom"},
        {"type": "conversation.response.done", "usage": {}},
    ]
    chunks, _ = run(_collect(events))
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
