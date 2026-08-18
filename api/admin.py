# -*- coding: utf-8 -*-
"""管理与统计接口。全部需要登录（会话 Cookie 或 X-Admin-Token 头）。

不支持 ?token= —— 查询串会进浏览器历史、Referer 和访问日志。

凭据可见性:
  - 上游账号 api_key 默认只返回预览, 完整值需登录 + 显式 ?reveal=1
  - 下游访问密钥只在签发那一刻返回明文, 库里只有 SHA-256, 任何接口都取不回
  - 不接受服务端文件路径导入(那会是个任意文件读取)
"""
import csv
import io
import time

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from api.deps import get_ctx, require_admin
from api.openai_api import sync_models
from core.billing import BillingError
from core.pool import parse_records

router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])

ACCOUNT_ACTIONS = ("enable", "disable", "remove", "restore")
KEY_ACTIONS = ("enable", "disable", "revoke")


def _key_label(names: dict, key_id: str) -> str:
    """令牌被吊销后名字就查不到了，回落成裸 id 对用户没有意义。"""
    if not key_id:
        return ""
    return names.get(key_id) or f"已吊销 {key_id[:6]}…"


def _id_list(payload: dict, plural: str, singular: str) -> list:
    """批量接口兼容两种传法：{"emails": [...]} 或单个 {"email": "..."}。"""
    return payload.get(plural) or ([payload[singular]] if payload.get(singular) else [])


# ---------- 统计与日志 ----------

@router.get("/stats")
async def stats(request: Request, hours: int = Query(default=24, ge=1, le=24 * 30)):
    ctx = get_ctx(request)
    data = await run_in_threadpool(ctx.store.stats, hours)
    data["pool"] = ctx.pool.summary()
    names = {k["id"]: k["name"] for k in ctx.keys.list()}
    for row in data.get("by_client") or []:
        row["name"] = _key_label(names, row["client_key"])
    return data


@router.get("/logs")
async def logs(request: Request,
               hours: int = Query(default=24, ge=1, le=24 * 90),
               page: int = Query(default=1, ge=1),
               limit: int = Query(default=50, ge=1, le=200),
               status: str = "", account: str = "", client_key: str = "",
               model: str = "", endpoint: str = "", stream: str = "", search: str = ""):
    ctx = get_ctx(request)
    result = await run_in_threadpool(
        ctx.store.query_logs, hours=hours, page=page, limit=limit, status=status,
        account=account, client_key=client_key, model=model, endpoint=endpoint,
        stream=stream, search=search)
    names = {k["id"]: k["name"] for k in ctx.keys.list()}
    for row in result["rows"]:
        row["client_name"] = _key_label(names, row["client_key"])
    return result


@router.get("/logs/filters")
async def log_filters(request: Request):
    ctx = get_ctx(request)
    return {
        "endpoints": await run_in_threadpool(ctx.store.distinct_values, "endpoint"),
        "models": await run_in_threadpool(ctx.store.distinct_values, "requested_model"),
        "keys": [{"id": k["id"], "name": k["name"]} for k in ctx.keys.list()],
    }


@router.get("/export")
async def export_csv(request: Request, hours: int = Query(default=24, ge=1, le=24 * 30),
                     model: str = Query(default="")):
    ctx = get_ctx(request)
    rows = await run_in_threadpool(ctx.store.export_rows, hours, model)
    buf = io.StringIO()
    writer = csv.writer(buf)
    columns = list(rows[0].keys()) if rows else ["ts", "account", "model", "status"]
    writer.writerow(["time"] + columns)
    for r in rows:
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))]
                        + [r[c] for c in columns])
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=mistral_usage_{hours}h.csv"})


@router.post("/cleanup")
async def cleanup(request: Request, payload: dict = Body(default={})):
    ctx = get_ctx(request)
    days = max(1, int(payload.get("days", 30)))
    return {"deleted": await run_in_threadpool(ctx.store.cleanup, days), "days": days}


# ---------- 上游账号 ----------

@router.get("/accounts")
async def list_accounts(request: Request, reveal: int = Query(default=0, ge=0, le=1)):
    ctx = get_ctx(request)
    return {"accounts": ctx.pool.get_accounts(reveal=bool(reveal)),
            "count": len(ctx.pool.accounts), "summary": ctx.pool.summary()}


