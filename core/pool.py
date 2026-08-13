# -*- coding: utf-8 -*-
"""Mistral 多账号池: 账号管理 + 限流感知调度。

实测依据 (保留自旧版测试记录):
  - Mistral 系模型: 50 req/min + 50,000 tokens/min; GLM 系: 50 req/min + 250,000 tokens/min
  - 限额按 org(账号)独立, 同账号多 key 共享, 多账号线性叠加
  - 单请求可超 token 窗口限额, 但 remaining 截断为 0, 该分钟窗口内后续请求全 429
  - 固定 60s 窗口; 429 响应通常无 Retry-After

本模块不做任何网络与 Web 框架相关的事, 便于单测。
"""
import csv
import io
import json
import os
import threading
import time
from dataclasses import dataclass, field

from .store import ACCOUNT_FIELDS, UsageStore

WINDOW_SECONDS = 60.0
DEFAULT_LIMIT_TOKENS = 50_000
DEFAULT_LIMIT_REQ = 50


def est_tokens(messages) -> int:
    """粗略估算输入 tokens, 仅用于账号选择。

    旧版按 len(chars)//4 估算, 对中文会低估 5-6 倍。改用 UTF-8 字节数 /3.5:
    ASCII 约 4 字符/token (1 字节/字符), CJK 约 1 字符/token (3 字节/字符),
    两者用同一系数的误差都在 15% 以内, 且是单次 C 层 encode, 对 1MB 上下文也够快。
    """
    total = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            total += len(content.encode("utf-8", "ignore"))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"].encode("utf-8", "ignore"))
    return int(total / 3.5)


@dataclass(slots=True)
class Account:
    email: str = ""
    api_key: str = ""
    email_password: str = ""
    mistral_password: str = ""
    org_id: str = ""
    workspace_id: str = ""
    key_id: str = ""
    org_tier: str = ""
    created_at: str = ""
    # 注册时顺手存下的控制台会话（JSON，有效期 90 天）。查月度额度用，
    # 有它就不必再拿密码登录；过期会自动用密码换一份新的。
    console_session: str = ""

    enabled: bool = True
    limit_tokens: int = DEFAULT_LIMIT_TOKENS
    remaining_tokens: int = DEFAULT_LIMIT_TOKENS
    limit_req: int = DEFAULT_LIMIT_REQ
    remaining_req: int = DEFAULT_LIMIT_REQ
    window_start: float = field(default_factory=time.time)
    cooldown_until: float = 0.0
    last_used: float = 0.0
    last_status: str = "idle"
    consecutive_errors: int = 0
    last_remaining_after: str = ""
    inflight: int = 0
    # 已派单但尚未回执的预估用量。上游只在响应头里回报余量，并发派单时彼此
    # 看不见对方的消耗，靠这个预留把同一窗口的超额认购挡住。运行时字段，不入库。
    reserved_tokens: int = 0

    # 免费档是每月 10 美元额度而非 token 配额，花光后整号 402、次月 1 号恢复。
    # exhausted_until 是恢复时刻，其余几个是巡检拿到的额度快照。
    budget_used_pct: float = 0.0
    budget_total: float = 0.0
    budget_reset_at: str = ""
    budget_checked_at: float = 0.0
    exhausted_until: float = 0.0

    @classmethod
    def from_record(cls, rec: dict) -> "Account":
        return cls(**{f: (rec.get(f) or "") for f in ACCOUNT_FIELDS})

    def to_record(self) -> dict:
        return {f: getattr(self, f) for f in ACCOUNT_FIELDS}

    def key_preview(self) -> str:
        return f"{self.api_key[:6]}…{self.api_key[-4:]}" if len(self.api_key) > 12 else "—"

    def to_dict(self, now: float | None = None, reveal: bool = False) -> dict:
        now = now or time.time()
        d = {
            "email": self.email,
            "key_preview": self.key_preview(),
            "org_id": self.org_id,
            "org_tier": self.org_tier,
            "enabled": self.enabled,
            "limit_tokens": self.limit_tokens,
            "remaining_tokens": self.remaining_tokens,
            "limit_req": self.limit_req,
            "remaining_req": self.remaining_req,
            "inflight": self.inflight,
            "reserved_tokens": self.reserved_tokens,
            "window_reset_at": self.window_start + WINDOW_SECONDS,
            "window_reset_in": max(0.0, self.window_start + WINDOW_SECONDS - now),
            "cooldown_until": self.cooldown_until,
            "cooling": self.cooldown_until > now,
            "budget_used_pct": round(self.budget_used_pct, 2),
            "budget_total": self.budget_total,
            "budget_reset_at": self.budget_reset_at,
            "budget_checked_at": self.budget_checked_at,
            "exhausted_until": self.exhausted_until,
            "exhausted": self.exhausted_until > now,
            "last_used": self.last_used,
            "last_status": self.last_status,
            "consecutive_errors": self.consecutive_errors,
        }
        if reveal:
            d["api_key"] = self.api_key
        return d


