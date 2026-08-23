# -*- coding: utf-8 -*-
import time

from core.pool import AccountPool, Org, est_tokens, parse_records
from core.store import UsageStore


def make_pool(n=3, store=None):
    pool = AccountPool(store)
    pool.import_records([{"email": f"a{i}@x.com", "api_key": f"k{i}", "org_id": f"o{i}"}
                         for i in range(n)],
                        persist=store is not None)
    return pool


def orgs(pool):
    """所有启用账号下的 Org，按导入顺序平铺。"""
    return [o for a in pool.accounts if a.enabled for o in a.orgs]


def first_org(pool):
    return orgs(pool)[0]


# ---------- token 估算 ----------

def test_est_tokens_handles_chinese():
    """旧版 len(chars)//4 对中文低估 5-6 倍, 会让调度器严重误判剩余配额。"""
    chinese = [{"role": "user", "content": "中" * 1000}]
    est = est_tokens(chinese)
    assert 700 <= est <= 1200, f"1000 个汉字应估到 ~1000 tokens, 实际 {est}"
    assert est_tokens([{"role": "user", "content": "a" * 1000}]) < est


def test_est_tokens_reads_multimodal_parts():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello world"},
                                         {"type": "image_url", "image_url": {"url": "x"}}]}]
    assert est_tokens(msgs) > 0


def test_est_tokens_tolerates_junk():
    assert est_tokens(None) == 0
    assert est_tokens([{"role": "user"}]) == 0
    assert est_tokens(["not a dict"]) == 0


# ---------- 调度 ----------

def test_pick_round_robins():
    pool = make_pool(3)
    picked = []
    for _ in range(3):
        org = pool.pick()
        picked.append(org.email)
        pool.release(org)
    assert len(set(picked)) == 3, "三次连续 pick 应命中三个不同账号"


def test_pick_counts_inflight_against_quota():
    """并发下不能把同一账号的窗口配额超额认购。"""
    pool = make_pool(1)
    first_org(pool).remaining_req = 2
    first, second = pool.pick(), pool.pick()
    assert first is second is first_org(pool)
    assert first_org(pool).inflight == 2
    third = pool.pick()
    # 配额只剩 2 且已有 2 个在途 -> RR 不再返回, 只能走兜底
    assert third is first_org(pool)
    assert first_org(pool).inflight == 3


def test_release_decrements_inflight_and_never_goes_negative():
    pool = make_pool(1)
    org = pool.pick()
    pool.release(org)
    pool.release(org)
    assert org.inflight == 0
    pool.release(None)


def test_pick_skips_disabled_and_cooling():
    pool = make_pool(3)
    pool.accounts[0].enabled = False
    orgs(pool)[0].cooldown_until = time.time() + 60
    for _ in range(3):
        org = pool.pick()
        assert org is orgs(pool)[1]
        pool.release(org)


# ---------- 超支防护：反复超支同一账号会被上游停用整个账号 ----------

def test_pick_skips_account_whose_window_cannot_fit():
    pool = make_pool(2)
    orgs(pool)[0].remaining_tokens = 1_000
    assert pool.pick(5_000) is orgs(pool)[1]


def test_reservation_blocks_concurrent_oversubscription():
    """两个并发请求各要 3 万，5 万的窗口只装得下一个，第二个必须换号。"""
    pool = make_pool(2)
    first = pool.pick(30_000)
    assert first is orgs(pool)[0]
    assert first.reserved_tokens == 30_000
    assert pool.pick(30_000) is orgs(pool)[1]


def test_release_returns_reservation():
    pool = make_pool(1)
    org = pool.pick(20_000)
    assert org.reserved_tokens == 20_000
    pool.release(org, 20_000)
    assert org.reserved_tokens == 0
    pool.release(org, 20_000)
    assert org.reserved_tokens == 0


def test_oversized_request_lands_on_full_window_account():
    """请求比整个窗口还大时，只让满窗账号承接，把超支限制成每窗口一次。"""
    pool = make_pool(2)
    orgs(pool)[0].remaining_tokens = 40_000        # 已用掉一部分
    org = pool.pick(60_000)
    assert org is orgs(pool)[1]


def test_oversized_requests_not_stacked_on_same_account():
    pool = make_pool(2)
    first = pool.pick(60_000)
    second = pool.pick(60_000)
    assert first is not second, "超大请求不能连续压在同一个账号上"


def test_oversized_still_dispatches_when_no_account_is_full():
    """所有账号都用过一点时仍要发得出去，否则大上下文直接不可用。"""
    pool = make_pool(2)
    for o in orgs(pool):
        o.remaining_tokens = 30_000
    assert pool.pick(60_000) is not None


def test_pick_returns_none_when_empty():
    assert AccountPool().pick() is None


def test_pick_returns_none_when_all_cooling():
    pool = make_pool(2)
    for o in orgs(pool):
        o.cooldown_until = time.time() + 60
    assert pool.pick() is None
    assert pool.next_window_wait() > 0


# ---------- 限流窗口 ----------

def test_mark_error_429_cools_until_window_end():
    pool = make_pool(1)
    org = first_org(pool)
    org.window_start = time.time()
    pool.mark_error(org, 429)
    assert org.remaining_req == 0
    assert org.remaining_tokens == 0
    assert org.cooldown_until > time.time()


def test_mark_error_429_respects_retry_after():
    pool = make_pool(1)
    pool.mark_error(first_org(pool), 429, retry_after=300)
    assert first_org(pool).cooldown_until >= time.time() + 290


def test_mark_error_5xx_backs_off_progressively():
    pool = make_pool(1)
    org = first_org(pool)
    pool.mark_error(org, 500)
    first = org.cooldown_until
    pool.mark_error(org, 500)
    assert org.cooldown_until > first
    assert org.consecutive_errors == 2


