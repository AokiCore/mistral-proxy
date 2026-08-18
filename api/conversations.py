# -*- coding: utf-8 -*-
"""/v1/conversations —— Mistral Conversations API 透传端点。

GLM-5.2 等 Z.ai 模型只能通过这个端点调用（走 /chat/completions 会 429）。
本端点原样转发请求和响应，支持流式与非流式，复用账号池的故障转移能力。

下游客户端直接用 conversations 格式调用：
  POST /v1/conversations
  {model, inputs, tools, completion_args, instructions, stream}
"""
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from api.deps import (APIError, client_auth, enforce_quota, get_ctx, now_ms,
                      read_json_body, upstream_error_response)
from core.clientkeys import ClientKey
from core.openai_compat import error_envelope
from core.upstream import UpstreamFailure, UpstreamRejected

router = APIRouter(prefix="/v1", tags=["openai"])

ENDPOINT = "/v1/conversations"


@router.post("/conversations")
async def conversations(request: Request, key: ClientKey = Depends(client_auth)):
    ctx = get_ctx(request)
    t0 = time.time()
    payload = await read_json_body(request, ctx)

    requested_model = str(payload.get("model") or "")
    enforce_quota(ctx, key, requested_model)

    upstream_model = ctx.registry.resolve_id(requested_model)
    stream = bool(payload.get("stream"))

    body = dict(payload)
    body["model"] = upstream_model

    try:
        lease = await ctx.upstream.open(
            "POST", "/conversations", json_body=body, stream=stream,
            skip_limits=True)
    except (UpstreamRejected, UpstreamFailure) as e:
        return upstream_error_response(ctx, e, upstream_model=upstream_model,
                                       endpoint=ENDPOINT, requested_model=requested_model,
                                       key=key, t0=t0, stream=stream)

    if not stream:
        try:
            raw = await lease.read()
        finally:
            await lease.aclose()
        return _finish_nonstream(ctx, raw, lease, t0, requested_model,
                                 upstream_model, key)

    generator = _stream(ctx, lease, t0, requested_model, upstream_model, key)
    return StreamingResponse(
        generator, media_type="text/event-stream",
        headers={"X-Pool-Account": lease.account_email, "Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _finish_nonstream(ctx, raw: bytes, lease, t0: float, requested_model: str,
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

    usage = parsed.get("usage") or {}
    total = usage.get("total_tokens", 0)
    ctx.keys.note_usage(key, total)
    ctx.store.record(lease.account_email, upstream_model, ENDPOINT, 200,
                     prompt_tokens=usage.get("prompt_tokens", 0),
                     completion_tokens=usage.get("completion_tokens", 0),
                     duration_ms=now_ms(t0), attempts=lease.attempts,
                     requested_model=requested_model, client_key=key.id)
    return Response(json.dumps(parsed, ensure_ascii=False).encode("utf-8"),
                    status_code=200, media_type="application/json",
                    headers={"X-Pool-Account": lease.account_email,
                             "X-Pool-Attempts": str(lease.attempts)})


async def _stream(ctx, lease, t0: float, requested_model: str,
                  upstream_model: str, key: ClientKey):
    """原样转发上游 SSE，只记账。

    conversations 流式事件（ConversationEvents）：
      conversation.response.started/done/error
      message.output.delta
      tool.execution.started/delta/done
      agent.handoff.started/done
      function.call.delta
    透传端点只关心 done（拿 usage 记账）和 error（记日志），其余原样转发。
    """
    status = 200
    error = ""
    usage = None
    try:
        async for line in lease.response.aiter_lines():
            if not line:
                # 空行 = SSE 事件分隔，原样转发
                yield b"\n"
                continue
            yield (line + "\n").encode("utf-8")
            if line.startswith("data:"):
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                    etype = event.get("type")
                    if etype == "conversation.response.done":
                        usage = event.get("usage")
                    elif etype == "conversation.response.error":
                        error = event.get("message") or "upstream conversation error"
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        status, error = 0, f"stream {type(e).__name__}: {e}"
    finally:
        await lease.aclose()
        u = usage or {}
        ctx.keys.note_usage(key, u.get("total_tokens", 0))
        ctx.store.record(lease.account_email, upstream_model, ENDPOINT, status,
                         prompt_tokens=u.get("prompt_tokens", 0),
                         completion_tokens=u.get("completion_tokens", 0),
                         duration_ms=now_ms(t0), stream=True, attempts=lease.attempts,
                         requested_model=requested_model, client_key=key.id,
                         error=error[:160])
