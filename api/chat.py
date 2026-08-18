# -*- coding: utf-8 -*-
"""/v1/chat/completions —— 2API 的主路径。

一次请求依次经过：调用方鉴权与配额 -> 模型解析 -> 请求归一化 -> 账号池故障转移 ->
思考格式转换 -> 响应标准化 -> 用量记账。

流式的资源所有权：Upstream.open() 返回的 Lease 一旦交给生成器，就由生成器的 finally
负责 aclose()，中途 return 的每条路径都必须保证 Lease 已被关闭。
"""
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from api.deps import (APIError, client_auth, enforce_quota, get_ctx, now_ms,
                      read_json_body, upstream_error_response)
from core import reasoning as R
from core.clientkeys import ClientKey
from core.conversations import (chat_to_conversations,
                                conversations_response_to_chat,
                                conversations_stream_events, needs_conversations)
from core.openai_compat import (RequestError, clean_usage, error_envelope,
                                normalize_chat_request, normalize_chat_response,
                                normalize_stream_event)
from core.pool import est_tokens
from core.upstream import UpstreamFailure, UpstreamRejected

router = APIRouter(prefix="/v1", tags=["openai"])

ENDPOINT = "/v1/chat/completions"


def _resolve_mode(request: Request, ctx, meta: dict) -> str:
    """本次请求的思考输出格式：请求头 > reasoning.exclude > 全局配置。"""
    header = (request.headers.get("X-Reasoning-Format") or "").strip().lower()
    if header in R.MODES:
        return header
    if meta.get("exclude_reasoning"):
        return R.STRIP
    return ctx.settings.reasoning_format


@router.post("/chat/completions")
async def chat_completions(request: Request, key: ClientKey = Depends(client_auth)):
    ctx = get_ctx(request)
    t0 = time.time()
    payload = await read_json_body(request, ctx)

    requested_model = str(payload.get("model") or "")
    enforce_quota(ctx, key, requested_model)

    info = ctx.registry.resolve(requested_model)
    upstream_model = info.id if info else requested_model
    supports_reasoning = info.supports_reasoning if info else True

    # GLM-5.2 等 Z.ai 模型只能走 /v1/conversations，chat/completions 会 429。
    # 在这里把请求转成 conversations 格式，走单独的转发路径。
    if needs_conversations(upstream_model):
        return await _conversations_proxy(ctx, payload, upstream_model,
                                           requested_model, key, t0)

    try:
        body, meta = normalize_chat_request(payload, supports_reasoning,
                                            ctx.settings.reasoning_passback)
    except RequestError as e:
        raise APIError(e.status, e.message, e.type, e.param, e.code) from e
    body["model"] = upstream_model

    stream = meta["stream"]
    mode = _resolve_mode(request, ctx, meta)
    est = est_tokens(body.get("messages"))
    attempt_log: list[tuple] = []

    def on_failed(account, status, raw, error):
        attempt_log.append((account.email, status, error))

    try:
        lease = await ctx.upstream.open(
            "POST", "/chat/completions", json_body=body, est_tokens=est,
            stream=stream, on_attempt_failed=on_failed)
    except (UpstreamRejected, UpstreamFailure) as e:
        return upstream_error_response(ctx, e, upstream_model=upstream_model,
                                       endpoint=ENDPOINT, requested_model=requested_model,
                                       key=key, t0=t0, stream=stream)

    if not stream:
        try:
            raw = await lease.read()
        finally:
            await lease.aclose()
        return _finish_nonstream(ctx, raw, lease, mode, t0, requested_model,
                                 upstream_model, key)

    generator = _stream(ctx, lease, mode, t0, requested_model, upstream_model, key, est,
                        meta["include_usage"])
    return StreamingResponse(
        generator, media_type="text/event-stream",
        headers={"X-Pool-Account": lease.account_email, "Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no", "Connection": "keep-alive"})


