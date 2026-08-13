# -*- coding: utf-8 -*-
import concurrent.futures
import time

from core.store import UsageStore


def make_store(tmp_path, name="t.db", writer=False):
    return UsageStore(str(tmp_path / name), start_writer=writer)


def test_wal_enabled(tmp_path):
    store = make_store(tmp_path)
    mode = store._con.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    store.close()


def test_record_and_stats(tmp_path):
    store = make_store(tmp_path)
    store.record("a@x.com", "m1", "/v1/chat/completions", 200, 10, 20, 100, False)
    store.record("a@x.com", "m1", "/v1/chat/completions", 429, 0, 0, 50, True, "rate limited")
    store.flush()

    s = store.stats(24)
    assert s["overview"]["all"]["n"] == 2
    assert s["overview"]["all"]["tok"] == 30
    assert s["overview"]["all"]["ok"] == 1
    assert s["overview"]["all"]["r429"] == 1
    assert s["by_model"][0]["model"] == "m1"
    assert len(s["errors"]) == 1
    assert s["errors"][0]["status"] == 429
    store.close()


def test_stats_on_empty_db_has_no_nulls(tmp_path):
    """空库时聚合列必须是 0 而不是 None, 否则前端算成功率会炸。"""
    store = make_store(tmp_path)
    s = store.stats(24)
    for bucket in s["overview"].values():
        assert all(v is not None for v in bucket.values())
        assert bucket["n"] == 0 and bucket["ok"] == 0
    store.close()


