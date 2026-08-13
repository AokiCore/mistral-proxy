# -*- coding: utf-8 -*-
"""共享上下文、鉴权依赖与 /v1 端点的公共请求处理。

两套互不相干的凭据：
  - 管理端：登录密码 + 会话 Cookie（也接受 X-Admin-Token 头，方便脚本调用）
  - 调用方：签发给下游的 sk-pool-… 密钥，只作用于 /v1/*
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from core.auth import AuthManager, LoginThrottle, SESSION_COOKIE
from core.billing import BudgetClient
from core.clientkeys import ClientKey, ClientKeyStore, QuotaError
from core.config import Settings
from core.models import ModelRegistry
from core.openai_compat import error_envelope, normalize_error
from core.pool import AccountPool
from core.store import UsageStore
from core.upstream import Upstream, UpstreamFailure, UpstreamRejected

OPEN_ACCESS = ClientKey(id="", name="anonymous", enabled=True)


class APIError(HTTPException):
    """按 OpenAI 的 {"error": {...}} 包络返回的错误。"""

    def __init__(self, status: int, message: str, err_type: str = "api_error",
                 param: str | None = None, code: str | None = None,
                 headers: dict | None = None):
        super().__init__(status_code=status,
                         detail=error_envelope(message, err_type, param, code),
                         headers=headers)


@dataclass
class AppContext:
    settings: Settings
    store: UsageStore
    pool: AccountPool
    registry: ModelRegistry
    keys: ClientKeyStore
    auth: AuthManager
    upstream: Upstream
    client: httpx.AsyncClient
    sem: asyncio.Semaphore
    started_at: float = 0.0
    budgets: BudgetClient = field(default_factory=BudgetClient)
    login_throttle: LoginThrottle = field(default_factory=LoginThrottle)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


# ---------- 管理端鉴权 ----------

def admin_allowed(request: Request) -> bool:
    """会话 Cookie 或 X-Admin-Token 头任一通过即可。

    刻意不支持 ?token= —— 查询串会进浏览器历史、Referer 和服务端访问日志。
    """
    auth = get_ctx(request).auth
    if not auth.enabled:
        return True
    header = request.headers.get("X-Admin-Token", "")
    if header and auth.verify_password(header):
        return True
    return auth.verify_session(request.cookies.get(SESSION_COOKIE, ""))


def require_admin(request: Request) -> None:
    if not admin_allowed(request):
        raise APIError(401, "未登录或会话已过期", "authentication_error",
                       code="not_authenticated")


def redirect_to_login(request: Request) -> RedirectResponse | None:
    """页面路由用：未登录就跳登录页，登录后回到原地址。"""
    if admin_allowed(request):
        return None
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


# ---------- 调用方鉴权 ----------

def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def client_auth(request: Request) -> ClientKey:
    """校验下游密钥。一把都没签发时放行，返回匿名身份。"""
    ctx = get_ctx(request)
    if not ctx.keys.auth_required:
        return OPEN_ACCESS
    supplied = _bearer(request) or request.headers.get("X-Api-Key", "")
    if not supplied:
        raise APIError(401, "Missing API key. Pass it as 'Authorization: Bearer <key>'.",
                       "authentication_error")
    key = ctx.keys.verify(supplied)
    if key is None:
        raise APIError(401, "Incorrect API key provided.", "authentication_error",
                       code="invalid_api_key")
    return key


def enforce_quota(ctx: AppContext, key: ClientKey, model: str) -> None:
    if not key.id:
        return
    try:
        ctx.keys.check(key, model)
    except QuotaError as e:
        raise APIError(e.status, e.message, e.type) from e


def now_ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


# ---------- /v1 端点的公共请求处理 ----------

async def read_json_body(request: Request, ctx: AppContext) -> dict:
    """读取并校验 JSON 请求体（带大小上限），chat / embeddings / moderations 共用。"""
    raw = await request.body()
    if len(raw) > ctx.settings.max_body_bytes:
        raise APIError(413, f"Request body exceeds {ctx.settings.max_body_bytes} bytes",
                       "invalid_request_error")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise APIError(400, f"Invalid JSON body: {e}", "invalid_request_error") from e
    if not isinstance(payload, dict):
        raise APIError(400, "Request body must be a JSON object", "invalid_request_error")
    return payload


def upstream_error_response(ctx: AppContext, exc: UpstreamRejected | UpstreamFailure, *,
                            upstream_model: str, endpoint: str, requested_model: str,
                            key: ClientKey, t0: float, stream: bool = False) -> JSONResponse:
    """故障转移失败的统一善后：记账 + 转成 OpenAI 错误包络。

    UpstreamRejected 是上游明确拒绝（换账号也没用），原样转发上游的错误体；
    UpstreamFailure 是所有账号都试过仍失败，按限流报错并带 Retry-After。
    """
    if isinstance(exc, UpstreamRejected):
        ctx.store.record(exc.account_email, upstream_model, endpoint, exc.status,
                         duration_ms=now_ms(t0), stream=stream, attempts=exc.attempts,
                         requested_model=requested_model, client_key=key.id,
                         error=exc.body[:160].decode("utf-8", "replace"))
        return JSONResponse(normalize_error(exc.status, exc.body), status_code=exc.status)

    ctx.store.record("(pool)", upstream_model, endpoint, exc.status,
                     duration_ms=now_ms(t0), stream=stream, attempts=exc.attempts,
                     requested_model=requested_model, client_key=key.id,
                     error=exc.message[:160])
    headers = {"Retry-After": str(max(1, int(exc.retry_after)))} if exc.retry_after else None
    return JSONResponse(error_envelope(exc.message, "rate_limit_error"),
                        status_code=exc.status, headers=headers)
