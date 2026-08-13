# -*- coding: utf-8 -*-
"""组织重建（删组织→建组织→发新 key）的单元测试。

用 MockTransport 假装控制台，验证 OrgRebuilder 按正确顺序调用端点、
从响应里抠出新的 org_id / workspace_id / api_key / key_id，
以及旧 key 会被新 key 覆盖。
"""
import json

import httpx
import pytest

from core.billing import BillingError
from core.org_rebuild import OrgRebuilder, RebuildResult
from core.pool import Account, AccountPool

CONSOLE = "https://admin.mistral.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/37.36")

OLD_ORG = "fc72a34a-d5c1-4e22-a527-d85723b2469f"
NEW_ORG = "9b4a9d51-4c46-4a17-8e32-5f2018781bde"
NEW_WS = "e05691da-e26a-4407-bfaf-63f7acdf4022"
NEW_KEY = "sk-newkey-1234567890abcdef"
NEW_KEY_ID = "kid-new"


def _fake_console(routes):
    """把 (method, path-prefix) -> (status, json) 的映射做成 MockTransport。

    会话有效（带 ory_session cookie）即放行；GET /organization 会补一个
    csrftoken set-cookie，模拟控制台首次访问时后端签发 csrf。
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({
            "method": request.method,
            "path": request.url.path,
            "x_csrftoken": request.headers.get("x-csrftoken"),
            "body": request.content.decode("utf-8", "ignore") if request.content else "",
        })
        # 控制台首页：补 csrftoken cookie（_ensure_csrf 在缺 csrf 时访问）
        if request.method == "GET" and request.url.path == "/organization":
            return httpx.Response(200, headers={
                "set-cookie": "csrftoken=csrf-from-console; Path=/"})
        for (method, prefix, checker), (status, payload) in routes.items():
            if request.method != method:
                continue
            if not request.url.path.startswith(prefix):
                continue
            if checker is not None and not checker(request):
                continue
            if callable(payload):
                return httpx.Response(status, **payload(request))
            return httpx.Response(status, **payload)
        return httpx.Response(404, text=f"no route for {request.method} {request.url.path}")

    return httpx.MockTransport(handler), calls


def _acc(**kw):
    base = dict(
        email="mist747289@moonstarsun.shop",
        api_key="sk-oldkey-deadbeef",
        mistral_password="Pw9060409!xAa1!",
        org_id=OLD_ORG,
        workspace_id="ws-old-uuid",
        console_session='{"ory_session_x": "sess-token"}',
    )
    base.update(kw)
    return Account(**base)


def test_rebuild_happy_path():
    """删旧组织→建新组织→列新 ws→发新 key，顺序与字段都对。"""
    routes = {
        ("DELETE", "/api/users/organization/", None):
            (200, {"json": {"details": "Organization successfully deleted"}}),
        ("POST", "/api/users/organizations", None):
            (200, {"json": {"uuid": NEW_ORG, "name": "1", "org_tier": "B"}}),
        ("GET", "/api/workspaces", None):
            (200, {"json": {"total": 1, "items": [
                {"uuid": NEW_WS, "name": "Default Workspace", "is_default": True}]}}),
        ("POST", "/api/billing/api-keys", None):
            (200, {"json": {"key": NEW_KEY, "key_id": NEW_KEY_ID}}),
    }
    transport, calls = _fake_console(routes)
    rb = OrgRebuilder(transport=transport)

    result = httpx.__dict__ and None  # placeholder, real call below
    import asyncio
    result = asyncio.run(rb.rebuild(_acc()))

    assert isinstance(result, RebuildResult)
    assert result.ok is True
    assert result.org_id == NEW_ORG
    assert result.workspace_id == NEW_WS
    assert result.api_key == NEW_KEY
    assert result.key_id == NEW_KEY_ID

    # 顺序: DELETE -> POST organizations -> GET workspaces -> POST api-keys
    methods = [(c["method"], c["path"]) for c in calls if c["x_csrftoken"]]
    assert methods[0] == ("DELETE", f"/api/users/organization/{OLD_ORG}")
    assert methods[1] == ("POST", "/api/users/organizations")
    assert methods[2][0] == "GET" and methods[2][1].startswith("/api/workspaces")
    assert methods[3] == ("POST", "/api/billing/api-keys")

    # 发 key 的 body 指向新 workspace
    key_call = calls[-1]
    body = json.loads(key_call["body"])
    assert body["workspace_uuid"] == NEW_WS


def test_rebuild_falls_back_to_password_login(tmp_path, monkeypatch):
    """会话失效时用密码登一次，再继续重建。"""
    login_calls = []

    async def fake_login(self, email, password):
        login_calls.append((email, password))
        jar = httpx.Cookies()
        jar.set("ory_session_x", "fresh-session", domain="admin.mistral.ai")
        jar.set("csrftoken", "csrf-fresh", domain="admin.mistral.ai")
        return jar

    monkeypatch.setattr(OrgRebuilder, "_login", fake_login)

    routes = {
        ("DELETE", "/api/users/organization/", None):
            (200, {"json": {"details": "deleted"}}),
        ("POST", "/api/users/organizations", None):
            (200, {"json": {"uuid": NEW_ORG, "org_tier": "B"}}),
        ("GET", "/api/workspaces", None):
            (200, {"json": {"items": [
                {"uuid": NEW_WS, "is_default": True}]}}),
        ("POST", "/api/billing/api-keys", None):
            (200, {"json": {"key": NEW_KEY, "key_id": NEW_KEY_ID}}),
    }
    transport, calls = _fake_console(routes)
    rb = OrgRebuilder(transport=transport)
    acc = _acc(console_session="")  # 无会话，必须密码登

    import asyncio
    result = asyncio.run(rb.rebuild(acc))

    assert result.ok is True
    assert login_calls == [("mist747289@moonstarsun.shop", "Pw9060409!xAa1!")]
    # 后续调用用的是登录拿到的 session
    assert result.api_key == NEW_KEY
    assert result.session and "fresh-session" in result.session


def test_rebuild_delete_fails_returns_not_ok():
    """删组织失败时不继续后续步骤，返回 ok=False。"""
    routes = {
        ("DELETE", "/api/users/organization/", None):
            (403, {"json": {"detail": "forbidden"}}),
    }
    transport, calls = _fake_console(routes)
    rb = OrgRebuilder(transport=transport)

    import asyncio
    result = asyncio.run(rb.rebuild(_acc()))

    assert result.ok is False
    assert result.error
    # 失败后不应再发后续请求
    paths = [c["path"] for c in calls if c["x_csrftoken"]]
    assert all("/api/users/organizations" not in p for p in paths)


def test_rebuild_new_org_has_no_default_workspace_returns_not_ok():
    """新组织建出来却查不到 default workspace，说明上游行为变了，不能硬塞。"""
    routes = {
        ("DELETE", "/api/users/organization/", None):
            (200, {"json": {"details": "deleted"}}),
        ("POST", "/api/users/organizations", None):
            (200, {"json": {"uuid": NEW_ORG, "org_tier": "B"}}),
        ("GET", "/api/workspaces", None):
            (200, {"json": {"items": [
                {"uuid": "ws-non-default", "is_default": False}]}}),
    }
    transport, _ = _fake_console(routes)
    rb = OrgRebuilder(transport=transport)

    import asyncio
    result = asyncio.run(rb.rebuild(_acc()))

    assert result.ok is False
    assert "default" in result.error.lower() or "workspace" in result.error.lower()


def test_pool_apply_rebuild_updates_account_and_persists():
    """pool.apply_rebuild 原子地写入新凭据并清掉 exhausted 状态。"""
    import time
    pool = AccountPool()
    acc = _acc()
    pool.accounts.append(acc)
    acc.exhausted_until = time.time() + 99999  # 标记为耗尽
    acc.budget_used_pct = 100.0
    acc.last_status = "budget"

    result = RebuildResult(
        ok=True, org_id=NEW_ORG, workspace_id=NEW_WS,
        api_key=NEW_KEY, key_id=NEW_KEY_ID, session='{"ory_session_x":"s"}')

    applied = pool.apply_rebuild(acc, result)

    assert applied is True
    assert acc.org_id == NEW_ORG
    assert acc.workspace_id == NEW_WS
    assert acc.api_key == NEW_KEY
    assert acc.key_id == NEW_KEY_ID
    assert acc.console_session == '{"ory_session_x":"s"}'
    assert acc.exhausted_until == 0.0
    assert acc.budget_used_pct == 0.0
    assert acc.budget_checked_at == 0.0
    assert acc.last_status == "rebuilt"


def test_pool_apply_rebuild_skips_failed_result():
    """重建失败时不动账号，返回 False。"""
    import time
    pool = AccountPool()
    acc = _acc()
    pool.accounts.append(acc)
    old_key = acc.api_key
    acc.exhausted_until = time.time() + 99999

    result = RebuildResult(ok=False, error="delete failed")
    applied = pool.apply_rebuild(acc, result)

    assert applied is False
    assert acc.api_key == old_key
    assert acc.exhausted_until > 0  # 仍标记耗尽
