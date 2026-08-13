# -*- coding: utf-8 -*-
"""下游客户端密钥：签发、校验、限速、配额、模型白名单。

密钥只在签发那一刻返回明文，库里存 SHA-256，因此数据库泄露不等于密钥泄露。
列表里靠 prefix（前 12 位）辨认是哪一把。
"""
import hashlib
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

KEY_PREFIX = "sk-pool-"
PREVIEW_LEN = 12


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass(slots=True)
class ClientKey:
    id: str
    name: str = ""
    key_hash: str = ""
    prefix: str = ""
    enabled: bool = True
    created_at: float = 0.0
    expires_at: float = 0.0
    rpm_limit: int = 0            # 0 = 不限
    daily_token_limit: int = 0    # 0 = 不限
    allowed_models: list = field(default_factory=list)  # 空 = 全部允许
    total_requests: int = 0
    total_tokens: int = 0
    last_used: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "prefix": self.prefix,
            "enabled": self.enabled, "created_at": self.created_at,
            "expires_at": self.expires_at, "rpm_limit": self.rpm_limit,
            "daily_token_limit": self.daily_token_limit,
            "allowed_models": self.allowed_models,
            "total_requests": self.total_requests, "total_tokens": self.total_tokens,
            "last_used": self.last_used,
            "expired": bool(self.expires_at and self.expires_at < time.time()),
        }


class QuotaError(Exception):
    def __init__(self, message: str, status: int = 429, err_type: str = "rate_limit_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = err_type


class ClientKeyStore:
    def __init__(self, store=None):
        self.store = store
        self.keys: dict[str, ClientKey] = {}
        self._by_hash: dict[str, str] = {}
        self._recent: dict[str, deque] = defaultdict(deque)
        self._static_key_hash = ""

    def set_static_key(self, raw: str) -> None:
        """--api-key 传进来的固定密钥, 不入库, 权限等同无限制。"""
        self._static_key_hash = hash_key(raw) if raw else ""

    def load(self) -> None:
        if not self.store:
            return
        self.keys = {k.id: k for k in self.store.load_client_keys()}
        self._by_hash = {k.key_hash: k.id for k in self.keys.values()}

    @property
    def auth_required(self) -> bool:
        return bool(self._static_key_hash) or any(k.enabled for k in self.keys.values())

    # ---------- 签发 / 管理 ----------

    def create(self, name: str = "", rpm_limit: int = 0, daily_token_limit: int = 0,
               allowed_models=None, ttl_days: int = 0) -> tuple[ClientKey, str]:
        raw = generate_key()
        key = ClientKey(
            id=secrets.token_hex(8), name=name or "unnamed", key_hash=hash_key(raw),
            prefix=raw[:PREVIEW_LEN], created_at=time.time(),
            expires_at=time.time() + ttl_days * 86400 if ttl_days else 0.0,
            rpm_limit=max(0, int(rpm_limit)),
            daily_token_limit=max(0, int(daily_token_limit)),
            allowed_models=list(allowed_models or []))
        self.keys[key.id] = key
        self._by_hash[key.key_hash] = key.id
        if self.store:
            self.store.save_client_key(key)
        return key, raw

    def update(self, key_id: str, **fields) -> bool:
        key = self.keys.get(key_id)
        if not key:
            return False
        for name in ("name", "enabled", "rpm_limit", "daily_token_limit",
                     "allowed_models", "expires_at"):
            if name in fields and fields[name] is not None:
                setattr(key, name, fields[name])
        if self.store:
            self.store.save_client_key(key)
        return True

    def revoke(self, key_id: str) -> bool:
        key = self.keys.pop(key_id, None)
        if not key:
            return False
        self._by_hash.pop(key.key_hash, None)
        self._recent.pop(key_id, None)
        if self.store:
            self.store.delete_client_key(key_id)
        return True

    def list(self) -> list[dict]:
        today = self.today_usage()
        out = []
        for key in sorted(self.keys.values(), key=lambda k: -k.created_at):
            item = key.to_dict()
            item["today"] = today.get(key.id, {"requests": 0, "tokens": 0})
            out.append(item)
        return out

    def today_usage(self) -> dict:
        return self.store.client_key_usage_today() if self.store else {}

    # ---------- 校验 ----------

    def verify(self, raw: str) -> ClientKey | None:
        """返回匹配的 ClientKey；固定密钥返回哨兵对象；不匹配返回 None。"""
        if not raw:
            return None
        digest = hash_key(raw)
        if self._static_key_hash and secrets.compare_digest(digest, self._static_key_hash):
            return ClientKey(id="static", name="static (--api-key)", enabled=True)
        key_id = self._by_hash.get(digest)
        return self.keys.get(key_id) if key_id else None

    def check(self, key: ClientKey, model: str) -> None:
        """检查启用状态 / 有效期 / 模型白名单 / RPM / 日 token 配额, 不通过则抛 QuotaError。"""
        now = time.time()
        if not key.enabled:
            raise QuotaError("This API key has been disabled", 403, "permission_error")
        if key.expires_at and key.expires_at < now:
            raise QuotaError("This API key has expired", 403, "permission_error")
        if key.allowed_models and model and model not in key.allowed_models:
            raise QuotaError(
                f"Model '{model}' is not allowed for this API key "
                f"(allowed: {', '.join(key.allowed_models)})", 403, "permission_error")

        if key.rpm_limit:
            window = self._recent[key.id]
            cutoff = now - 60
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= key.rpm_limit:
                raise QuotaError(
                    f"Rate limit reached for this API key: {key.rpm_limit} requests/min")
            window.append(now)

        if key.daily_token_limit:
            used = self.today_usage().get(key.id, {}).get("tokens", 0)
            if used >= key.daily_token_limit:
                raise QuotaError(
                    f"Daily token quota exhausted: {used}/{key.daily_token_limit}")

    def note_usage(self, key: ClientKey, tokens: int) -> None:
        if key.id == "static":
            return
        key.total_requests += 1
        key.total_tokens += max(0, tokens)
        key.last_used = time.time()