def test_stats_window_scoping(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m", "/e", 200, 1, 1, 10, False)
    store.flush()
    with store._lock, store._con:
        store._con.execute("UPDATE requests SET ts = ?", (time.time() - 10 * 3600,))

    assert store.stats(hours=1)["overview"]["window"]["n"] == 0
    assert store.stats(hours=24)["overview"]["window"]["n"] == 1
    assert store.stats(hours=1)["overview"]["all"]["n"] == 1
    store.close()


def test_cleanup_removes_old_rows(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m", "/e", 200, 1, 1, 10, False)
    store.flush()
    with store._lock, store._con:
        store._con.execute("UPDATE requests SET ts = ?", (time.time() - 40 * 86400,))
    assert store.cleanup(days=30) == 1
    assert store.stats(24)["overview"]["all"]["n"] == 0
    store.close()


def test_export_rows_filters_by_model(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m1", "/e", 200, 1, 1, 10, False)
    store.record("a", "m2", "/e", 200, 1, 1, 10, False)
    store.flush()
    assert len(store.export_rows(24)) == 2
    assert len(store.export_rows(24, model="m2")) == 1
    store.close()


def test_concurrent_writes_do_not_lock(tmp_path):
    """旧版每次请求 sqlite3.connect() 且无 busy_timeout, 并发下会抛 database is locked。"""
    store = make_store(tmp_path, writer=True)

    def write(i):
        store.record(f"a{i}", "m", "/e", 200, 1, 1, 1, False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(write, range(500)))
    store.flush(timeout=10)

    assert store.stats(24)["overview"]["all"]["n"] == 500
    assert store.dropped == 0
    store.close()


def test_account_record_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.save_account_records([{"email": "a@x.com", "api_key": "k", "org_id": "o1"}])
    store.save_account_records([{"email": "a@x.com", "api_key": "k2", "org_id": "o1"}])
    rows = store.load_account_records()
    assert len(rows) == 1 and rows[0]["api_key"] == "k2"
    store.close()


def test_tombstone_lifecycle(tmp_path):
    store = make_store(tmp_path)
    store.save_account_records([{"email": "a@x.com", "api_key": "k"}])
    store.delete_account("a@x.com")
    assert store.load_account_records() == []
    assert store.deleted_emails() == {"a@x.com"}
    store.undelete("a@x.com")
    assert store.deleted_emails() == set()
    store.close()


def test_schema_upgrade_on_existing_db(tmp_path):
    """老库只有 requests / accounts 两张表且缺列, 打开时应自动补齐且不丢数据。"""
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, account TEXT, model TEXT,
        endpoint TEXT, status INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
        total_tokens INTEGER, duration_ms INTEGER, stream INTEGER, error TEXT)""")
    con.execute("INSERT INTO requests (ts, account, model, status, total_tokens)"
                " VALUES (?,?,?,?,?)", (time.time(), "old@x.com", "m", 200, 5))
    con.commit()
    con.close()

    store = UsageStore(path, start_writer=False)
    assert store.stats(24)["overview"]["all"]["n"] == 1
    assert store.deleted_emails() == set()
    # 新列补齐后写入新格式的行不应报错
    store.record("new@x.com", "m", "/e", 200, ttft_ms=42, attempts=2, client_key="ck")
    store.flush()
    rows = store.export_rows(1)
    assert any(r["ttft_ms"] == 42 and r["client_key"] == "ck" for r in rows)
    store.close()


# ---------- 新增指标 ----------

def test_new_columns_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.record("a@x.com", "upstream-model", "/v1/chat/completions", 200,
                 prompt_tokens=10, completion_tokens=20, duration_ms=500, stream=True,
                 requested_model="gpt-4o", reasoning_tokens=8, cached_tokens=4,
                 ttft_ms=120, attempts=3, finish_reason="stop", client_key="ck1")
    store.flush()
    row = store.export_rows(1)[0]
    assert row["requested_model"] == "gpt-4o"
    assert row["model"] == "upstream-model"
    assert (row["reasoning_tokens"], row["cached_tokens"]) == (8, 4)
    assert (row["ttft_ms"], row["attempts"], row["finish_reason"]) == (120, 3, "stop")
    assert row["client_key"] == "ck1"
    store.close()


def test_percentiles(tmp_path):
    store = make_store(tmp_path)
    for i in range(1, 101):
        store.record("a", "m", "/e", 200, duration_ms=i, ttft_ms=i * 2)
    store.flush()
    lat = store.stats(24)["latency"]
    assert lat["duration"]["n"] == 100
    assert 49 <= lat["duration"]["p50"] <= 52
    assert 94 <= lat["duration"]["p95"] <= 97
    assert 98 <= lat["duration"]["p99"] <= 100
    assert lat["ttft"]["p50"] == lat["duration"]["p50"] * 2
    store.close()


def test_percentiles_ignore_failures_and_zeros(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m", "/e", 200, duration_ms=100)
    store.record("a", "m", "/e", 429, duration_ms=9999)
    store.record("a", "m", "/e", 200, duration_ms=0)
    store.flush()
    assert store.stats(24)["latency"]["duration"]["n"] == 1
    store.close()


def test_percentiles_empty(tmp_path):
    store = make_store(tmp_path)
    lat = store.stats(24)["latency"]
    assert lat["duration"] == {"p50": 0, "p90": 0, "p95": 0, "p99": 0, "n": 0}
    store.close()


def test_stats_groups_by_requested_model(tmp_path):
    """按客户端请求的模型名聚合, 别名和真实模型要分开看。"""
    store = make_store(tmp_path)
    store.record("a", "mistral-large-latest", "/e", 200, requested_model="gpt-4o",
                 reasoning_tokens=5, prompt_tokens=1, completion_tokens=9)
    store.record("a", "mistral-large-latest", "/e", 200,
                 requested_model="mistral-large-latest")
    store.flush()
    models = {m["model"]: m for m in store.stats(24)["by_model"]}
    assert set(models) == {"gpt-4o", "mistral-large-latest"}
    assert models["gpt-4o"]["rtok"] == 5
    store.close()


def test_stats_by_client_and_status_and_endpoint(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m", "/v1/chat/completions", 200, client_key="ck1")
    store.record("a", "m", "/v1/chat/completions", 429, client_key="ck1")
    store.record("a", "m", "/v1/embeddings", 200, client_key="ck2")
    store.record("a", "m", "/v1/embeddings", 200)  # 匿名, 不计入 by_client
    store.flush()
    s = store.stats(24)
    assert {c["client_key"]: c["n"] for c in s["by_client"]} == {"ck1": 2, "ck2": 1}
    assert {r["status"]: r["n"] for r in s["by_status"]} == {200: 3, 429: 1}
    assert {e["endpoint"]: e["n"] for e in s["by_endpoint"]} == {
        "/v1/chat/completions": 2, "/v1/embeddings": 2}
    store.close()


def test_retries_counted(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m", "/e", 200, attempts=3)
    store.record("a", "m", "/e", 200, attempts=1)
    store.flush()
    assert store.stats(24)["overview"]["window"]["retries"] == 2
    store.close()


def test_time_bucket_switches_to_daily_for_long_windows(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m", "/e", 200)
    store.flush()
    assert store.stats(24)["bucket"] == 3600
    assert store.stats(720)["bucket"] == 86400
    store.close()


def test_client_key_usage_today(tmp_path):
    store = make_store(tmp_path)
    store.record("a", "m", "/e", 200, prompt_tokens=10, completion_tokens=5, client_key="ck1")
    store.record("a", "m", "/e", 200, prompt_tokens=1, completion_tokens=1, client_key="ck1")
    store.flush()
    assert store.client_key_usage_today()["ck1"] == {"requests": 2, "tokens": 17}
    store.close()


def test_meta_kv(tmp_path):
    store = make_store(tmp_path)
    assert store.get_meta("nope") is None
    store.set_meta("k", "v1")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"
    store.close()
