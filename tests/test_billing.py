# -*- coding: utf-8 -*-
"""月度美元额度：解析、调度摘除、402 故障转移。

免费档发的是每月 10 美元 API 额度而不是 token 配额，花光后整号所有模型都 402。
实测两个号跑掉同样 8000 多万 token，打 embed 的只花 $0.74，打 glm-5-2 的直接用满，
所以只看 token 数判断不出账号死活，必须单独跟踪额度。
"""
import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from app import _pick_for_budget_check
from core.billing import (BillingError, Budget, extract_api_budget, next_reset_ts,
                          parse_budget)
from core.pool import AccountPool

# 控制台订阅页真实载荷的形状：vibe_budget 在前，api_budget 在后
PAGE = ('...,"budget":{"vibe_budget":{"usage_percentage":0,"initial_budget":10,'
        '"currency":"USD","reset_at":"2026-09-01T00:00:00Z","payg_enabled":false},'
        '"api_budget":{"usage_percentage":7.4025503,"initial_budget":10,'
        '"currency":"USD","reset_at":"2026-09-01T00:00:00Z","payg_enabled":false},'
        '"usage_percentage":7.4025503},...')


def make_pool(n=3):
    pool = AccountPool(None)
    pool.import_records([{"email": f"a{i}@x.com", "api_key": f"k{i}",
                          "mistral_password": "pw"} for i in range(n)], persist=False)
    return pool


# ---------- 解析 ----------

def test_extract_picks_api_budget_not_vibe():
    """两个额度对象长得一样，取错会把 API 用量读成 0。"""
    b = extract_api_budget(PAGE)
    assert b.used_pct == pytest.approx(7.4025503)
    assert b.total == 10
    assert b.reset_at == "2026-09-01T00:00:00Z"


def test_extract_handles_escaped_payload():
    """RSC 载荷里引号是转义的。"""
    assert extract_api_budget(PAGE.replace('"', '\\"')).total == 10


def test_parse_budget_from_api():
    """/api/billing/v2/budget 的返回是 376 字节纯 JSON，比抠页面可靠得多。"""
    b = parse_budget({
        "vibe_budget": {"usage_percentage": 0.0, "initial_budget": 10.0,
                        "currency": "USD", "reset_at": "2026-09-01T00:00:00Z"},
        "api_budget": {"usage_percentage": 67.3328703, "initial_budget": 10.0,
                       "currency": "USD", "reset_at": "2026-09-01T00:00:00Z"}})
    assert b.used_pct == pytest.approx(67.3328703)
    assert b.remaining == pytest.approx(3.26671297)


@pytest.mark.parametrize("bad", [{}, {"vibe_budget": {}}, {"api_budget": None},
                                 {"api_budget": "nope"}])
def test_parse_budget_rejects_junk(bad):
    with pytest.raises(BillingError):
        parse_budget(bad)


def test_extract_raises_without_budget():
    with pytest.raises(BillingError):
        extract_api_budget('{"something":"else"}')


def test_extract_raises_on_broken_object():
    with pytest.raises(BillingError):
        extract_api_budget('"api_budget":{"usage_percentage":1')


def test_budget_properties():
    assert Budget(used_pct=100.0, total=10).exhausted
    assert not Budget(used_pct=99.9, total=10).exhausted
    assert Budget(used_pct=25.0, total=10).remaining == pytest.approx(7.5)


def test_next_reset_parses_iso():
    ts = next_reset_ts("2026-09-01T00:00:00Z")
    assert datetime.fromtimestamp(ts, timezone.utc).month == 9


def test_next_reset_falls_back_to_first_of_next_month():
    for bad in ("", "not-a-date"):
        d = datetime.fromtimestamp(next_reset_ts(bad), timezone.utc)
        assert d.day == 1 and d > datetime.now(timezone.utc)


# ---------- 调度摘除 ----------

def test_exhausted_account_is_skipped():
    pool = make_pool(2)
    pool.update_budget(pool.accounts[0], Budget(used_pct=100.0, total=10,
                                                reset_at="2099-01-01T00:00:00Z"))
    for _ in range(4):
        acc = pool.pick()
        assert acc is pool.accounts[1]
        pool.release(acc)


def test_budget_recovery_puts_account_back():
    pool = make_pool(1)
    acc = pool.accounts[0]
    pool.update_budget(acc, Budget(used_pct=100.0, total=10,
                                   reset_at="2099-01-01T00:00:00Z"))
    assert pool.pick() is None
    pool.update_budget(acc, Budget(used_pct=3.0, total=10))
    assert pool.pick() is acc