def test_success_clears_error_streak():
    pool = make_pool(1)
    org = first_org(pool)
    pool.mark_error(org, 500)
    pool.mark_success(org)
    assert org.consecutive_errors == 0
    assert org.last_status == "ok"


def test_window_expiry_restores_quota():
    pool = make_pool(1)
    org = first_org(pool)
    pool.mark_error(org, 429)
    org.window_start = time.time() - 61
    org.cooldown_until = 0
    picked = pool.pick()
    assert picked is org
    assert org.remaining_req == org.limit_req


def test_update_limits_parses_headers():
    pool = make_pool(1)
    org = first_org(pool)
    pool.update_limits(org, {"X-RateLimit-Limit-Tokens-Minute": "250000",
                             "x-ratelimit-remaining-tokens-minute": "1234",
                             "x-ratelimit-limit-req-minute": "50",
                             "x-ratelimit-remaining-req-minute": "7"})
    assert (org.limit_tokens, org.remaining_tokens) == (250000, 1234)
    assert (org.limit_req, org.remaining_req) == (50, 7)


def test_update_limits_ignores_garbage():
    pool = make_pool(1)
    org = first_org(pool)
    before = org.limit_tokens
    pool.update_limits(org, {"x-ratelimit-limit-tokens-minute": "n/a"})
    assert org.limit_tokens == before


# ---------- 导入 / 删除 ----------

def test_parse_records_accepts_json_and_csv():
    assert parse_records('[{"email":"a@x.com","api_key":"k"}]')[0]["email"] == "a@x.com"
    assert parse_records('{"email":"a@x.com","api_key":"k"}')[0]["api_key"] == "k"
    csv_rows = parse_records("email,api_key\na@x.com,k1\nb@x.com,k2")
    assert [r["email"] for r in csv_rows] == ["a@x.com", "b@x.com"]
    assert parse_records("") == []


def test_record_without_key_creates_no_org():
    pool = AccountPool()
    added, _ = pool.import_records([{"email": "a@x.com"}, {"api_key": "k"},
                                    {"email": "b@x.com", "api_key": "k"}])
    assert added == 2
    assert [a.email for a in pool.accounts] == ["a@x.com", "b@x.com"]
    # 只有带 api_key 的记录才有可用组织；纯凭据记录不产生组织
    assert [(o.email, o.api_key) for o in orgs(pool)] == [("b@x.com", "k")]


def test_record_with_key_but_no_org_id_gets_placeholder_org():
    """只有 api_key 没有 org_id 的老文件也要能用，否则整池静默 429。"""
    pool = AccountPool()
    pool.import_records([{"email": "a@x.com", "api_key": "k"}])
    assert len(orgs(pool)) == 1
    assert orgs(pool)[0].api_key == "k"
    assert orgs(pool)[0].org_id


def test_import_updates_existing_key():
    pool = make_pool(1)
    added, updated = pool.import_records(
        [{"email": "a0@x.com", "api_key": "new", "org_id": "o0"}])
    assert (added, updated) == (0, 1)
    assert first_org(pool).api_key == "new"


def test_removed_account_is_not_resurrected_by_reimport(tmp_path):
    """旧版删除只改内存, 重启重新导入 keys 文件后账号会复活。"""
    store = UsageStore(str(tmp_path / "t.db"), start_writer=False)
    pool = make_pool(2, store)
    assert pool.remove_account("a0@x.com") is True

    pool.import_records([{"email": "a0@x.com", "api_key": "k0", "org_id": "o0"}])
    assert [a.email for a in pool.accounts] == ["a1@x.com"]

    fresh = AccountPool(store)
    fresh.load_from_store()
    assert [a.email for a in fresh.accounts] == ["a1@x.com"]

    store.undelete("a0@x.com")
    fresh.import_records([{"email": "a0@x.com", "api_key": "k0", "org_id": "o0"}])
    assert len(fresh.accounts) == 2
    store.close()


def test_manually_added_account_survives_restart(tmp_path):
    store = UsageStore(str(tmp_path / "t.db"), start_writer=False)
    pool = AccountPool(store)
    pool.import_records([{"email": "new@x.com", "api_key": "kk", "org_id": "oo"}])

    fresh = AccountPool(store)
    fresh.load_from_store()
    assert [a.email for a in fresh.accounts] == ["new@x.com"]
    assert orgs(fresh)[0].api_key == "kk"
    store.close()


def test_state_roundtrip(tmp_path):
    store = UsageStore(str(tmp_path / "t.db"), start_writer=False)
    pool = make_pool(1, store)
    pool.accounts[0].enabled = False
    pool.accounts[0].orgs[0].remaining_tokens = 42
    pool.save_states()

    fresh = AccountPool(store)
    fresh.load_from_store()
    assert fresh.accounts[0].enabled is False
    assert [o.remaining_tokens for o in fresh.accounts[0].orgs] == [42]
    store.close()


# ---------- 脱敏 ----------

def test_to_dict_hides_api_key_by_default():
    org = Org(email="a@x.com", org_id="o1", api_key="sk-abcdefghijklmnop")
    assert "api_key" not in org.to_dict()
    assert org.to_dict()["key_preview"] == "sk-abc…mnop"
    assert org.to_dict(reveal=True)["api_key"] == "sk-abcdefghijklmnop"


def test_summary_counts():
    pool = make_pool(3)
    pool.accounts[0].enabled = False
    orgs(pool)[0].cooldown_until = time.time() + 30
    s = pool.summary()
    assert s["total"] == 3 and s["enabled"] == 2 and s["cooling"] == 1
