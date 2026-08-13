# -*- coding: utf-8 -*-
"""管理台页面与健康检查。

页面只渲染骨架，数据全部由前端 fetch 拉取。未登录的页面请求会 303 到 /login。
"""
import os
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api.deps import admin_allowed, get_ctx, redirect_to_login

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

templates = Jinja2Templates(directory=TEMPLATE_DIR)
router = APIRouter(tags=["pages"])

PAGES = {
    "dashboard": ("dashboard.html", "仪表盘"),
    "channels": ("channels.html", "上游渠道"),
    "tokens": ("tokens.html", "访问令牌"),
    "models": ("models.html", "模型"),
    "logs": ("logs.html", "调用日志"),
    "settings": ("settings.html", "设置"),
}


RANGES = ((1, "1 小时"), (24, "24 小时"), (168, "7 天"), (720, "30 天"))


def _render(request: Request, key: str, **extra):
    guard = redirect_to_login(request)
    if guard:
        return guard
    template, title = PAGES[key]
    return templates.TemplateResponse(
        request, template, {"active": key, "title": title, **extra})


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, hours: int = Query(default=24, ge=1, le=24 * 30)):
    return _render(request, "dashboard", hours=hours, ranges=RANGES)


@router.get("/channels", response_class=HTMLResponse)
async def channels(request: Request):
    return _render(request, "channels")


@router.get("/tokens", response_class=HTMLResponse)
async def tokens(request: Request):
    return _render(request, "tokens")


@router.get("/models", response_class=HTMLResponse)
async def models(request: Request):
    return _render(request, "models")


@router.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    return _render(request, "logs")


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return _render(request, "settings")


# 旧路径保留跳转，避免书签失效
@router.get("/admin")
async def legacy_admin():
    return RedirectResponse("/channels", status_code=301)


@router.get("/keys")
async def legacy_keys():
    return RedirectResponse("/tokens", status_code=301)


@router.get("/health")
async def health(request: Request):
    """未登录时只返回存活状态，不泄露账号池规模。"""
    ctx = get_ctx(request)
    if not admin_allowed(request):
        return {"status": "ok"}
    s = ctx.pool.summary()
    return {
        "status": "ok",
        "uptime_s": int(time.time() - ctx.started_at),
        "accounts": s["total"], "enabled": s["enabled"], "cooling": s["cooling"],
        "drained": s["drained"], "exhausted": s["exhausted"], "inflight": s["inflight"],
        "requests_left": s["requests_left"], "tokens_left": s["tokens_left"],
        "budget_checked": s["budget_checked"], "budget_left": s["budget_left"],
        "models": len(ctx.registry.models),
        "client_auth": ctx.keys.auth_required,
        "default_password": ctx.auth.using_generated_password,
    }