def test_summary_excludes_exhausted_from_capacity():
    pool = make_pool(2)
    before = pool.summary()["tokens_left"]
    pool.update_budget(pool.accounts[0], Budget(used_pct=100.0, total=10,
                                                reset_at="2099-01-01T00:00:00Z"))
    after = pool.summary()
    assert after["exhausted"] == 1
    assert after["tokens_left"] == before // 2, "花光的号不该再算进可用容量"


def test_mark_exhausted_is_monotonic():
    pool = make_pool(1)
    acc = pool.accounts[0]
    far = time.time() + 86400
    pool.mark_exhausted(acc, far)
    pool.mark_exhausted(acc, time.time() + 10)
    assert acc.exhausted_until == far


# ---------- 巡检取号顺序 ----------

def test_checker_prefers_used_but_never_checked():
    pool = make_pool(3)
    now = time.time()
    pool.accounts[0].last_used = 0                      # 没用过 -> 不查
    pool.accounts[1].last_used = now - 10                # 用过没查过 -> 最优先
    pool.accounts[2].last_used = now - 5
    pool.accounts[2].budget_checked_at = now - 1
    assert _pick_for_budget_check(pool, 3600, now) is pool.accounts[1]


def test_checker_skips_idle_and_exhausted():
    pool = make_pool(2)
    now = time.time()
    pool.accounts[0].last_used = 0
    pool.accounts[1].last_used = now
    pool.accounts[1].exhausted_until = now + 3600
    assert _pick_for_budget_check(pool, 3600, now) is None


def test_checker_skips_accounts_without_any_credential():
    pool = make_pool(1)
    a = pool.accounts[0]
    a.last_used = time.time()
    a.mistral_password = ""
    assert _pick_for_budget_check(pool, 3600, time.time()) is None
    # 只要有注册时存下的会话就够，不需要密码
    a.console_session = '{"ory_session_x": "v"}'
    assert _pick_for_budget_check(pool, 3600, time.time()) is a


def test_stored_session_is_used_without_login(monkeypatch):
    """注册脚本存过会话的号，查额度不该再走登录。"""
    from core import billing

    logged = []

    async def no_login(self, email, password):
        logged.append(email)
        raise AssertionError("有会话就不该登录")

    async def read(self, cookies):
        assert cookies == {"ory_session_abc": "v"}
        return Budget(used_pct=12.0, total=10.0)

    monkeypatch.setattr(billing.BudgetClient, "_login", no_login)
    monkeypatch.setattr(billing.BudgetClient, "_read", read)
    b, session = asyncio.run(billing.BudgetClient().fetch(
        "a@x.com", "pw", '{"ory_session_abc": "v"}'))
    assert b.used_pct == 12.0
    assert session == '{"ory_session_abc": "v"}', "会话没变就原样返回"
    assert not logged


def test_expired_session_falls_back_to_password(monkeypatch):
    from core import billing

    calls = {"read": 0}

    async def login(self, email, password):
        jar = httpx.Cookies()
        jar.set("__cf_bm", "noise", domain=".mistral.ai")
        jar.set("ory_session_new", "v2", domain=".mistral.ai")
        return jar

    async def read(self, cookies):
        calls["read"] += 1
        if calls["read"] == 1:
            raise billing.BillingError("会话失效（401）")
        return Budget(used_pct=5.0, total=10.0)

    monkeypatch.setattr(billing.BudgetClient, "_login", login)
    monkeypatch.setattr(billing.BudgetClient, "_read", read)
    b, session = asyncio.run(billing.BudgetClient().fetch(
        "a@x.com", "pw", '{"ory_session_old": "v1"}'))
    assert b.used_pct == 5.0
    assert json.loads(session) == {"ory_session_new": "v2"}, "要把新会话回传去落库"


def test_fetch_without_credentials_raises(monkeypatch):
    from core import billing

    async def read(self, cookies):
        raise billing.BillingError("过期")

    monkeypatch.setattr(billing.BudgetClient, "_read", read)
    with pytest.raises(BillingError):
        asyncio.run(billing.BudgetClient().fetch("a@x.com", "", '{"ory_session": "x"}'))


def test_session_cookies_drops_cloudflare_noise():
    """用真的 httpx.Cookies：它直接迭代给的是名字字符串，不是 cookie 对象。"""
    from core.billing import session_cookies

    jar = httpx.Cookies()
    for name in ("__cf_bm", "ory_session_abc", "__cflb", "csrftoken"):
        jar.set(name, "v", domain=".mistral.ai")
    assert session_cookies(jar) == {"ory_session_abc": "v"}


