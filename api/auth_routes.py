# -*- coding: utf-8 -*-
"""登录页与会话端点。

暴力破解节流（同一来源 IP 15 分钟内最多 10 次失败）由 ctx.login_throttle 承担，
状态跟着应用上下文走，不挂模块级全局。
"""
from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from api.deps import admin_allowed, get_ctx
from core.auth import SESSION_COOKIE

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _safe_next(raw: str) -> str:
    """只允许站内相对路径，挡掉 //evil.com 这类开放重定向。"""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    from api.pages import templates
    ctx = get_ctx(request)
    if not ctx.auth.enabled or admin_allowed(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next)})


@router.post("/auth/login")
async def login(request: Request, payload: dict = Body(default={})):
    ctx = get_ctx(request)
    throttle = ctx.login_throttle
    ip = _client_ip(request)

    locked = throttle.locked_for(ip)
    if locked:
        return JSONResponse(
            {"error": f"失败次数过多，请 {locked // 60 + 1} 分钟后再试"}, status_code=429)

    if not ctx.auth.verify_password(str(payload.get("password") or "")):
        throttle.note_failure(ip)
        remaining = throttle.limit - throttle.failure_count(ip)
        return JSONResponse(
            {"error": "密码错误" + (f"，还可尝试 {remaining} 次" if remaining <= 3 else "")},
            status_code=401)

    throttle.reset(ip)
    cookie, max_age = ctx.auth.issue_session(
        ttl_days=30 if payload.get("remember") else 1)
    response = JSONResponse({"ok": True, "next": _safe_next(str(payload.get("next") or "/"))})
    response.set_cookie(SESSION_COOKIE, cookie, max_age=max_age, httponly=True,
                        samesite="lax", path="/",
                        secure=request.url.scheme == "https")
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/auth/status")
async def status(request: Request):
    ctx = get_ctx(request)
    return {"auth_enabled": ctx.auth.enabled,
            "logged_in": admin_allowed(request),
            "password_source": ctx.auth.password_source}
