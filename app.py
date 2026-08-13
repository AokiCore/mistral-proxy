# -*- coding: utf-8 -*-
"""Mistral 账号池 2API 网关 —— 应用装配与启动入口。

    python app.py --admin-token 你的密码

端点:
  POST /v1/chat/completions   OpenAI 兼容对话代理(流式/非流式, 429/5xx 自动换账号)
  POST /v1/embeddings         向量化
  POST /v1/moderations        内容审核
  GET  /v1/models             模型清单  GET /v1/models/{id} 单个模型
  GET  /  /admin  /keys  /models   统计 / 账号 / 密钥 / 模型 管理页
  GET  /health                健康检查
  /admin/*                    管理 API, 详见 api/admin.py
"""
import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import admin, auth_routes, chat, openai_api, pages
from api.deps import AppContext
from core.auth import AuthManager
from core.billing import BillingError, BudgetClient
from core.clientkeys import ClientKeyStore
from core.config import (DEFAULT_CONFIG_FILE, ConfigError, Settings, build_settings,
                         load_config_file, resolve_keys_file, write_config_template)
from core.models import ModelRegistry
from core.openai_compat import error_envelope, type_for_status
from core.pool import AccountPool, read_records_file
from core.reasoning import MODES as REASONING_MODES
from core.reasoning import REASONING_CONTENT
from core.store import UsageStore
from core.upstream import Upstream

log = logging.getLogger("app")


async def _state_saver(ctx: AppContext) -> None:
    while True:
        await asyncio.sleep(ctx.settings.state_save_interval)
        try:
            await asyncio.to_thread(ctx.pool.save_states)
        except Exception:
            log.exception("save_states failed")


async def _model_syncer(ctx: AppContext) -> None:
    """启动后先同步一次模型清单，之后按间隔刷新。"""
    await asyncio.sleep(1.0)
    while True:
        try:
            if ctx.registry.stale():
                count = await openai_api.sync_models(ctx)
                if count:
                    log.info("synced %d models from upstream", count)
        except Exception as e:
            log.warning("model sync failed: %s", e)
        await asyncio.sleep(60.0)


def _pick_for_budget_check(pool, stale_seconds: float, now: float):
    """挑一个最该查额度的账号。

    查一次要登控制台、约 2.5 秒 370KB，所以只查真花过钱的账号：
    用过但从没查过的排最前，其次是查过之后又用过的，最后是单纯过期的。
    闲置账号永远轮不到，池子空转时这个任务几乎不干活。
    """
    best, best_rank = None, ()
    for a in pool.accounts:
        if not a.enabled or not a.last_used:
            continue
        if not a.console_session and not a.mistral_password:
            continue          # 既没会话也没密码，查不了
        if a.exhausted_until > now:
            continue          # 已知花光了，等下月重置，没必要反复查
        if a.budget_checked_at <= 0:
            rank = (0, -a.last_used)
        elif a.last_used > a.budget_checked_at:
            rank = (1, a.budget_checked_at)
        elif now - a.budget_checked_at > stale_seconds:
            rank = (2, a.budget_checked_at)
        else:
            continue
        if best is None or rank < best_rank:
            best, best_rank = a, rank
    return best


async def _budget_checker(ctx: AppContext) -> None:
    """慢速轮询账号的月度美元额度，把花光的号从调度里摘掉。"""
    settings = ctx.settings
    stale = max(600.0, settings.budget_stale_hours * 3600.0)
    await asyncio.sleep(15.0)
    while True:
        try:
            acc = await asyncio.to_thread(
                _pick_for_budget_check, ctx.pool, stale, time.time())
            if acc is not None:
                budget, session = await ctx.budgets.fetch(
                    acc.email, acc.mistral_password, acc.console_session)
                ctx.pool.set_console_session(acc, session)
                ctx.pool.update_budget(acc, budget)
                if budget.exhausted:
                    log.info("[budget] %s 额度已用尽，尝试删组织重建", acc.email)
                    result = await ctx.rebuilder.rebuild(acc)
                    if ctx.pool.apply_rebuild(acc, result):
                        log.info("[budget] %s 重建成功，新 key %s…，已放回池子",
                                 acc.email, acc.api_key[:8])
                    else:
                        log.warning("[budget] %s 重建失败：%s",
                                    acc.email, result.error)
        except BillingError as e:
            log.warning("[budget] 查询失败: %s", e)
        except Exception:
            log.exception("[budget] 异常")
        await asyncio.sleep(max(5.0, settings.budget_check_interval))


def _default_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=settings.connect_timeout,
                              read=settings.read_timeout, write=60.0, pool=60.0),
        limits=httpx.Limits(max_connections=settings.max_concurrency * 2,
                            max_keepalive_connections=settings.max_concurrency),
        follow_redirects=False)