def test_console_session_persists(tmp_path):
    """会话要跟着账号记录落库，重启后不用重新登录。"""
    from core.store import UsageStore

    db = str(tmp_path / "s.db")
    store = UsageStore(db, start_writer=False)
    pool = AccountPool(store)
    pool.import_records([{"email": "a@x.com", "api_key": "k"}])
    pool.set_console_session(pool.accounts[0], '{"ory_session_z": "v"}')
    store.close()

    store2 = UsageStore(db, start_writer=False)
    pool2 = AccountPool(store2)
    pool2.load_from_store()
    assert pool2.accounts[0].console_session == '{"ory_session_z": "v"}'
    store2.close()


def test_checker_refreshes_stale_entries():
    pool = make_pool(1)
    now = time.time()
    a = pool.accounts[0]
    a.last_used = now - 9000
    a.budget_checked_at = now - 8000
    assert _pick_for_budget_check(pool, 3600, now) is a
    assert _pick_for_budget_check(pool, 99999, now) is None


# ---------- 402 故障转移 ----------

def test_402_fails_over_to_another_account(make_client):
    """402 是这个号没钱了，换个号还有救，不能直接抛给客户端。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        seen.append(key)
        if key == "key-a@x.com":
            return httpx.Response(402, json={"detail": "Check your subscription"})
        return httpx.Response(200, json={
            "id": "1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    client = make_client(handler)
    with client:
        r = client.post("/v1/chat/completions", json={
            "model": "mistral-small-latest", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert len(seen) == 2, "应当换号重试一次"
        pool = client.app.state.ctx.pool
        dead = next(a for a in pool.accounts if a.email == "a@x.com")
        assert dead.exhausted_until > time.time(), "402 的号要被标记为额度耗尽"


def test_402_on_every_account_reports_upstream_failure(make_client):
    client = make_client(lambda r: httpx.Response(402, json={"detail": "no budget"}))
    with client:
        r = client.post("/v1/chat/completions", json={
            "model": "mistral-small-latest", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code in (402, 429, 502)
    assert "error" in r.json()


# ---------- 管理接口 ----------

def test_budget_endpoint_rejects_oversized_batch(make_client):
    client = make_client(lambda r: httpx.Response(200, json={}))
    with client:
        r = client.post("/admin/accounts/budget",
                        json={"emails": [f"x{i}@y.com" for i in range(21)]})
    assert r.status_code == 400


def test_budget_endpoint_reports_missing_password(make_client):
    client = make_client(lambda r: httpx.Response(200, json={}))
    with client:
        r = client.post("/admin/accounts/budget", json={"email": "a@x.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["checked"] == []
    assert "密码" in body["failed"][0]["error"]


def test_account_dict_exposes_budget(make_client):
    client = make_client(lambda r: httpx.Response(200, json={}))
    with client:
        rows = client.get("/admin/accounts").json()
    row = rows["accounts"][0] if isinstance(rows, dict) else rows[0]
    for field in ("budget_used_pct", "budget_total", "exhausted"):
        assert field in row, f"账号信息里缺 {field}"


def test_budget_survives_restart(tmp_path):
    """额度状态要落库，重启后不能忘了哪些号已经花光。"""
    from core.store import UsageStore

    db = str(tmp_path / "b.db")
    store = UsageStore(db, start_writer=False)
    pool = AccountPool(store)
    pool.import_records([{"email": "a@x.com", "api_key": "k"}])
    pool.update_budget(pool.accounts[0], Budget(used_pct=100.0, total=10,
                                                reset_at="2099-01-01T00:00:00Z"))
    pool.save_states()
    store.close()

    store2 = UsageStore(db, start_writer=False)
    pool2 = AccountPool(store2)
    pool2.load_from_store()
    assert pool2.accounts[0].budget_used_pct == 100.0
    assert pool2.accounts[0].exhausted_until > time.time()
    assert pool2.pick() is None
    store2.close()


def test_json_payload_shape_matches_console():
    """防止字段名漂移：这些 key 是从真实控制台载荷里抄的。"""
    d = json.loads(PAGE[PAGE.find('"api_budget":') + 13:].split('},')[0] + "}")
    assert set(d) >= {"usage_percentage", "initial_budget", "currency", "reset_at"}