def parse_records(text: str) -> list[dict]:
    """解析粘贴内容: JSON (list/dict) 或 CSV 文本。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return [data] if isinstance(data, dict) else list(data)
    except json.JSONDecodeError:
        return list(csv.DictReader(io.StringIO(text)))


def read_records_file(path: str) -> list[dict]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [data] if isinstance(data, dict) else list(data)


class AccountPool:
    """线程安全的账号池 + 调度器。

    与旧版的差异:
      - 不在 __init__ 里建库/起线程, store 由外部注入 (可为 None, 纯内存模式)
      - 跟踪 inflight, 避免并发下把同一账号的窗口配额超额认购
      - remove_account 写墓碑表, 重启后不会因重新导入 keys 文件而复活
    """

    def __init__(self, store: UsageStore | None = None):
        self._lock = threading.RLock()
        self.accounts: list[Account] = []
        self._rr_index = 0
        self.store = store

    # ---------- 导入 / 持久化 ----------

    def load_from_store(self) -> int:
        """从 DB 恢复账号凭据与运行状态。"""
        if not self.store:
            return 0
        records = self.store.load_account_records()
        added, _ = self.import_records(records, persist=False)
        self.restore_states()
        return added

    def import_records(self, records, persist: bool = True) -> tuple[int, int]:
        """导入账号。已删除(墓碑)的 email 会被跳过。返回 (新增, 更新)。"""
        tombstones = self.store.deleted_emails() if self.store else set()
        added, updated, touched = 0, 0, []
        with self._lock:
            for rec in records or []:
                if not isinstance(rec, dict) or not rec.get("api_key"):
                    continue
                email = (rec.get("email") or "").strip()
                if not email or email in tombstones:
                    continue
                acc = self._find(email)
                if acc:
                    for f in ACCOUNT_FIELDS:
                        val = rec.get(f)
                        if val:
                            setattr(acc, f, val)
                    updated += 1
                else:
                    acc = Account.from_record({**rec, "email": email})
                    self.accounts.append(acc)
                    added += 1
                touched.append(acc.to_record())
        if persist and self.store and touched:
            self.store.save_account_records(touched)
        return added, updated

    def import_content(self, text: str) -> tuple[int, int]:
        return self.import_records(parse_records(text))

    def import_file(self, path: str) -> tuple[int, int]:
        return self.import_records(read_records_file(path))

    def save_states(self) -> None:
        if not self.store:
            return
        with self._lock:
            rows = [(a.email, 1 if a.enabled else 0, a.limit_tokens, a.remaining_tokens,
                     a.limit_req, a.remaining_req, a.window_start, a.cooldown_until,
                     a.last_used, a.last_status, a.consecutive_errors,
                     a.last_remaining_after, a.budget_used_pct, a.budget_total,
                     a.budget_reset_at, a.budget_checked_at, a.exhausted_until)
                    for a in self.accounts]
        self.store.save_states(rows)

    def restore_states(self) -> None:
        if not self.store:
            return
        states = self.store.load_states()
        with self._lock:
            for a in self.accounts:
                r = states.get(a.email)
                if not r:
                    continue
                a.enabled = bool(r["enabled"])
                a.limit_tokens = r["limit_tokens"]
                a.remaining_tokens = r["remaining_tokens"]
                a.limit_req = r["limit_req"]
                a.remaining_req = r["remaining_req"]
                a.window_start = r["window_start"]
                a.cooldown_until = r["cooldown_until"]
                a.last_used = r["last_used"]
                a.last_status = r["last_status"] or "idle"
                a.consecutive_errors = r["consecutive_errors"]
                a.last_remaining_after = r["last_remaining_after"] or ""
                # 老库没这几列, 用 keys() 判断避免 IndexError
                cols = r.keys()
                if "budget_used_pct" in cols:
                    a.budget_used_pct = r["budget_used_pct"] or 0.0
                    a.budget_total = r["budget_total"] or 0.0
                    a.budget_reset_at = r["budget_reset_at"] or ""
                    a.budget_checked_at = r["budget_checked_at"] or 0.0
                    a.exhausted_until = r["exhausted_until"] or 0.0

    # ---------- 查询 ----------

    def _find(self, email: str) -> Account | None:
        for a in self.accounts:
            if a.email == email:
                return a
        return None

    def get_accounts(self, reveal: bool = False) -> list[dict]:
        now = time.time()
        with self._lock:
            return [a.to_dict(now, reveal) for a in self.accounts]

    def summary(self) -> dict:
        now = time.time()
        with self._lock:
            enabled = [a for a in self.accounts if a.enabled]
            live = [a for a in enabled if a.exhausted_until <= now]
            checked = [a for a in enabled if a.budget_checked_at > 0]
            return {
                "total": len(self.accounts),
                "enabled": len(enabled),
                "cooling": sum(1 for a in self.accounts if a.cooldown_until > now),
                "drained": sum(1 for a in enabled if a.remaining_req <= 0),
                "exhausted": len(enabled) - len(live),
                "inflight": sum(a.inflight for a in self.accounts),
                # 额度花光的号一律不计入可用量, 否则会高估池子容量
                "requests_left": sum(a.remaining_req for a in live),
                "tokens_left": sum(a.remaining_tokens for a in live),
                "budget_checked": len(checked),
                "budget_left": round(sum(
                    a.budget_total * (1 - a.budget_used_pct / 100.0) for a in checked), 2),
                "budget_total": round(sum(a.budget_total for a in checked), 2),
            }

    # ---------- 变更 ----------

    def set_enabled(self, email: str, enabled: bool) -> bool:
        with self._lock:
            acc = self._find(email)
            if not acc:
                return False
            acc.enabled = enabled
        self.save_states()
        return True

    def remove_account(self, email: str) -> bool:
        with self._lock:
            for i, a in enumerate(self.accounts):
                if a.email == email:
                    del self.accounts[i]
                    break
            else:
                return False
        if self.store:
            self.store.delete_account(email)
        return True

    # ---------- 调度 ----------

    def _lazy_reset(self, a: Account, now: float) -> None:
        """窗口过期(60s)后惰性恢复额度。"""
        expired = now >= a.window_start + WINDOW_SECONDS
        exhausted = a.remaining_req <= 0 or a.remaining_tokens <= 0 or a.cooldown_until > 0
        if expired and exhausted:
            a.window_start = now
            a.remaining_req = a.limit_req
            a.remaining_tokens = a.limit_tokens
            a.cooldown_until = 0.0

    def pick(self, est_input_tokens: int = 0) -> Account | None:
        """选择一个账号并占用一个 inflight 名额。

        调用方必须在请求结束后调用 release(acc, est_input_tokens), 否则该账号会被
        永久判定为繁忙, 预留额度也不会归还。
        策略: 先 round-robin 找剩余窗口装得下的 (扣除在途预留), 找不到再评分兜底。
        """
        now = time.time()
        est = max(0, int(est_input_tokens))
        with self._lock:
            n = len(self.accounts)
            if n == 0:
                return None
            for a in self.accounts:
                self._lazy_reset(a, now)

            # 请求体积超过任何账号的整窗上限时永远装不下 (比如 1M 上下文对 25 万的
            # 窗口)。这种只让满窗且无在途的账号承接, 把超支限制为每窗口一次 ——
            # 实测反复超支同一个账号会被上游判定异常并停用整个账号, 不只是限速。
            cap = max((a.limit_tokens for a in self.accounts if a.enabled), default=0)
            oversized = est > cap

            chosen = None
            for k in range(n):
                a = self.accounts[(self._rr_index + k) % n]
                if not a.enabled or a.cooldown_until > now or a.exhausted_until > now:
                    continue
                if a.remaining_req - a.inflight <= 0:
                    continue
                if oversized:
                    if a.inflight or a.remaining_tokens < a.limit_tokens:
                        continue
                elif est and a.remaining_tokens - a.reserved_tokens < est:
                    continue
                self._rr_index = (self._rr_index + k + 1) % n
                chosen = a
                break

            if chosen is None:
                # 兜底: 评分最高的 (配额可能已尽但未冷却, 单请求超限也要发出去)
                best, best_score = None, -1.0
                for a in self.accounts:
                    if not a.enabled or a.cooldown_until > now or a.exhausted_until > now:
                        continue
                    free = max(0, a.remaining_tokens - a.reserved_tokens)
                    ts = free / max(a.limit_tokens, 1)
                    rs = a.remaining_req / max(a.limit_req, 1)
                    score = 0.6 * ts + 0.4 * rs
                    if est and free < est:
                        score *= 0.4
                    score /= 1 + a.inflight
                    if score > best_score:
                        best, best_score = a, score
                chosen = best

            if chosen is not None:
                chosen.inflight += 1
                chosen.reserved_tokens += est
                chosen.last_used = now
                chosen.last_status = "busy"
            return chosen

    def release(self, acc: Account | None, est_input_tokens: int = 0) -> None:
        if acc is None:
            return
        with self._lock:
            if acc.inflight > 0:
                acc.inflight -= 1
            acc.reserved_tokens = max(0, acc.reserved_tokens - max(0, int(est_input_tokens)))

    def update_limits(self, acc: Account, headers) -> None:
        """从响应头更新限额 (X-RateLimit-*-minute)。"""
        h = {k.lower(): v for k, v in dict(headers).items()}
        with self._lock:
            for key, attr in (("x-ratelimit-limit-tokens-minute", "limit_tokens"),
                              ("x-ratelimit-remaining-tokens-minute", "remaining_tokens"),
                              ("x-ratelimit-limit-req-minute", "limit_req"),
                              ("x-ratelimit-remaining-req-minute", "remaining_req")):
                raw = h.get(key)
                if raw is None:
                    continue
                try:
                    setattr(acc, attr, int(raw))
                except (TypeError, ValueError):
                    continue
            acc.last_remaining_after = (f"{acc.remaining_tokens}/{acc.limit_tokens} tok, "
                                        f"{acc.remaining_req}/{acc.limit_req} req")

    def mark_success(self, acc: Account) -> None:
        with self._lock:
            acc.last_status = "ok"
            acc.consecutive_errors = 0

    def set_console_session(self, acc: Account, session: str) -> None:
        """密码换来的新会话要落库，否则重启后又得重登一遍。"""
        if not session or session == acc.console_session:
            return
        with self._lock:
            acc.console_session = session
        if self.store:
            self.store.save_account_records([acc.to_record()])

    def mark_exhausted(self, acc: Account, until: float, used_pct: float = 100.0) -> None:
        """月度美元额度花光: 整号所有模型都会 402, 直到下月重置才恢复。"""
        with self._lock:
            acc.exhausted_until = max(acc.exhausted_until, until)
            acc.budget_used_pct = used_pct
            acc.last_status = "budget"

    def update_budget(self, acc: Account, budget, now: float | None = None) -> None:
        """写入一次额度巡检结果; 已耗尽的顺手标记停用到重置时刻。"""
        from .billing import next_reset_ts

        now = now or time.time()
        with self._lock:
            acc.budget_used_pct = budget.used_pct
            acc.budget_total = budget.total
            acc.budget_reset_at = budget.reset_at
            acc.budget_checked_at = now
            if budget.exhausted:
                acc.exhausted_until = max(acc.exhausted_until,
                                          next_reset_ts(budget.reset_at))
            elif acc.exhausted_until > now:
                # 额度已恢复(比如跨月了), 立刻放回池子
                acc.exhausted_until = 0.0

    def mark_error(self, acc: Account, status: int, error: str = "",
                   retry_after: float | None = None) -> None:
        now = time.time()
        with self._lock:
            acc.last_status = f"err:{status}"
            acc.consecutive_errors += 1
            if status == 429:
                # 窗口内额度耗尽 -> 冷却到本窗口结束 (固定 60s 窗口)
                reset_at = acc.window_start + WINDOW_SECONDS
                if retry_after:
                    reset_at = max(reset_at, now + retry_after)
                acc.cooldown_until = max(reset_at, now + 5)
                acc.remaining_req = 0
                acc.remaining_tokens = 0
            else:
                backoff = min(30.0, 5.0 * acc.consecutive_errors)
                acc.cooldown_until = max(acc.cooldown_until, now + backoff)

    def next_window_wait(self) -> float:
        now = time.time()
        with self._lock:
            waits = [a.cooldown_until - now for a in self.accounts
                     if a.enabled and a.cooldown_until > now]
        return max(0.0, min(waits)) if waits else 0.0