def create_app(settings: Settings, client_factory=None) -> FastAPI:
    """client_factory 只为测试注入 httpx.MockTransport 而存在。"""
    client_factory = client_factory or _default_client
    app = FastAPI(title="Mistral Pool 2API", version="3.0.0",
                  docs_url=None, redoc_url=None)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        store = UsageStore(settings.db_path)
        pool = AccountPool(store)
        pool.load_from_store()

        if settings.keys_file:
            try:
                added, updated = pool.import_records(read_records_file(settings.keys_file))
                log.info("[pool] %s: +%d 新增 / %d 更新", settings.keys_file, added, updated)
            except (OSError, ValueError) as e:
                log.warning("[pool] 导入 %s 失败: %s", settings.keys_file, e)
        pool.restore_states()
        pool.save_states()

        registry = ModelRegistry(store)
        registry.load()

        keys = ClientKeyStore(store)
        keys.load()
        keys.set_static_key(settings.client_api_key)

        auth = AuthManager(store, settings.admin_token, settings.auth_enabled)
        if settings.config_file and settings.admin_token:
            auth.fixed_source = f"配置文件 {os.path.basename(settings.config_file)}"
        auth.load()

        client = client_factory(settings)
        sem = asyncio.Semaphore(settings.max_concurrency)
        ctx = AppContext(settings=settings, store=store, pool=pool, registry=registry,
                         keys=keys, auth=auth, upstream=Upstream(pool, client, sem, settings),
                         client=client, sem=sem, started_at=time.time(),
                         budgets=BudgetClient())
        _app.state.ctx = ctx

        summary = pool.summary()
        log.info("[pool] %d 个账号 (%d 启用)", summary["total"], summary["enabled"])
        log.info("[keys] %d 个访问令牌, 调用方鉴权%s",
                 len(keys.keys), "开" if keys.auth_required else "关")
        if auth.generated_password:
            print("\n" + "=" * 60)
            print("  首次启动, 已随机生成管理台登录密码:")
            print(f"      {auth.generated_password}")
            print("  只显示这一次, 库里只存散列。想自己指定密码, 三选一:")
            print("    1. 配置文件 config.toml 里写 admin_password = \"你的密码\"")
            print("       （没有的话先跑 python app.py --init-config 生成模板）")
            print("    2. 登录后在管理台「设置」页改")
            print("    3. python app.py --set-password")
            print("=" * 60 + "\n")
        settings.warn_if_open_proxy(keys.auth_required)
        for warning in settings.warnings:
            log.warning(warning)

        tasks = [asyncio.create_task(_state_saver(ctx)),
                 asyncio.create_task(_model_syncer(ctx))]
        if settings.budget_check:
            tasks.append(asyncio.create_task(_budget_checker(ctx)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.to_thread(pool.save_states)
            await client.aclose()
            await asyncio.to_thread(store.close)

    app.router.lifespan_context = lifespan

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        body = detail if isinstance(detail, dict) and "error" in detail \
            else error_envelope(str(detail), type_for_status(exc.status_code))
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        first = (exc.errors() or [{}])[0]
        # loc 前缀是 body/query/path 之类的来源标记，param 只该给字段名
        loc = ".".join(str(x) for x in first.get("loc", [])
                       if x not in ("body", "query", "path", "header"))
        return JSONResponse(
            error_envelope(f"{loc}: {first.get('msg', 'invalid')}" if loc
                           else first.get("msg", "invalid request"),
                           "invalid_request_error", loc or None), status_code=400)

    app.mount("/static", StaticFiles(directory=pages.STATIC_DIR), name="static")
    app.include_router(chat.router)
    app.include_router(openai_api.router)
    app.include_router(admin.router)
    app.include_router(auth_routes.router)
    app.include_router(pages.router)
    return app


def build_parser() -> argparse.ArgumentParser:
    """所有默认值都是 None，好区分"用户显式给了"和"没给"，让配置文件能起作用。"""
    p = argparse.ArgumentParser(
        description="Mistral 账号池 2API 网关",
        epilog="不带参数直接运行即可；持久化配置写在 config.toml，用 --init-config 生成模板。")
    p.add_argument("--config", default=None,
                   help=f"配置文件路径（默认自动读取 {os.path.basename(DEFAULT_CONFIG_FILE)}）")
    p.add_argument("--init-config", action="store_true",
                   help="生成带注释的 config.toml 模板后退出")
    p.add_argument("--no-config", action="store_true", help="忽略配置文件")

    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--keys", default=None, help="启动时导入的上游账号文件 (json/csv)")
    p.add_argument("--no-keys", action="store_true", default=None,
                   help="只用数据库里的账号，不读文件")
    p.add_argument("--db", default=None, help="SQLite 路径")

    p.add_argument("--admin-token", "--admin-password", dest="admin_token", default=None,
                   help="管理台登录密码；不给则读配置文件，都没有就首次启动随机生成并存库")
    p.add_argument("--no-auth", action="store_true", default=None,
                   help="完全关闭管理台登录（仅限本机调试）")
    p.add_argument("--set-password", action="store_true",
                   help="交互式设置管理密码后退出（不启动服务）")
    p.add_argument("--api-key", default=None, help="固定的调用方密钥")

    p.add_argument("--max-concurrency", type=int, default=None, help="并发转发上限")
    p.add_argument("--max-retry-accounts", type=int, default=None,
                   help="429/5xx 时最多换几个账号")
    p.add_argument("--reasoning-format", default=None, choices=list(REASONING_MODES),
                   help="思考内容输出格式：reasoning_content(默认, 兼容最广) / "
                        "think_tags(内联 <think>) / passthrough(原样) / strip(丢弃)")
    p.add_argument("--thinking", default=None, help=argparse.SUPPRESS)  # 旧参数名
    p.add_argument("--allow-insecure", action="store_true", default=None,
                   help="允许绑定非回环地址的同时关闭登录（危险）")
    return p


def parse_args(argv=None) -> Settings:
    args = build_parser().parse_args(argv)

    if args.init_config:
        path = args.config or DEFAULT_CONFIG_FILE
        write_config_template(path)
        raise SystemExit(f"已生成配置模板: {path}\n改完里面的 admin_password 再启动即可。")

    file_values: dict = {}
    config_path = ""
    if not args.no_config:
        candidate = args.config or DEFAULT_CONFIG_FILE
        if args.config or os.path.exists(candidate):
            file_values = load_config_file(candidate)
            config_path = candidate

    cli: dict = {
        "host": args.host, "port": args.port, "db_path": args.db,
        "admin_token": args.admin_token, "client_api_key": args.api_key,
        "max_concurrency": args.max_concurrency,
        "max_retry_accounts": args.max_retry_accounts,
        "reasoning_format": args.reasoning_format,
        "allow_insecure": args.allow_insecure,
        "auth_enabled": False if args.no_auth else None,
        "keys_file": "" if args.no_keys else args.keys,
    }
    settings = build_settings(file_values, cli)
    settings.config_file = config_path
    settings.set_password_mode = args.set_password

    if args.thinking:
        legacy = {"split": REASONING_CONTENT, "passthrough": "passthrough", "strip": "strip"}
        settings.reasoning_format = legacy.get(args.thinking, settings.reasoning_format)
        settings.warnings.append("--thinking 已更名为 --reasoning-format，本次按旧值映射。")

    resolve_keys_file(settings)
    settings.validate()
    return settings


def set_password_cli(settings: Settings) -> int:
    """--set-password：交互式改库里的管理密码，改完退出。"""
    import getpass

    if settings.admin_token:
        source = f"配置文件 {settings.config_file}" if settings.config_file else "--admin-token"
        print(f"[config] 当前密码由 {source} 指定，改那里即可，无需 --set-password。",
              file=sys.stderr)
        return 2

    store = UsageStore(settings.db_path, start_writer=False)
    try:
        auth = AuthManager(store, "", True)
        auth.load()
        try:
            first = getpass.getpass("新的管理密码（至少 6 位）: ")
            again = getpass.getpass("再输一次: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消", file=sys.stderr)
            return 1
        if first != again:
            print("两次输入不一致", file=sys.stderr)
            return 1
        try:
            auth.set_password(first)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print("密码已更新，所有登录会话已失效。")
        return 0
    finally:
        store.close()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%m-%d %H:%M:%S")

    try:
        settings = parse_args(argv)
    except ConfigError as e:
        print(f"[config] {e}", file=sys.stderr)
        return 2

    if settings.set_password_mode:
        return set_password_cli(settings)

    if settings.config_file:
        log.info("[config] 已加载 %s", settings.config_file)
    app = create_app(settings)
    log.info("[server] 管理台 http://%s:%d/   API http://%s:%d/v1",
             settings.host, settings.port, settings.host, settings.port)
    log.info("[server] reasoning=%s 并发=%d 重试=%d 登录=%s",
             settings.reasoning_format, settings.max_concurrency,
             settings.max_retry_accounts, "开" if settings.auth_enabled else "关")
    # 放在反代后面时要认 X-Forwarded-Proto，否则 request.url.scheme 恒为 http，
    # 会话 Cookie 就不会带 Secure 标记。只信任本机来的转发头。
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