@router.post("/accounts")
async def add_account(request: Request, payload: dict = Body(...)):
    ctx = get_ctx(request)
    email = (payload.get("email") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    if not email or not api_key:
        return JSONResponse({"error": "need 'email' and 'api_key'"}, status_code=400)
    ctx.store.undelete(email)
    added, updated = ctx.pool.import_records([{**payload, "email": email, "api_key": api_key}])
    ctx.pool.save_states()
    return {"added": added, "updated": updated, "total": len(ctx.pool.accounts)}


@router.post("/accounts/import")
async def import_accounts(request: Request, payload: dict = Body(...)):
    """导入账号。

    内容由浏览器读好再传上来（选文件或粘贴都走这一条），服务端不碰本地文件路径 ——
    接受调用方指定路径就等于开了个任意文件读取。
    """
    ctx = get_ctx(request)
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({"error": "内容为空"}, status_code=400)
    try:
        records = parse_records(content)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": f"解析失败：{e}"}, status_code=400)
    if not records:
        return JSONResponse(
            {"error": "没解析出任何账号。支持 mistral_keys.json 那样的 JSON 数组，"
                      "或带 email,api_key 表头的 CSV。"}, status_code=400)

    usable = [r for r in records if isinstance(r, dict) and r.get("api_key")]
    if not usable:
        return JSONResponse(
            {"error": f"解析出 {len(records)} 条记录，但都没有 api_key 字段"}, status_code=400)

    added, updated = ctx.pool.import_records(usable)
    ctx.pool.save_states()
    return {"added": added, "updated": updated, "skipped": len(records) - len(usable),
            "blocked": len(usable) - added - updated, "total": len(ctx.pool.accounts)}


@router.post("/accounts/action")
async def account_action(request: Request, payload: dict = Body(...)):
    ctx = get_ctx(request)
    action = payload.get("action", "")
    emails = _id_list(payload, "emails", "email")
    if action not in ACCOUNT_ACTIONS:
        return JSONResponse({"error": f"action 必须是 {ACCOUNT_ACTIONS} 之一"}, status_code=400)
    if not emails:
        return JSONResponse({"error": "need 'emails' or 'email'"}, status_code=400)

    ok = 0
    for email in emails:
        if action == "enable":
            ok += ctx.pool.set_enabled(email, True)
        elif action == "disable":
            ok += ctx.pool.set_enabled(email, False)
        elif action == "remove":
            ok += ctx.pool.remove_account(email)
        else:
            ctx.store.undelete(email)
            ok += 1
    ctx.pool.save_states()
    return {"ok": ok, "total": len(ctx.pool.accounts)}


@router.post("/accounts/budget")
async def refresh_budget(request: Request, payload: dict = Body(...)):
    """按需查一批账号的月度美元额度。

    有存过控制台会话的约 0.5 秒一个，没有的要先用密码登录（约 3 秒），
    所以一次最多 20 个，避免请求超时。
    """
    ctx = get_ctx(request)
    emails = _id_list(payload, "emails", "email")
    if not emails:
        return JSONResponse({"error": "need 'emails' or 'email'"}, status_code=400)
    if len(emails) > 20:
        return JSONResponse({"error": "一次最多查 20 个，慢慢来"}, status_code=400)

    done, failed = [], []
    for email in emails:
        acc = next((a for a in ctx.pool.accounts if a.email == email), None)
        if acc is None:
            failed.append({"email": email, "error": "账号不存在"})
            continue
        if not acc.console_session and not acc.mistral_password:
            failed.append({"email": email, "error": "既没有控制台会话也没有密码，查不了"})
            continue
        try:
            budget, session = await ctx.budgets.fetch(
                acc.email, acc.mistral_password, acc.console_session)
        except BillingError as e:
            failed.append({"email": email, "error": str(e)})
            continue
        ctx.pool.set_console_session(acc, session)
        # 更新所有 Org 的额度
        for org in acc.orgs:
            ctx.pool.update_budget(org, budget)
        done.append({"email": email, **budget.to_dict()})
    ctx.pool.save_states()
    return {"checked": done, "failed": failed}


# ---------- 下游访问密钥 ----------

@router.get("/keys")
async def list_keys(request: Request):
    ctx = get_ctx(request)
    return {"keys": ctx.keys.list(), "auth_required": ctx.keys.auth_required,
            "static_key_configured": bool(ctx.settings.client_api_key)}


@router.post("/keys")
async def create_key(request: Request, payload: dict = Body(default={})):
    ctx = get_ctx(request)
    try:
        key, raw = ctx.keys.create(
            name=str(payload.get("name") or "").strip(),
            rpm_limit=int(payload.get("rpm_limit") or 0),
            daily_token_limit=int(payload.get("daily_token_limit") or 0),
            allowed_models=payload.get("allowed_models") or [],
            ttl_days=int(payload.get("ttl_days") or 0))
    except (TypeError, ValueError) as e:
        return JSONResponse({"error": f"参数不合法: {e}"}, status_code=400)
    return {"key": raw, "info": key.to_dict(),
            "warning": "这是唯一一次显示完整密钥，请立刻保存"}


@router.post("/keys/action")
async def key_action(request: Request, payload: dict = Body(...)):
    ctx = get_ctx(request)
    action = payload.get("action", "")
    ids = _id_list(payload, "ids", "id")
    if action not in KEY_ACTIONS:
        return JSONResponse({"error": f"action 必须是 {KEY_ACTIONS} 之一"}, status_code=400)
    if not ids:
        return JSONResponse({"error": "need 'ids' or 'id'"}, status_code=400)
    ok = 0
    for key_id in ids:
        if action == "revoke":
            ok += ctx.keys.revoke(key_id)
        else:
            ok += ctx.keys.update(key_id, enabled=(action == "enable"))
    return {"ok": ok, "total": len(ctx.keys.keys)}


@router.post("/keys/update")
async def update_key(request: Request, payload: dict = Body(...)):
    ctx = get_ctx(request)
    key_id = payload.get("id")
    if not key_id:
        return JSONResponse({"error": "need 'id'"}, status_code=400)
    fields = {k: payload[k] for k in
              ("name", "rpm_limit", "daily_token_limit", "allowed_models") if k in payload}
    if "ttl_days" in payload:
        days = int(payload["ttl_days"] or 0)
        fields["expires_at"] = time.time() + days * 86400 if days else 0.0
    if not ctx.keys.update(key_id, **fields):
        return JSONResponse({"error": "密钥不存在"}, status_code=404)
    return {"ok": True}


# ---------- 模型注册表 ----------

@router.get("/models")
async def admin_models(request: Request):
    ctx = get_ctx(request)
    return {
        "synced_at": ctx.registry.synced_at,
        "count": len(ctx.registry.models),
        "aliases": ctx.registry.custom_aliases,
        "models": [
            {"id": m.id, "name": m.name, "context": m.max_context_length,
             "capabilities": m.capabilities, "aliases": m.aliases,
             "deprecation": m.deprecation, "description": m.description}
            for m in sorted(ctx.registry.models.values(), key=lambda x: x.id)],
    }


@router.post("/models/sync")
async def models_sync(request: Request):
    ctx = get_ctx(request)
    return {"synced": await sync_models(ctx), "synced_at": ctx.registry.synced_at}


@router.post("/models/alias")
async def set_alias(request: Request, payload: dict = Body(...)):
    ctx = get_ctx(request)
    alias = (payload.get("alias") or "").strip()
    if payload.get("action") == "remove":
        return {"ok": ctx.registry.remove_alias(alias)}
    try:
        ctx.registry.set_alias(alias, payload.get("target") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "aliases": ctx.registry.custom_aliases}


# ---------- 设置 ----------

@router.get("/config")
async def config(request: Request):
    ctx = get_ctx(request)
    s = ctx.settings
    return {"reasoning_format": s.reasoning_format, "max_concurrency": s.max_concurrency,
            "max_retry_accounts": s.max_retry_accounts, "host": s.host, "port": s.port,
            "auth_enabled": ctx.auth.enabled, "password_source": ctx.auth.password_source,
            "password_source_label": ctx.auth.password_source_label,
            "default_password": ctx.auth.using_generated_password,
            "config_file": s.config_file,
            "client_auth": ctx.keys.auth_required,
            "static_key_configured": bool(s.client_api_key),
            "db_path": s.db_path, "keys_file": s.keys_file,
            "read_timeout": s.read_timeout, "model_count": len(ctx.registry.models),
            "uptime_s": int(time.time() - ctx.started_at),
            "dropped_usage_rows": ctx.store.dropped}


@router.post("/config")
async def update_config(request: Request, payload: dict = Body(...)):
    """只允许改可以热更新的项。"""
    from core.reasoning import MODES
    ctx = get_ctx(request)
    changed = {}
    fmt = payload.get("reasoning_format")
    if fmt is not None:
        if fmt not in MODES:
            return JSONResponse({"error": f"reasoning_format 必须是 {MODES} 之一"},
                                status_code=400)
        ctx.settings.reasoning_format = fmt
        changed["reasoning_format"] = fmt
    retries = payload.get("max_retry_accounts")
    if retries is not None:
        ctx.settings.max_retry_accounts = max(1, min(20, int(retries)))
        changed["max_retry_accounts"] = ctx.settings.max_retry_accounts
    return {"ok": True, "changed": changed}


@router.post("/password")
async def change_password(request: Request, payload: dict = Body(...)):
    ctx = get_ctx(request)
    if ctx.auth.password_source != "database":
        return JSONResponse(
            {"error": "当前密码来自 --admin-token 启动参数，改密码请改启动参数"},
            status_code=400)
    if not ctx.auth.verify_password(str(payload.get("current") or "")):
        return JSONResponse({"error": "当前密码不正确"}, status_code=401)
    try:
        ctx.auth.set_password(str(payload.get("new") or ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "message": "密码已更新，所有登录会话已失效，请重新登录"}


@router.post("/sessions/revoke")
async def revoke_sessions(request: Request):
    get_ctx(request).auth.rotate_sessions()
    return {"ok": True, "message": "所有设备的登录会话已失效"}
