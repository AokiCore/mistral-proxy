# -*- coding: utf-8 -*-
"""管理端登录：密码存储与会话签发。

密码
  --admin-token 给了就用它（进程内固定，不落库）；没给就从库里读，库里也没有就随机生成一个
  并把 PBKDF2 散列写进库、明文只在控制台打印一次。任何情况下库里都没有明文。

会话
  签名 Cookie，不存服务端会话表：payload 是过期时间，签名密钥 = HMAC(salt, 密码散列)。
  这样改密码或轮换 salt 都会让已签发的 Cookie 立刻全部失效，"登出所有设备"就是轮换 salt。
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

SESSION_COOKIE = "mp_session"
DEFAULT_TTL_DAYS = 7
PBKDF2_ROUNDS = 200_000

LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW = 900.0

META_PASSWORD = "admin_password"
META_SALT = "session_salt"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), PBKDF2_ROUNDS)
    return digest.hex()


class AuthManager:
    """管理端认证。auth_enabled 为 False 时全部放行（显式 --no-auth 才会出现）。"""

    def __init__(self, store=None, admin_token: str = "", enabled: bool = True):
        self.store = store
        self.enabled = enabled
        self._fixed_token = admin_token or ""
        self._salt = ""
        self._password_salt = ""
        self._password_hash = ""
        self._is_generated = False
        self.generated_password = ""
        self.fixed_source = "--admin-token"  # 启动流程会改成"配置文件 xxx"之类

    # ---------- 初始化 ----------

    def load(self) -> None:
        """读取或初始化密码与会话 salt。返回后 generated_password 非空表示这次新生成了密码。"""
        if not self.store:
            return
        self._salt = self.store.get_meta(META_SALT) or ""
        if not self._salt:
            self._salt = secrets.token_hex(16)
            self.store.set_meta(META_SALT, self._salt)

        if self._fixed_token:
            return  # 命令行密码优先，不碰库里的

        raw = self.store.get_meta(META_PASSWORD)
        if raw:
            try:
                record = json.loads(raw)
                self._password_salt = record["salt"]
                self._password_hash = record["hash"]
                self._is_generated = bool(record.get("generated"))
                return
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        if self.enabled:
            self.generated_password = self._readable_password()
            self.set_password(self.generated_password, generated=True)

    @staticmethod
    def _readable_password() -> str:
        alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
        return "-".join("".join(secrets.choice(alphabet) for _ in range(4))
                        for _ in range(3))

    def set_password(self, password: str, generated: bool = False) -> None:
        """设置密码。generated=True 表示这是系统随机生成的, UI 会提示用户改掉。"""
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        self._password_salt = secrets.token_hex(16)
        self._password_hash = hash_password(password, self._password_salt)
        self._is_generated = generated
        if self.store:
            self.store.set_meta(META_PASSWORD, json.dumps(
                {"salt": self._password_salt, "hash": self._password_hash,
                 "generated": generated}))
        self.rotate_sessions()  # 改密码后旧 Cookie 立即失效

    @property
    def password_source(self) -> str:
        return "fixed" if self._fixed_token else "database"

    @property
    def password_source_label(self) -> str:
        """给界面看的人话。fixed_source 由启动流程填成具体来源。"""
        return self.fixed_source if self._fixed_token else "数据库（可在设置页修改）"

    @property
    def using_generated_password(self) -> bool:
        """当前仍在用系统随机生成的密码（没被用户改过）。"""
        return self.enabled and not self._fixed_token and self._is_generated

    # ---------- 校验 ----------

    def verify_password(self, supplied: str) -> bool:
        if not self.enabled:
            return True
        if not supplied:
            return False
        if self._fixed_token:
            return hmac.compare_digest(supplied, self._fixed_token)
        if not self._password_hash:
            return False
        return hmac.compare_digest(
            hash_password(supplied, self._password_salt), self._password_hash)

    # ---------- 会话 ----------

    def _signing_key(self) -> bytes:
        base = self._fixed_token or self._password_hash
        return hashlib.sha256((self._salt + "|" + base).encode("utf-8")).digest()

    def rotate_sessions(self) -> None:
        self._salt = secrets.token_hex(16)
        if self.store:
            self.store.set_meta(META_SALT, self._salt)

    def issue_session(self, ttl_days: int = DEFAULT_TTL_DAYS) -> tuple[str, int]:
        max_age = int(ttl_days * 86400)
        payload = _b64e(json.dumps({"exp": int(time.time()) + max_age}).encode("utf-8"))
        signature = hmac.new(self._signing_key(), payload.encode("ascii"),
                             hashlib.sha256).hexdigest()
        return f"{payload}.{signature}", max_age

    def verify_session(self, cookie: str) -> bool:
        if not self.enabled:
            return True
        if not cookie or "." not in cookie:
            return False
        payload, _, signature = cookie.rpartition(".")
        expected = hmac.new(self._signing_key(), payload.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            return json.loads(_b64d(payload))["exp"] > time.time()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return False


class LoginThrottle:
    """登录暴力破解节流：同一来源 IP 在窗口内失败次数超限则锁定到最早一次失败过期。

    每个应用上下文持有一个实例——挂在模块级会让多个 app 实例（尤其是测试）
    互相污染彼此的失败计数。"""

    def __init__(self, limit: int = LOGIN_FAIL_LIMIT, window: float = LOGIN_FAIL_WINDOW):
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque] = defaultdict(deque)

    def locked_for(self, ip: str) -> int:
        """返回剩余锁定秒数，0 表示放行。顺手清掉窗口外的旧记录。"""
        failures = self._failures[ip]
        cutoff = time.time() - self.window
        while failures and failures[0] < cutoff:
            failures.popleft()
        if len(failures) < self.limit:
            return 0
        return int(failures[0] + self.window - time.time()) + 1

    def note_failure(self, ip: str) -> None:
        self._failures[ip].append(time.time())

    def failure_count(self, ip: str) -> int:
        return len(self._failures.get(ip, ()))

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)
