# -*- coding: utf-8 -*-
"""组织重建（建新组织→发新 key）的单元测试。

用 MockTransport 假装控制台，验证 OrgRebuilder 按正确顺序调用端点、
从响应里抠出新的 org_id / workspace_id / api_key / key_id，
以及 pool.add_org 把结果作为新 Org 挂到账号下。
"""
import json

import httpx

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
        mistral_password="Pw9060409!xAa1!",
        console_session='{"ory_session_x": "sess-token"}',
    )
    base.update(kw)
    return Account(**base)


def _routes(overrides: dict | None = None):
    """默认的建组织三步路由；overrides 里值为 None 表示去掉该步。"""
    routes = {
        ("POST", "/api/users/organizations", None):
            (200, {"json": {"uuid": NEW_ORG, "name": "1", "org_tier": "B"}}),
        ("GET", "/api/workspaces", None):
            (200, {"json": {"total": 1, "items": [
                {"uuid": NEW_WS, "name": "Default Workspace", "is_default": True}]}}),
        ("POST", "/api/billing/api-keys", None):
            (200, {"json": {"key": NEW_KEY, "key_id": NEW_KEY_ID}}),
    }
    for key, val in (overrides or {}).items():
        if val is None:
            routes.pop(key, None)
        else:
            routes[key] = val
    return routes


def test_rebuild_happy_path():
    """建新组织→列新 ws→发新 key，顺序与字段都对；旧组织保留不动。"""
    transport, calls = _fake_console(_routes())
    rb = OrgRebuilder(transport=transport)

    import asyncio
    result = asyncio.run(rb.rebuild(_acc()))

    assert isinstance(result, RebuildResult)
    assert result.ok is True
    assert result.org_id == NEW_ORG
    assert result.workspace_id == NEW_WS
    assert result.api_key == NEW_KEY
    assert result.key_id == NEW_KEY_ID

    # 顺序: POST organizations -> GET workspaces -> POST api-keys，全程无 DELETE
    methods = [(c["method"], c["path"]) for c in calls if c["x_csrftoken"]]
    assert methods[0] == ("POST", "/api/users/organizations")
    assert methods[1][0] == "GET" and methods[1][1].startswith("/api/workspaces")
    assert methods[2] == ("POST", "/api/billing/api-keys")
    assert all(m != "DELETE" for m, _ in methods), "重建不应删除旧组织"

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

    transport, calls = _fake_console(_routes())
    rb = OrgRebuilder(transport=transport)
    acc = _acc(console_session="")  # 无会话，必须密码登

    import asyncio
    result = asyncio.run(rb.rebuild(acc))

    assert result.ok is True
    assert login_calls == [("mist747289@moonstarsun.shop", "Pw9060409!xAa1!")]
    # 后续调用用的是登录拿到的 session
    assert result.api_key == NEW_KEY
    assert result.session and "fresh-session" in result.session


def test_rebuild_without_any_credential_returns_not_ok():
    rb = OrgRebuilder()
    result = rb.rebuild(_acc(console_session="", mistral_password=""))
    import asyncio
    result = asyncio.run(result) if hasattr(result, "__await__") else result
    assert result.ok is False
    assert result.error


def test_rebuild_create_org_fails_returns_not_ok():
    """建组织失败时不继续后续步骤，返回 ok=False。"""
    transport, calls = _fake_console(_routes({
        ("POST", "/api/users/organizations", None):
            (403, {"json": {"detail": "forbidden"}})}))
    rb = OrgRebuilder(transport=transport)

    import asyncio
    result = asyncio.run(rb.rebuild(_acc()))

    assert result.ok is False
    assert result.error
    # 失败后不应再发后续请求
    paths = [c["path"] for c in calls if c["x_csrftoken"]]
    assert all("/api/billing/api-keys" not in p for p in paths)


def test_rebuild_new_org_has_no_default_workspace_returns_not_ok():
    """新组织建出来却查不到 default workspace，说明上游行为变了，不能硬塞。"""
    transport, _ = _fake_console(_routes({
        ("GET", "/api/workspaces", None):
            (200, {"json": {"items": [
                {"uuid": "ws-non-default", "is_default": False}]}})}))
    rb = OrgRebuilder(transport=transport)

    import asyncio
    result = asyncio.run(rb.rebuild(_acc()))

    assert result.ok is False
    assert "default" in result.error.lower() or "workspace" in result.error.lower()


def test_pool_add_org_appends_new_org_and_persists(tmp_path):
    """add_org 把重建结果作为新 Org 挂到账号下，旧 Org 保持不动。"""
    from core.pool import Org
    from core.store import UsageStore

    store = UsageStore(str(tmp_path / "t.db"), start_writer=False)
    pool = AccountPool(store)
    pool.import_records([{"email": "mist747289@moonstarsun.shop",
                          "api_key": "sk-oldkey", "org_id": OLD_ORG,
                          "mistral_password": "Pw9060409!xAa1!",
                          "console_session": '{"ory_session_x": "sess-token"}'}],
                        persist=True)
    acc = pool.accounts[0]
    old_org = OLD_ORG
    assert acc.orgs[0].org_id == old_org

    result = RebuildResult(
        ok=True, org_id=NEW_ORG, workspace_id=NEW_WS,
        api_key=NEW_KEY, key_id=NEW_KEY_ID, session='{"ory_session_x":"s"}')

    org = pool.add_org(acc, result)

    assert org is not None
    assert [o.org_id for o in acc.orgs] == [old_org, NEW_ORG], "旧组织保留，新组织追加"
    assert org.api_key == NEW_KEY and org.workspace_id == NEW_WS
    assert acc.console_session == '{"ory_session_x":"s"}'
    # 落库可恢复
    fresh = AccountPool(store)
    fresh.load_from_store()
    new_ids = [o.org_id for o in fresh.accounts[0].orgs]
    assert new_ids == [old_org, NEW_ORG]
    assert fresh.accounts[0].orgs[1].api_key == NEW_KEY
    store.close()


def test_pool_add_org_is_idempotent():
    """同一 org_id 重复 add 不产生重复组织。"""
    pool = AccountPool()
    acc = _acc()
    pool.accounts.append(acc)
    result = RebuildResult(ok=True, org_id=NEW_ORG, workspace_id=NEW_WS,
                           api_key=NEW_KEY, key_id=NEW_KEY_ID)
    first = pool.add_org(acc, result)
    again = pool.add_org(acc, result)
    assert first is again
    assert len(acc.orgs) == 1


def test_pool_add_org_skips_failed_result():
    """重建失败时不动账号，返回 None。"""
    pool = AccountPool()
    acc = _acc()
    pool.accounts.append(acc)

    result = RebuildResult(ok=False, error="delete failed")
    applied = pool.add_org(acc, result)

    assert applied is None
    assert acc.orgs == []
