# -*- coding: utf-8 -*-
"""其余 OpenAI 兼容端点：models / embeddings / moderations。

models 走本地注册表而不是每次回源，注册表由后台任务定期从上游同步。
embeddings 的 encoding_format=base64 与 dimensions 上游不支持（前者被忽略、后者 422），
在本层补齐语义。
"""
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from api.deps import (APIError, client_auth, enforce_quota, get_ctx, now_ms,
                      read_json_body, upstream_error_response)
from core.clientkeys import ClientKey
from core.openai_compat import (RequestError, error_envelope,
                                normalize_embeddings_request,
                                normalize_embeddings_response,
                                normalize_moderations_response)
from core.upstream import UpstreamFailure, UpstreamRejected

router = APIRouter(prefix="/v1", tags=["openai"])


async def sync_models(ctx) -> int:
    """从上游拉取模型清单刷新注册表。失败时保留旧数据。"""
    try:
        lease = await ctx.upstream.open("GET", "/models")
    except (UpstreamFailure, UpstreamRejected):
        return 0
    try:
        raw = await lease.read()
    finally:
        await lease.aclose()
    try:
        data = json.loads(raw).get("data") or []
    except (json.JSONDecodeError, AttributeError):
        return 0
    return ctx.registry.update(data)


@router.get("/models")
async def list_models(request: Request, _key: ClientKey = Depends(client_auth)):
    ctx = get_ctx(request)
    if not ctx.registry.models:
        await sync_models(ctx)
    return {"object": "list", "data": ctx.registry.list_openai()}


@router.get("/models/{model_id:path}")
async def retrieve_model(model_id: str, request: Request,
                         _key: ClientKey = Depends(client_auth)):
    ctx = get_ctx(request)
    if not ctx.registry.models:
        await sync_models(ctx)
    info = ctx.registry.resolve(model_id)
    if not info:
        raise APIError(404, f"The model '{model_id}' does not exist", "not_found_error",
                       param="model", code="model_not_found")
    payload = info.to_openai()
    if info.id != model_id:
        payload.update({"id": model_id, "root": info.id, "alias_of": info.id})
    return payload


@router.post("/embeddings")
async def embeddings(request: Request, key: ClientKey = Depends(client_auth)):
    ctx = get_ctx(request)
    t0 = time.time()
    payload = await read_json_body(request, ctx)
    requested_model = str(payload.get("model") or "")
    enforce_quota(ctx, key, requested_model)

    try:
        body, meta = normalize_embeddings_request(payload)
    except RequestError as e:
        raise APIError(e.status, e.message, e.type, e.param, e.code) from e
    body["model"] = ctx.registry.resolve_id(body["model"])

    return await _forward(
        ctx, "/embeddings", body, key, t0, requested_model, "/v1/embeddings",
        lambda parsed: normalize_embeddings_response(
            parsed, meta["encoding_format"], meta["dimensions"]))


@router.post("/moderations")
async def moderations(request: Request, key: ClientKey = Depends(client_auth)):
    ctx = get_ctx(request)
    t0 = time.time()
    payload = await read_json_body(request, ctx)
    requested_model = str(payload.get("model") or "mistral-moderation-latest")
    enforce_quota(ctx, key, requested_model)

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        raw_input = [raw_input]
    if not isinstance(raw_input, list) or not raw_input:
        raise APIError(400, "'input' is required", "invalid_request_error", param="input")

    body = {"model": ctx.registry.resolve_id(requested_model),
            "input": [x if isinstance(x, str) else json.dumps(x) for x in raw_input]}
    return await _forward(ctx, "/moderations", body, key, t0, requested_model,
                          "/v1/moderations", normalize_moderations_response)


# ---------- 内部工具 ----------

async def _forward(ctx, path: str, body: dict, key: ClientKey, t0: float,
                   requested_model: str, endpoint: str, transform) -> Response:
    try:
        lease = await ctx.upstream.open("POST", path, json_body=body)
    except (UpstreamRejected, UpstreamFailure) as e:
        return upstream_error_response(ctx, e, upstream_model=body["model"],
                                       endpoint=endpoint, requested_model=requested_model,
                                       key=key, t0=t0)

    try:
        raw = await lease.read()
    finally:
        await lease.aclose()

    try:
        parsed = transform(json.loads(raw))
    except json.JSONDecodeError:
        return JSONResponse(error_envelope("Upstream returned a non-JSON body", "api_error"),
                            status_code=502)

    usage = parsed.get("usage") or {}
    total = usage.get("total_tokens", 0)
    ctx.keys.note_usage(key, total)
    ctx.store.record(lease.account_email, body["model"], endpoint, 200,
                     prompt_tokens=usage.get("prompt_tokens", 0),
                     completion_tokens=0, duration_ms=now_ms(t0),
                     attempts=lease.attempts, requested_model=requested_model,
                     client_key=key.id)
    return Response(json.dumps(parsed, ensure_ascii=False).encode("utf-8"),
                    status_code=200, media_type="application/json",
                    headers={"X-Pool-Account": lease.account_email})
