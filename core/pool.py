# -*- coding: utf-8 -*-
"""Mistral 多账号池: 账号管理 + 限流感知调度。

架构: 一个 Account(email) 下挂多个 Org，每个 Org 有独立的 org_id/api_key/额度。
pick() 选的是 Org，限速/额度/inflight 状态全在 Org 上。
额度耗尽时删旧组织→建新组织→新 Org 挂到同一 Account 下。

本模块不做任何网络与 Web 框架相关的事, 便于单测。
"""
import csv
import hashlib
import io
import json
import os
import threading
import time
from dataclasses import dataclass, field

from .store import (ACCOUNT_FIELDS, ORG_FIELDS, ORG_STATE_FIELDS, UsageStore)

WINDOW_SECONDS = 60.0
DEFAULT_LIMIT_TOKENS = 50_000
DEFAULT_LIMIT_REQ = 50


def est_tokens(messages) -> int:
    """粗略估算输入 tokens, 仅用于账号选择。"""
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
class Org:
    """一个组织: 独立的 org_id/api_key/额度/限速状态。"""
    email: str = ""               # 反向引用所属 Account 的 email
    org_id: str = ""
    workspace_id: str = ""
    key_id: str = ""
    api_key: str = ""
    org_tier: str = ""
    created_at: str = ""

    # 运行时限速状态
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
    reserved_tokens: int = 0

    # 额度状态
    budget_used_pct: float = 0.0
    budget_total: float = 0.0
    budget_reset_at: str = ""
    budget_checked_at: float = 0.0
    exhausted_until: float = 0.0

    @property
    def uid(self) -> str:
        """稳定的 UI 标识符（api_key 的 hash）。"""
        return hashlib.sha256(self.api_key.encode()).hexdigest()[:16] if self.api_key else ""

    def key_preview(self) -> str:
        return f"{self.api_key[:6]}…{self.api_key[-4:]}" if len(self.api_key) > 12 else "—"

    def to_org_record(self) -> dict:
        return {f: getattr(self, f) for f in ORG_FIELDS}

    def to_dict(self, now: float | None = None, reveal: bool = False) -> dict:
        now = now or time.time()
        d = {
            "email": self.email,
            "uid": self.uid,
            "key_preview": self.key_preview(),
            "org_id": self.org_id,
            "org_tier": self.org_tier,
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


@dataclass(slots=True)
class Account:
    """一个 Mistral 账号(email)，下挂多个 Org。"""
    email: str = ""
    email_password: str = ""
    mistral_password: str = ""
    console_session: str = ""
    enabled: bool = True
    orgs: list = field(default_factory=list)

    @classmethod
    def from_record(cls, rec: dict) -> "Account":
        return cls(**{f: (rec.get(f) or "") for f in ACCOUNT_FIELDS})

    def to_record(self) -> dict:
        return {f: getattr(self, f) for f in ACCOUNT_FIELDS}

    def to_dict(self, now: float | None = None, reveal: bool = False) -> dict:
        return {
            "email": self.email,
            "enabled": self.enabled,
            "has_session": bool(self.console_session),
            "has_password": bool(self.mistral_password),
            "orgs": [o.to_dict(now, reveal) for o in self.orgs],
            "org_count": len(self.orgs),
        }


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
    """线程安全的账号池 + 调度器。"""

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
        # 加载账号级别凭据
        for rec in self.store.load_account_records():
            acc = Account.from_record(rec)
            self.accounts.append(acc)
        # 加载组织级别凭据
        org_recs = self.store.load_org_records()
        self._merge_org_records(org_recs, persist=False)
        self.restore_states()
        return len(self.accounts)

    def _merge_org_records(self, org_recs: list[dict], persist: bool = True) -> int:
        """把组织凭据合并到对应的 Account 下。"""
        added = 0
        touched = []
        with self._lock:
            for rec in org_recs:
                email = (rec.get("email") or "").strip()
                org_id = (rec.get("org_id") or "").strip()
                api_key = (rec.get("api_key") or "").strip()
                if not email or not org_id or not api_key:
                    continue
                acc = self._find(email)
                if not acc:
                    continue
                # 按 org_id 去重
                existing = next((o for o in acc.orgs if o.org_id == org_id), None)
                if existing:
                    for f in ORG_FIELDS:
                        val = rec.get(f)
                        if val:
                            setattr(existing, f, val)
                else:
                    org = Org(
                        email=email,
                        org_id=org_id,
                        workspace_id=rec.get("workspace_id") or "",
                        key_id=rec.get("key_id") or "",
                        api_key=api_key,
                        org_tier=rec.get("org_tier") or "",
                        created_at=rec.get("created_at") or "",
                    )
                    acc.orgs.append(org)
                    added += 1
                touched.append(rec)
        if persist and self.store and touched:
            self.store.save_org_records(touched)
        return added

    def import_records(self, records, persist: bool = True) -> tuple[int, int]:
        """导入账号。同 email 的多条 record 合并为一个 Account + 多个 Org。
        兼容旧格式（每条 record 含 email + api_key + org_id 等）。"""
        tombstone_emails = self.store.deleted_emails() if self.store else set()
        tombstone_orgs = self.store.deleted_org_keys() if self.store else set()
        added, updated = 0, 0
        acc_touched = []
        org_recs = []
        with self._lock:
            for rec in records or []:
                if not isinstance(rec, dict):
                    continue
                email = (rec.get("email") or "").strip()
                api_key = (rec.get("api_key") or "").strip()
                org_id = (rec.get("org_id") or "").strip()
                if not email or email in tombstone_emails:
                    continue
                # 账号级别
                acc = self._find(email)
                if acc:
                    for f in ACCOUNT_FIELDS:
                        val = rec.get(f)
                        if val:
                            setattr(acc, f, val)
                    updated += 1
                else:
                    acc = Account.from_record(rec)
                    self.accounts.append(acc)
                    added += 1
                acc_touched.append(acc.to_record())
                # 组织级别
                if api_key and org_id:
                    org_key = f"{email}\x00{org_id}"
                    if org_key in tombstone_orgs:
                        continue
                    existing = next((o for o in acc.orgs if o.org_id == org_id), None)
                    if existing:
                        for f in ORG_FIELDS:
                            val = rec.get(f)
                            if val:
                                setattr(existing, f, val)
                    else:
                        org = Org(
                            email=email, org_id=org_id,
                            workspace_id=rec.get("workspace_id") or "",
                            key_id=rec.get("key_id") or "",
                            api_key=api_key,
                            org_tier=rec.get("org_tier") or "",
                            created_at=rec.get("created_at") or "",
                        )
                        acc.orgs.append(org)
                    org_recs.append({f: rec.get(f, "") for f in ORG_FIELDS})
        if persist and self.store:
            if acc_touched:
                self.store.save_account_records(acc_touched)
            if org_recs:
                self.store.save_org_records(org_recs)
        return added, updated

    def import_content(self, text: str) -> tuple[int, int]:
        return self.import_records(parse_records(text))

    def import_file(self, path: str) -> tuple[int, int]:
        return self.import_records(read_records_file(path))

    def save_states(self) -> None:
        if not self.store:
            return
        with self._lock:
            rows = []
            for acc in self.accounts:
                for org in acc.orgs:
                    rows.append((acc.email, org.org_id, 1 if acc.enabled else 0,
                                 org.limit_tokens, org.remaining_tokens,
                                 org.limit_req, org.remaining_req, org.window_start,
                                 org.cooldown_until, org.last_used, org.last_status,
                                 org.consecutive_errors, org.last_remaining_after,
                                 org.budget_used_pct, org.budget_total,
                                 org.budget_reset_at, org.budget_checked_at,
                                 org.exhausted_until))
        self.store.save_org_states(rows)

    def restore_states(self) -> None:
        if not self.store:
            return
        states = self.store.load_org_states()
        with self._lock:
            for acc in self.accounts:
                for org in acc.orgs:
                    r = states.get(f"{acc.email}\x00{org.org_id}")
                    if not r:
                        continue
                    acc.enabled = bool(r["enabled"])
                    org.limit_tokens = r["limit_tokens"]
                    org.remaining_tokens = r["remaining_tokens"]
                    org.limit_req = r["limit_req"]
                    org.remaining_req = r["remaining_req"]
                    org.window_start = r["window_start"]
                    org.cooldown_until = r["cooldown_until"]
                    org.last_used = r["last_used"]
                    org.last_status = r["last_status"] or "idle"
                    org.consecutive_errors = r["consecutive_errors"]
                    org.last_remaining_after = r["last_remaining_after"] or ""
                    cols = r.keys()
                    if "budget_used_pct" in cols:
                        org.budget_used_pct = r["budget_used_pct"] or 0.0
                        org.budget_total = r["budget_total"] or 0.0
                        org.budget_reset_at = r["budget_reset_at"] or ""
                        org.budget_checked_at = r["budget_checked_at"] or 0.0
                        org.exhausted_until = r["exhausted_until"] or 0.0

    # ---------- 查询 ----------

    def _find(self, email: str) -> Account | None:
        for a in self.accounts:
            if a.email == email:
                return a
        return None

    def _find_org_by_uid(self, uid: str) -> Org | None:
        for acc in self.accounts:
            for org in acc.orgs:
                if org.uid == uid:
                    return org
        return None

    def _all_orgs(self) -> list[Org]:
        """返回所有 enabled 账号下的 org（平铺）。"""
        return [org for acc in self.accounts if acc.enabled for org in acc.orgs]

    def get_accounts(self, reveal: bool = False) -> list[dict]:
        now = time.time()
        with self._lock:
            return [a.to_dict(now, reveal) for a in self.accounts]

    def summary(self) -> dict:
        now = time.time()
        with self._lock:
            all_orgs = self._all_orgs()
            live = [o for o in all_orgs if o.exhausted_until <= now]
            checked = [o for o in all_orgs if o.budget_checked_at > 0]
            return {
                "total": len(self.accounts),
                "enabled": len([a for a in self.accounts if a.enabled]),
                "orgs": len(all_orgs),
                "cooling": sum(1 for o in all_orgs if o.cooldown_until > now),
                "drained": sum(1 for o in all_orgs if o.remaining_req <= 0),
                "exhausted": len(all_orgs) - len(live),
                "inflight": sum(o.inflight for o in all_orgs),
                "requests_left": sum(o.remaining_req for o in live),
                "tokens_left": sum(o.remaining_tokens for o in live),
                "budget_checked": len(checked),
                "budget_left": round(sum(
                    o.budget_total * (1 - o.budget_used_pct / 100.0) for o in checked), 2),
                "budget_total": round(sum(o.budget_total for o in checked), 2),
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

    def remove_org(self, uid: str) -> bool:
        with self._lock:
            for acc in self.accounts:
                for i, org in enumerate(acc.orgs):
                    if org.uid == uid:
                        del acc.orgs[i]
                        if self.store:
                            self.store.delete_org(acc.email, org.org_id)
                        return True
        return False

    # ---------- 调度 ----------

    def _lazy_reset(self, org: Org, now: float) -> None:
        """窗口过期(60s)后惰性恢复额度。"""
        expired = now >= org.window_start + WINDOW_SECONDS
        exhausted = (org.remaining_req <= 0 or org.remaining_tokens <= 0
                     or org.cooldown_until > 0)
        if expired and exhausted:
            org.window_start = now
            org.remaining_req = org.limit_req
            org.remaining_tokens = org.limit_tokens
            org.cooldown_until = 0.0

    def pick(self, est_input_tokens: int = 0, *, ignore_req_limit: bool = False) -> Org | None:
        """选择一个 Org 并占用一个 inflight 名额。"""
        now = time.time()
        est = max(0, int(est_input_tokens))
        with self._lock:
            orgs = self._all_orgs()
            n = len(orgs)
            if n == 0:
                return None
            for o in orgs:
                self._lazy_reset(o, now)

            cap = max((o.limit_tokens for o in orgs), default=0)
            oversized = est > cap

            chosen = None
            for k in range(n):
                o = orgs[(self._rr_index + k) % n]
                if o.cooldown_until > now or o.exhausted_until > now:
                    continue
                if not ignore_req_limit and o.remaining_req - o.inflight <= 0:
                    continue
                if oversized:
                    if o.inflight or o.remaining_tokens < o.limit_tokens:
                        continue
                elif est and o.remaining_tokens - o.reserved_tokens < est:
                    continue
                self._rr_index = (self._rr_index + k + 1) % n
                chosen = o
                break

            if chosen is None:
                best, best_score = None, -1.0
                for o in orgs:
                    if o.cooldown_until > now or o.exhausted_until > now:
                        continue
                    free = max(0, o.remaining_tokens - o.reserved_tokens)
                    ts = free / max(o.limit_tokens, 1)
                    rs = o.remaining_req / max(o.limit_req, 1)
                    score = 0.6 * ts + 0.4 * rs
                    if est and free < est:
                        score *= 0.4
                    score /= 1 + o.inflight
                    if score > best_score:
                        best, best_score = o, score
                chosen = best

            if chosen is not None:
                chosen.inflight += 1
                chosen.reserved_tokens += est
                chosen.last_used = now
                chosen.last_status = "busy"
            return chosen

    def release(self, org: Org | None, est_input_tokens: int = 0) -> None:
        if org is None:
            return
        with self._lock:
            if org.inflight > 0:
                org.inflight -= 1
            org.reserved_tokens = max(0, org.reserved_tokens - max(0, int(est_input_tokens)))

    def update_limits(self, org: Org, headers) -> None:
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
                    setattr(org, attr, int(raw))
                except (TypeError, ValueError):
                    continue
            org.last_remaining_after = (f"{org.remaining_tokens}/{org.limit_tokens} tok, "
                                        f"{org.remaining_req}/{org.limit_req} req")

    def mark_success(self, org: Org) -> None:
        with self._lock:
            org.last_status = "ok"
            org.consecutive_errors = 0

    def set_console_session(self, acc: Account, session: str) -> None:
        """console_session 是账号级别的，更新时同步到同 email 的所有 Org。"""
        if not session or session == acc.console_session:
            return
        with self._lock:
            acc.console_session = session
        if self.store:
            self.store.save_account_records([acc.to_record()])

    def add_org(self, acc: Account, result) -> Org | None:
        """把 rebuild 结果作为新 Org 加到 Account.orgs。
        旧 Org 保持 exhausted 状态，新 Org 立即可用。"""
        if not result.ok:
            return None
        with self._lock:
            # 幂等：如果 org_id 已存在就不重复加
            existing = next((o for o in acc.orgs if o.org_id == result.org_id), None)
            if existing:
                return existing
            org = Org(
                email=acc.email,
                org_id=result.org_id,
                workspace_id=result.workspace_id,
                key_id=result.key_id,
                api_key=result.api_key,
                org_tier="",  # 新组织 tier 未知，巡检时补
                created_at="",
                last_status="rebuilt",
            )
            acc.orgs.append(org)
            if result.session:
                acc.console_session = result.session
            org_rec = org.to_org_record()
            acc_rec = acc.to_record()
        if self.store:
            self.store.save_org_records([org_rec])
            self.store.save_account_records([acc_rec])
        return org

    def mark_exhausted(self, org: Org, until: float, used_pct: float = 100.0) -> None:
        with self._lock:
            org.exhausted_until = max(org.exhausted_until, until)
            org.budget_used_pct = used_pct
            org.last_status = "budget"

    def update_budget(self, org: Org, budget, now: float | None = None) -> None:
        from .billing import next_reset_ts
        now = now or time.time()
        with self._lock:
            org.budget_used_pct = budget.used_pct
            org.budget_total = budget.total
            org.budget_reset_at = budget.reset_at
            org.budget_checked_at = now
            if budget.exhausted:
                org.exhausted_until = max(org.exhausted_until,
                                          next_reset_ts(budget.reset_at))
            elif org.exhausted_until > now:
                org.exhausted_until = 0.0

    def mark_error(self, org: Org, status: int, error: str = "",
                   retry_after: float | None = None) -> None:
        now = time.time()
        with self._lock:
            org.last_status = f"err:{status}"
            org.consecutive_errors += 1
            if status == 429:
                reset_at = org.window_start + WINDOW_SECONDS
                if retry_after:
                    reset_at = max(reset_at, now + retry_after)
                org.cooldown_until = max(reset_at, now + 5)
                org.remaining_req = 0
                org.remaining_tokens = 0
            else:
                backoff = min(30.0, 5.0 * org.consecutive_errors)
                org.cooldown_until = max(org.cooldown_until, now + backoff)

    def next_window_wait(self) -> float:
        now = time.time()
        with self._lock:
            waits = [o.cooldown_until - now for o in self._all_orgs()
                     if o.cooldown_until > now]
        return max(0.0, min(waits)) if waits else 0.0
