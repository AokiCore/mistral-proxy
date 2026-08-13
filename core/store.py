# -*- coding: utf-8 -*-
"""SQLite 持久化层。

设计要点:
  - 单连接 + WAL + busy_timeout, 不每次请求都 sqlite3.connect(), 避免 "database is locked"
  - 用量写入走后台批量写线程, 调用方非阻塞(代理热路径不碰磁盘)
  - 账号凭据入库, DB 是唯一事实源; 配合 deleted_accounts 墓碑表, 删除的账号不会因重新导入而复活
  - 建表全部 IF NOT EXISTS, 缺列用 ALTER TABLE 补, 老库可以直接打开
"""
import json
import logging
import queue
import sqlite3
import threading
import time

log = logging.getLogger("store")

ACCOUNT_FIELDS = ("email", "api_key", "email_password", "mistral_password",
                  "org_id", "workspace_id", "key_id", "org_tier", "created_at",
                  "console_session")

# 注册时顺手带回来的控制台会话（有效期 90 天），查额度用，省掉密码登录
RECORD_MIGRATIONS = {"console_session": "TEXT"}

STATE_FIELDS = ("email", "enabled", "limit_tokens", "remaining_tokens", "limit_req",
                "remaining_req", "window_start", "cooldown_until", "last_used",
                "last_status", "consecutive_errors", "last_remaining_after",
                "budget_used_pct", "budget_total", "budget_reset_at",
                "budget_checked_at", "exhausted_until")

# 免费档是每月美元额度而非 token 配额，耗尽后整号 402，这几列记录额度状态
ACCOUNT_MIGRATIONS = {
    "budget_used_pct": "REAL DEFAULT 0", "budget_total": "REAL DEFAULT 0",
    "budget_reset_at": "TEXT", "budget_checked_at": "REAL DEFAULT 0",
    "exhausted_until": "REAL DEFAULT 0",
}

USAGE_FIELDS = ("ts", "account", "model", "requested_model", "endpoint", "status",
                "prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
                "total_tokens", "duration_ms", "ttft_ms", "attempts", "stream",
                "finish_reason", "client_key", "error")

# 老库缺少的列，打开时自动补
USAGE_MIGRATIONS = {
    "requested_model": "TEXT", "reasoning_tokens": "INTEGER DEFAULT 0",
    "cached_tokens": "INTEGER DEFAULT 0", "ttft_ms": "INTEGER DEFAULT 0",
    "attempts": "INTEGER DEFAULT 1", "finish_reason": "TEXT", "client_key": "TEXT",
}

CLIENT_KEY_FIELDS = ("id", "name", "key_hash", "prefix", "enabled", "created_at",
                     "expires_at", "rpm_limit", "daily_token_limit", "allowed_models",
                     "total_requests", "total_tokens", "last_used")

_WRITE_BATCH = 200
_WRITER_POLL = 0.25