async def _conversations_proxy(ctx, payload: dict, upstream_model: str,
                               requested_model: str, key: ClientKey, t0: float):
    """把 chat/completions 请求转成 conversations 格式发给上游，再把响应转回来。"""
    body, stream = chat_to_conversations(payload)
    body["model"] = upstream_model
    est = est_tokens(payload.get("messages"))

    try:
        lease = await ctx.upstream.open(
            "POST", "/conversations", json_body=body, est_tokens=est,
            stream=stream, skip_limits=True)
    except (UpstreamRejected, UpstreamFailure) as e:
        return upstream_error_response(ctx, e, upstream_model=upstream_model,
                                       endpoint=ENDPOINT, requested_model=requested_model,
                                       key=key, t0=t0, stream=stream)

    if not stream:
        try:
            raw = await lease.read()
        finally:
            await lease.aclose()
        return _finish_conv_nonstream(ctx, raw, lease, t0, requested_model,
                                      upstream_model, key)

    generator = _conv_stream(ctx, lease, t0, requested_model, upstream_model, key)
    return StreamingResponse(
        generator, media_type="text/event-stream",
        headers={"X-Pool-Account": lease.account_email, "Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _finish_conv_nonstream(ctx, raw: bytes, lease, t0: float, requested_model: str,
                           upstream_model: str, key: ClientKey) -> Response:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        ctx.store.record(lease.account_email, upstream_model, ENDPOINT, 502,
                         duration_ms=now_ms(t0), attempts=lease.attempts,
                         requested_model=requested_model, client_key=key.id,
                         error="upstream returned non-JSON body")
        return JSONResponse(error_envelope("Upstream returned a non-JSON body", "api_error"),
                            status_code=502)

    body = conversations_response_to_chat(parsed, requested_model)
    usage = body.get("usage") or {}
    total = usage.get("total_tokens", 0)
    ctx.keys.note_usage(key, total)
    ctx.store.record(lease.account_email, upstream_model, ENDPOINT, 200,
                     prompt_tokens=usage.get("prompt_tokens", 0),
                     completion_tokens=usage.get("completion_tokens", 0),
                     duration_ms=now_ms(t0), attempts=lease.attempts,
                     requested_model=requested_model, client_key=key.id)
    return Response(json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    status_code=200, media_type="application/json",
                    headers={"X-Pool-Account": lease.account_email,
                             "X-Pool-Attempts": str(lease.attempts)})


async def _conv_stream(ctx, lease, t0: float, requested_model: str,
                       upstream_model: str, key: ClientKey):
    """把 conversations SSE 流转成 chat.completion.chunk 流。"""
    status = 200
    error = ""
    usage = None
    done_marker = b'[DONE]'
    try:
        async for chunk in conversations_stream_events(lease, requested_model):
            yield chunk
            # 从收尾块里抠 usage 记账
            if isinstance(chunk, bytes):
                try:
                    data = chunk[6:].strip()  # skip "data: "
                    if data and data != done_marker:
                        ev = json.loads(data)
                        if ev.get("usage"):
                            usage = ev["usage"]
                except (json.JSONDecodeError, ValueError):
                    pass
    except Exception as e:
        status, error = 0, f"stream {type(e).__name__}: {e}"
    finally:
        # lease 已在 conversations_stream_events 的 finally 里 aclose
        u = usage or {}
        ctx.keys.note_usage(key, u.get("total_tokens", 0))
        ctx.store.record(lease.account_email, upstream_model, ENDPOINT, status,
                         prompt_tokens=u.get("prompt_tokens", 0),
                         completion_tokens=u.get("completion_tokens", 0),
                         duration_ms=now_ms(t0), stream=True, attempts=lease.attempts,
                         requested_model=requested_model, client_key=key.id,
                         error=error[:160])


def _finish_nonstream(ctx, raw: bytes, lease, mode: str, t0: float,
                      requested_model: str, upstream_model: str, key: ClientKey) -> Response:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        ctx.store.record(lease.account_email, upstream_model, ENDPOINT, 502,
                         duration_ms=now_ms(t0), attempts=lease.attempts,
                         requested_model=requested_model, client_key=key.id,
                         error="upstream returned non-JSON body")
        return JSONResponse(error_envelope("Upstream returned a non-JSON body", "api_error"),
                            status_code=502)

    think_chars = answer_chars = 0
    finish_reason = ""
    for choice in parsed.get("choices") or []:
        if isinstance(choice, dict):
            finish_reason = finish_reason or (choice.get("finish_reason") or "")
            t, a = R.apply_to_message(choice.get("message"), mode)
            think_chars += t
            answer_chars += a

    usage = parsed.get("usage") or {}
    completion_tokens = usage.get("completion_tokens", 0) or 0
    reasoning_tokens = R.reasoning_tokens_for(think_chars, answer_chars, completion_tokens)
    body = normalize_chat_response(parsed, reasoning_tokens)

    total = body["usage"].get("total_tokens", 0)
    ctx.keys.note_usage(key, total)
    ctx.store.record(lease.account_email, upstream_model, ENDPOINT, 200,
                     prompt_tokens=body["usage"].get("prompt_tokens", 0),
                     completion_tokens=completion_tokens, duration_ms=now_ms(t0),
                     stream=False, attempts=lease.attempts,
                     requested_model=requested_model, reasoning_tokens=reasoning_tokens,
                     cached_tokens=(usage.get("prompt_tokens_details") or {}).get(
                         "cached_tokens", 0),
                     finish_reason=finish_reason, client_key=key.id)
    return Response(json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    status_code=200, media_type="application/json",
                    headers={"X-Pool-Account": lease.account_email,
                             "X-Pool-Attempts": str(lease.attempts)})


async def _stream(ctx, lease, mode: str, t0: float, requested_model: str,
                  upstream_model: str, key: ClientKey, est: int,
                  include_usage: bool = False):
    """转发上游 SSE。

    usage 的位置有个取舍：上游总是把 usage 挂在带 finish_reason 的最后一个内容块上，
    而 OpenAI 只在客户端显式要了 stream_options.include_usage 时、用一个 choices 为空的
    独立末尾块来发。两边都照顾：
      - 客户端要了 include_usage -> 按 OpenAI 的样子发独立块，内容块上的 usage 摘掉
      - 客户端没要 -> 保持上游的位置就地补全 reasoning_tokens，不额外发块
        （凭空多发一个 choices 为空的块，会让直接取 chunk.choices[0] 的客户端崩掉）
    无论走哪条路，usage 都只出现一次，且一定带 reasoning_tokens。
    """
    converter = R.StreamConverter(mode=mode)
    usage = None
    finish_reason = ""
    ttft_ms = 0
    status = 200
    error = ""
    last_event = None

    try:
        async for line in lease.response.aiter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                continue  # SSE 注释/心跳行，不转发

            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue

            for choice in event.get("choices") or []:
                if isinstance(choice, dict) and choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

            converter.feed(event)
            normalize_stream_event(event)

            if event.get("usage"):
                usage = event.pop("usage")
                if not include_usage:
                    event["usage"] = clean_usage(
                        usage, converter.reasoning_tokens(
                            usage.get("completion_tokens") or 0))

            if not ttft_ms and _has_payload(event):
                ttft_ms = now_ms(t0)
            last_event = event
            yield _sse(event)

        tail = converter.finalize()
        if tail and last_event:
            closing = {**last_event, "choices": [{"index": 0, "delta": tail,
                                                  "finish_reason": None}]}
            closing.pop("usage", None)
            yield _sse(closing)

        if include_usage and usage is not None:
            completion_tokens = usage.get("completion_tokens") or int(
                (converter.think_chars + converter.answer_chars) / 3.5)
            yield _sse({"id": (last_event or {}).get("id", ""),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": (last_event or {}).get("model", upstream_model),
                        "choices": [],
                        "usage": clean_usage(
                            usage, converter.reasoning_tokens(completion_tokens))})
        yield b"data: [DONE]\n\n"
    except Exception as e:
        status, error = 0, f"stream {type(e).__name__}: {e}"
        yield _sse_error(error)
    finally:
        await lease.aclose()
        prompt_tokens = (usage or {}).get("prompt_tokens", est)
        completion_tokens = (usage or {}).get("completion_tokens") or int(
            (converter.think_chars + converter.answer_chars) / 3.5)
        reasoning_tokens = converter.reasoning_tokens(completion_tokens)
        ctx.keys.note_usage(key, prompt_tokens + completion_tokens)
        ctx.store.record(lease.account_email, upstream_model, ENDPOINT, status,
                         prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                         duration_ms=now_ms(t0), stream=True, attempts=lease.attempts,
                         requested_model=requested_model, reasoning_tokens=reasoning_tokens,
                         cached_tokens=((usage or {}).get("prompt_tokens_details") or {}).get(
                             "cached_tokens", 0),
                         ttft_ms=ttft_ms, finish_reason=finish_reason,
                         client_key=key.id, error=error[:160])


def _has_payload(event: dict) -> bool:
    for choice in event.get("choices") or []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if isinstance(delta, dict) and (delta.get("content") or delta.get("reasoning_content")
                                        or delta.get("tool_calls")):
            return True
    return False


def _sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _sse_error(message: str) -> bytes:
    return _sse(error_envelope(message, "api_error"))