class UsageStore:
    def __init__(self, db_path: str, start_writer: bool = True):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

        self.dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=20000)
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        if start_writer:
            self._writer = threading.Thread(target=self._writer_loop, daemon=True,
                                            name="usage-writer")
            self._writer.start()

    # ---------- schema ----------

    def _init_schema(self) -> None:
        with self._lock, self._con:
            c = self._con
            c.execute("""CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, account TEXT, model TEXT, requested_model TEXT, endpoint TEXT,
                status INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
                reasoning_tokens INTEGER DEFAULT 0, cached_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER, duration_ms INTEGER, ttft_ms INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 1, stream INTEGER, finish_reason TEXT,
                client_key TEXT, error TEXT)""")
            existing = {r["name"] for r in c.execute("PRAGMA table_info(requests)")}
            for column, decl in USAGE_MIGRATIONS.items():
                if column not in existing:
                    c.execute(f"ALTER TABLE requests ADD COLUMN {column} {decl}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_req_acc ON requests(account)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_req_ck ON requests(client_key, ts)")

            c.execute("""CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY,
                enabled INTEGER, limit_tokens INTEGER, remaining_tokens INTEGER,
                limit_req INTEGER, remaining_req INTEGER, window_start REAL,
                cooldown_until REAL, last_used REAL, last_status TEXT,
                consecutive_errors INTEGER, last_remaining_after TEXT,
                budget_used_pct REAL DEFAULT 0, budget_total REAL DEFAULT 0,
                budget_reset_at TEXT, budget_checked_at REAL DEFAULT 0,
                exhausted_until REAL DEFAULT 0)""")
            have = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
            for column, decl in ACCOUNT_MIGRATIONS.items():
                if column not in have:
                    c.execute(f"ALTER TABLE accounts ADD COLUMN {column} {decl}")
            c.execute("""CREATE TABLE IF NOT EXISTS account_records (
                email TEXT PRIMARY KEY, api_key TEXT, email_password TEXT,
                mistral_password TEXT, org_id TEXT, workspace_id TEXT,
                key_id TEXT, org_tier TEXT, created_at TEXT, console_session TEXT)""")
            have = {r["name"] for r in c.execute("PRAGMA table_info(account_records)")}
            for column, decl in RECORD_MIGRATIONS.items():
                if column not in have:
                    c.execute(f"ALTER TABLE account_records ADD COLUMN {column} {decl}")
            c.execute("""CREATE TABLE IF NOT EXISTS deleted_accounts (
                email TEXT PRIMARY KEY, ts REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY, v TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS client_keys (
                id TEXT PRIMARY KEY, name TEXT, key_hash TEXT UNIQUE, prefix TEXT,
                enabled INTEGER, created_at REAL, expires_at REAL, rpm_limit INTEGER,
                daily_token_limit INTEGER, allowed_models TEXT,
                total_requests INTEGER, total_tokens INTEGER, last_used REAL)""")

    # ---------- 用量写入 (异步批量) ----------

    def record(self, account: str, model: str, endpoint: str, status: int,
               prompt_tokens: int = 0, completion_tokens: int = 0, duration_ms: int = 0,
               stream: bool = False, error: str = "", *, requested_model: str = "",
               reasoning_tokens: int = 0, cached_tokens: int = 0, ttft_ms: int = 0,
               attempts: int = 1, finish_reason: str = "", client_key: str = "") -> None:
        """非阻塞记录一次请求。队列满时丢弃并计数, 绝不阻塞代理热路径。"""
        pt, ct = int(prompt_tokens or 0), int(completion_tokens or 0)
        row = (time.time(), account, model, requested_model or model, endpoint, int(status),
               pt, ct, int(reasoning_tokens or 0), int(cached_tokens or 0), pt + ct,
               int(duration_ms), int(ttft_ms or 0), int(attempts or 1),
               1 if stream else 0, finish_reason or "", client_key or "",
               (error or "")[:200])
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def _writer_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=_WRITER_POLL)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            batch = [first]
            try:
                while len(batch) < _WRITE_BATCH:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            try:
                self._insert_usage(batch)
            except Exception:
                log.exception("usage write failed")
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _insert_usage(self, rows: list[tuple]) -> None:
        placeholders = ",".join("?" * len(USAGE_FIELDS))
        with self._lock, self._con:
            self._con.executemany(
                f"INSERT INTO requests ({','.join(USAGE_FIELDS)}) VALUES ({placeholders})",
                rows)

    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        if self._writer is None:
            rows = []
            while True:
                try:
                    rows.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if rows:
                self._insert_usage(rows)

    def close(self) -> None:
        self._stop.set()
        if self._writer is not None:
            self.flush()
            self._writer.join(timeout=3.0)
        with self._lock:
            self._con.close()

    # ---------- meta ----------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._con:
            self._con.execute(
                "INSERT INTO meta (k, v) VALUES (?,?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    # ---------- 客户端密钥 ----------

    def load_client_keys(self) -> list:
        from core.clientkeys import ClientKey
        with self._lock:
            rows = self._con.execute("SELECT * FROM client_keys").fetchall()
        out = []
        for r in rows:
            try:
                allowed = json.loads(r["allowed_models"] or "[]")
            except json.JSONDecodeError:
                allowed = []
            out.append(ClientKey(
                id=r["id"], name=r["name"] or "", key_hash=r["key_hash"],
                prefix=r["prefix"] or "", enabled=bool(r["enabled"]),
                created_at=r["created_at"] or 0.0, expires_at=r["expires_at"] or 0.0,
                rpm_limit=r["rpm_limit"] or 0, daily_token_limit=r["daily_token_limit"] or 0,
                allowed_models=allowed, total_requests=r["total_requests"] or 0,
                total_tokens=r["total_tokens"] or 0, last_used=r["last_used"] or 0.0))
        return out

    def save_client_key(self, key) -> None:
        row = (key.id, key.name, key.key_hash, key.prefix, 1 if key.enabled else 0,
               key.created_at, key.expires_at, key.rpm_limit, key.daily_token_limit,
               json.dumps(key.allowed_models), key.total_requests, key.total_tokens,
               key.last_used)
        placeholders = ",".join("?" * len(CLIENT_KEY_FIELDS))
        updates = ",".join(f"{f}=excluded.{f}" for f in CLIENT_KEY_FIELDS if f != "id")
        with self._lock, self._con:
            self._con.execute(
                f"INSERT INTO client_keys ({','.join(CLIENT_KEY_FIELDS)})"
                f" VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}", row)

    def delete_client_key(self, key_id: str) -> None:
        with self._lock, self._con:
            self._con.execute("DELETE FROM client_keys WHERE id=?", (key_id,))

    def client_key_usage_today(self) -> dict:
        since = time.time() - 86400
        with self._lock:
            rows = self._con.execute(
                "SELECT client_key, COUNT(*) n, COALESCE(SUM(total_tokens),0) tok"
                " FROM requests WHERE ts>=? AND client_key!='' GROUP BY client_key",
                (since,)).fetchall()
        return {r["client_key"]: {"requests": r["n"], "tokens": r["tok"]} for r in rows}

    # ---------- 账号 ----------

    def load_account_records(self) -> list[dict]:
        with self._lock:
            rows = self._con.execute(
                f"SELECT {','.join(ACCOUNT_FIELDS)} FROM account_records").fetchall()
        return [dict(r) for r in rows]

    def save_account_records(self, records: list[dict]) -> None:
        if not records:
            return
        rows = [tuple(r.get(f) or "" for f in ACCOUNT_FIELDS) for r in records]
        placeholders = ",".join("?" * len(ACCOUNT_FIELDS))
        updates = ",".join(f"{f}=excluded.{f}" for f in ACCOUNT_FIELDS if f != "email")
        with self._lock, self._con:
            self._con.executemany(
                f"INSERT INTO account_records ({','.join(ACCOUNT_FIELDS)})"
                f" VALUES ({placeholders})"
                f" ON CONFLICT(email) DO UPDATE SET {updates}", rows)

    def delete_account(self, email: str) -> None:
        with self._lock, self._con:
            self._con.execute("DELETE FROM account_records WHERE email=?", (email,))
            self._con.execute("DELETE FROM accounts WHERE email=?", (email,))
            self._con.execute(
                "INSERT INTO deleted_accounts (email, ts) VALUES (?,?)"
                " ON CONFLICT(email) DO UPDATE SET ts=excluded.ts", (email, time.time()))

    def deleted_emails(self) -> set[str]:
        with self._lock:
            rows = self._con.execute("SELECT email FROM deleted_accounts").fetchall()
        return {r["email"] for r in rows}

    def undelete(self, email: str) -> None:
        with self._lock, self._con:
            self._con.execute("DELETE FROM deleted_accounts WHERE email=?", (email,))

    def load_states(self) -> dict[str, dict]:
        with self._lock:
            rows = self._con.execute("SELECT * FROM accounts").fetchall()
        return {r["email"]: dict(r) for r in rows}

    def save_states(self, rows: list[tuple]) -> None:
        if not rows:
            return
        placeholders = ",".join("?" * len(STATE_FIELDS))
        updates = ",".join(f"{f}=excluded.{f}" for f in STATE_FIELDS if f != "email")
        with self._lock, self._con:
            self._con.executemany(
                f"INSERT INTO accounts ({','.join(STATE_FIELDS)})"
                f" VALUES ({placeholders}) ON CONFLICT(email) DO UPDATE SET {updates}", rows)

    # ---------- 查询 ----------

    def cleanup(self, days: int = 30) -> int:
        cutoff = time.time() - days * 86400
        with self._lock, self._con:
            return self._con.execute("DELETE FROM requests WHERE ts < ?", (cutoff,)).rowcount

    def export_rows(self, hours: int, model: str = "") -> list[sqlite3.Row]:
        sql = ("SELECT ts, account, client_key, model, requested_model, endpoint, status,"
               " prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens,"
               " total_tokens, duration_ms, ttft_ms, attempts, stream, finish_reason, error"
               " FROM requests WHERE ts>=?")
        args: list = [time.time() - hours * 3600]
        if model:
            sql += " AND (model=? OR requested_model=?)"
            args += [model, model]
        with self._lock:
            return self._con.execute(sql + " ORDER BY id DESC", args).fetchall()

    LOG_COLUMNS = ("id, ts, account, client_key, model, requested_model, endpoint, status,"
                   " prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens,"
                   " total_tokens, duration_ms, ttft_ms, attempts, stream, finish_reason,"
                   " error")

    def query_logs(self, *, hours: int = 24, page: int = 1, limit: int = 50,
                   status: str = "", account: str = "", client_key: str = "",
                   model: str = "", endpoint: str = "", stream: str = "",
                   search: str = "") -> dict:
        """带筛选与分页的请求日志。status 支持 'error' 表示所有非 200。"""
        where = ["ts >= ?"]
        args: list = [time.time() - max(1, hours) * 3600]

        if status == "error":
            where.append("status != 200")
        elif status:
            try:
                where.append("status = ?")
                args.append(int(status))
            except ValueError:
                pass
        if account:
            where.append("account LIKE ?")
            args.append(f"%{account}%")
        if client_key:
            where.append("client_key = ?")
            args.append(client_key)
        if model:
            where.append("(requested_model LIKE ? OR model LIKE ?)")
            args += [f"%{model}%", f"%{model}%"]
        if endpoint:
            where.append("endpoint = ?")
            args.append(endpoint)
        if stream in ("0", "1"):
            where.append("stream = ?")
            args.append(int(stream))
        if search:
            where.append("(error LIKE ? OR account LIKE ? OR requested_model LIKE ?)")
            args += [f"%{search}%"] * 3

        clause = " AND ".join(where)
        limit = max(1, min(200, int(limit)))
        page = max(1, int(page))

        with self._lock:
            total = self._con.execute(
                f"SELECT COUNT(*) n FROM requests WHERE {clause}", args).fetchone()["n"]
            rows = self._con.execute(
                f"SELECT {self.LOG_COLUMNS} FROM requests WHERE {clause}"
                f" ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, (page - 1) * limit]).fetchall()
            totals = self._con.execute(
                f"SELECT COALESCE(SUM(total_tokens),0) tok,"
                f" COALESCE(SUM(CASE WHEN status=200 THEN 1 ELSE 0 END),0) ok"
                f" FROM requests WHERE {clause}", args).fetchone()

        return {"rows": [dict(r) for r in rows], "total": total, "page": page,
                "limit": limit, "pages": max(1, (total + limit - 1) // limit),
                "sum_tokens": totals["tok"], "ok": totals["ok"]}

    def distinct_values(self, column: str, hours: int = 168) -> list[str]:
        if column not in ("endpoint", "requested_model", "account", "client_key"):
            return []
        with self._lock:
            rows = self._con.execute(
                f"SELECT DISTINCT {column} v FROM requests WHERE ts>=? AND {column}!=''"
                f" ORDER BY v LIMIT 200", (time.time() - hours * 3600,)).fetchall()
        return [r["v"] for r in rows if r["v"]]

    def _percentiles(self, column: str, since: float, only_ok: bool = True) -> dict:
        """SQLite 没有内置分位数函数, 用 ORDER BY + OFFSET 取。"""
        where = f"ts>=? AND {column}>0" + (" AND status=200" if only_ok else "")
        total = self._con.execute(
            f"SELECT COUNT(*) n FROM requests WHERE {where}", (since,)).fetchone()["n"]
        if not total:
            return {"p50": 0, "p90": 0, "p95": 0, "p99": 0, "n": 0}
        out = {"n": total}
        for label, pct in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99)):
            offset = min(total - 1, int(total * pct / 100))
            row = self._con.execute(
                f"SELECT {column} v FROM requests WHERE {where}"
                f" ORDER BY {column} LIMIT 1 OFFSET ?", (since, offset)).fetchone()
            out[label] = row["v"] if row else 0
        return out

    def stats(self, hours: int = 24) -> dict:
        now = time.time()
        since = now - hours * 3600
        day_ago, week_ago = now - 86400, now - 7 * 86400

        with self._lock:
            def q(sql, *args):
                return self._con.execute(sql, args).fetchall()

            agg = ("SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,"
                   " COALESCE(SUM(prompt_tokens),0) ptok,"
                   " COALESCE(SUM(completion_tokens),0) ctok,"
                   " COALESCE(SUM(reasoning_tokens),0) rtok,"
                   " COALESCE(SUM(cached_tokens),0) cachetok,"
                   " COALESCE(SUM(CASE WHEN status=200 THEN 1 ELSE 0 END),0) ok,"
                   " COALESCE(SUM(CASE WHEN status=429 THEN 1 ELSE 0 END),0) r429,"
                   " COALESCE(SUM(CASE WHEN status>=500 THEN 1 ELSE 0 END),0) r5xx,"
                   " COALESCE(SUM(CASE WHEN stream=1 THEN 1 ELSE 0 END),0) streamed,"
                   " COALESCE(SUM(attempts-1),0) retries,"
                   " COALESCE(AVG(duration_ms),0) avg_ms FROM requests")
            # previous 是与 window 等长的上一个窗口，用于算环比涨跌
            overview = {
                "all": dict(q(agg)[0]),
                "today": dict(q(agg + " WHERE ts>=?", day_ago)[0]),
                "week": dict(q(agg + " WHERE ts>=?", week_ago)[0]),
                "window": dict(q(agg + " WHERE ts>=?", since)[0]),
                "previous": dict(q(agg + " WHERE ts>=? AND ts<?",
                                   since - hours * 3600, since)[0]),
            }
            latency = {"duration": self._percentiles("duration_ms", since),
                       "ttft": self._percentiles("ttft_ms", since)}

            by_account = [dict(r) for r in q(
                "SELECT account, COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,"
                " COALESCE(SUM(CASE WHEN status=200 THEN 1 ELSE 0 END),0) ok,"
                " COALESCE(SUM(CASE WHEN status=429 THEN 1 ELSE 0 END),0) r429,"
                " MAX(ts) last_ts, COALESCE(AVG(duration_ms),0) avg_ms"
                " FROM requests WHERE ts>=? GROUP BY account ORDER BY n DESC LIMIT 100", since)]
            by_model = [dict(r) for r in q(
                "SELECT requested_model model, COUNT(*) n,"
                " COALESCE(SUM(total_tokens),0) tok,"
                " COALESCE(SUM(reasoning_tokens),0) rtok,"
                " COALESCE(SUM(CASE WHEN status=200 THEN 1 ELSE 0 END),0) ok,"
                " COALESCE(SUM(CASE WHEN status=429 THEN 1 ELSE 0 END),0) r429,"
                " COALESCE(AVG(duration_ms),0) avg_ms"
                " FROM requests WHERE ts>=? GROUP BY requested_model ORDER BY n DESC", since)]
            by_client = [dict(r) for r in q(
                "SELECT client_key, COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,"
                " COALESCE(SUM(CASE WHEN status=200 THEN 1 ELSE 0 END),0) ok,"
                " COALESCE(AVG(duration_ms),0) avg_ms, MAX(ts) last_ts"
                " FROM requests WHERE ts>=? AND client_key!=''"
                " GROUP BY client_key ORDER BY n DESC LIMIT 50", since)]
            by_endpoint = [dict(r) for r in q(
                "SELECT endpoint, COUNT(*) n,"
                " COALESCE(SUM(CASE WHEN status=200 THEN 1 ELSE 0 END),0) ok"
                " FROM requests WHERE ts>=? GROUP BY endpoint ORDER BY n DESC", since)]
            by_status = [dict(r) for r in q(
                "SELECT status, COUNT(*) n FROM requests WHERE ts>=?"
                " GROUP BY status ORDER BY n DESC", since)]

            bucket = 3600 if hours <= 48 else 86400
            by_time = [dict(r) for r in q(
                f"SELECT CAST(ts/{bucket} AS INTEGER)*{bucket} t, COUNT(*) n,"
                " COALESCE(SUM(total_tokens),0) tok,"
                " COALESCE(SUM(CASE WHEN status=429 THEN 1 ELSE 0 END),0) r429,"
                " COALESCE(SUM(CASE WHEN status>=500 OR status=0 THEN 1 ELSE 0 END),0) err,"
                " COALESCE(AVG(duration_ms),0) avg_ms"
                f" FROM requests WHERE ts>=? GROUP BY t ORDER BY t", since)]

            errors = [dict(r) for r in q(
                "SELECT ts, account, requested_model model, status, endpoint, error"
                " FROM requests WHERE status!=200 ORDER BY id DESC LIMIT 30")]
            recent = [dict(r) for r in q(
                "SELECT ts, account, client_key, requested_model model, status,"
                " total_tokens, reasoning_tokens, duration_ms, ttft_ms, attempts,"
                " stream, finish_reason, error"
                " FROM requests ORDER BY id DESC LIMIT 60")]

        return {"overview": overview, "latency": latency, "by_account": by_account,
                "by_model": by_model, "by_client": by_client, "by_endpoint": by_endpoint,
                "by_status": by_status, "by_time": by_time, "bucket": bucket,
                "errors": errors, "recent": recent, "hours": hours,
                "dropped": self.dropped}
