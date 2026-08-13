# -*- coding: utf-8 -*-
"""运行配置。

优先级：命令行参数 > 环境变量 > 配置文件 > 默认值。

配置文件默认读同目录的 config.toml（Python 3.11+ 自带 tomllib，不引额外依赖），
也接受 .json。用 --config 指定别的路径，用 --init-config 生成带注释的模板。
"""
import ipaddress
import json
import os
from dataclasses import dataclass, field, fields

from core.reasoning import MODES as REASONING_MODES
from core.reasoning import REASONING_CONTENT

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

API_BASE = "https://api.mistral.ai/v1"
DEFAULT_KEYS_FILE = os.path.join(BASE_DIR, "mistral_keys.json")
DEFAULT_DB_FILE = os.path.join(BASE_DIR, "proxy_usage.db")
DEFAULT_CONFIG_FILE = os.path.join(BASE_DIR, "config.toml")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 客户端把上一轮思考回传时怎么处理。实测上游对这件事很不一致：
#   glm-5-2 会把 ThinkChunk 和 <think>...</think> 两种形式都在分词前剥掉（prompt_tokens
#   与不传时完全一致），magistral 两种都收，mistral-medium 收到 ThinkChunk 直接 400。
#   而换成任何别的包装（纯文本、自定义标记）三个模型都能正常进上下文。
# text   = 统一改写成不会被剥的纯文本标记，保证多轮里思考真的在上下文里（默认）
# native = 原样透传 ThinkChunk，交给上游自己决定（可能被静默丢弃或 400）
# off    = 丢弃回传的思考，省 token
PASSBACK_MODES = ("text", "native", "off")

# 配置文件里的键名 -> Settings 字段名。取对用户更直观的叫法, 不强迫跟内部字段一致。
CONFIG_ALIASES = {
    "admin_password": "admin_token",
    "admin_token": "admin_token",
    "api_key": "client_api_key",
    "client_api_key": "client_api_key",
    "db": "db_path",
    "db_path": "db_path",
    "keys": "keys_file",
    "keys_file": "keys_file",
}

CONFIG_TEMPLATE = '''# Mistral Pool 2API 配置文件
# 命令行参数会覆盖这里的设置；环境变量 MISTRAL_POOL_ADMIN_TOKEN / MISTRAL_POOL_API_KEY 介于两者之间。
# 改完重启生效。本文件含明文密码，已被 .gitignore 排除，注意别外传。

# ---------- 监听 ----------
host = "127.0.0.1"        # 要让局域网/公网访问改成 "0.0.0.0"
port = 8787

# ---------- 管理台登录 ----------
# 自己指定管理密码。留空或删掉这一行 = 首次启动随机生成一个并打印在控制台，
# 之后可以在管理台「设置」页自行修改。
admin_password = ""

# auth_enabled = false    # 彻底关掉管理台登录，仅限本机调试

# ---------- 调用方鉴权 ----------
# 固定的下游密钥。更推荐留空，改在管理台「访问令牌」页签发可撤销、可限额的令牌。
api_key = ""

# ---------- 网关行为 ----------
max_concurrency = 32              # 同时发往上游的请求数上限
max_retry_accounts = 4            # 遇到 429/5xx 时最多换几个上游账号
reasoning_format = "reasoning_content"
# reasoning_content = 兼容最广（DeepSeek / OpenRouter 系客户端）
# think_tags        = 思考内联成 <think>...</think>
# passthrough       = 上游原始 chunk 数组
# strip             = 丢弃思考内容

reasoning_passback = "text"       # 客户端回传上一轮思考时怎么办
# text   = 改写成不会被上游剥掉的标记，多轮里思考真的进上下文（默认）
# native = 原样透传，由上游决定（glm 会静默丢弃，mistral-medium 会 400）
# off    = 直接丢弃，省 token

# ---------- 额度巡检 ----------
# 免费档发的是每月 10 美元 API 额度（不是 token 配额），花光后整号 402、次月 1 号重置。
# 这个数字 API key 读不到，走控制台会话查：注册脚本存下来的会话有效期 90 天，
# 正常情况下一次登录都不用做，单次查询约 0.5 秒。只查「用过且很久没查」的账号。
budget_check = true
budget_check_interval = 30        # 两次查询之间隔多少秒
budget_stale_hours = 12           # 距上次查询超过这么久就重新查

# ---------- 存储 ----------
db = "proxy_usage.db"
keys_file = "mistral_keys.json"   # 启动时导入的上游账号文件，设为 "" 表示只用数据库

# ---------- 超时（秒） ----------
connect_timeout = 30
read_timeout = 900
'''


class ConfigError(Exception):
    pass


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    keys_file: str | None = None
    db_path: str = DEFAULT_DB_FILE

    admin_token: str = ""
    client_api_key: str = ""

    max_concurrency: int = 32
    max_retry_accounts: int = 4
    max_body_bytes: int = 16 * 1024 * 1024
    reasoning_format: str = REASONING_CONTENT
    reasoning_passback: str = "text"

    connect_timeout: float = 30.0
    read_timeout: float = 900.0
    state_save_interval: float = 3.0
    model_sync_interval: float = 1800.0

    # 额度巡检：免费档发的是每月美元额度，只能登控制台查，一次约 2.5 秒 370KB。
    # 因此只查「用过且很久没查」的账号，闲置账号不占任何开销。
    budget_check: bool = True
    budget_check_interval: float = 30.0    # 两次查询之间的间隔
    budget_stale_hours: float = 12.0       # 超过这么久没查就算过期

    auth_enabled: bool = True
    allow_insecure: bool = False
    set_password_mode: bool = False   # --set-password: 改完密码就退出, 不起服务
    config_file: str = ""             # 实际生效的配置文件路径, 空表示没用
    warnings: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.reasoning_format not in REASONING_MODES:
            raise ConfigError(
                f"reasoning_format 必须是 {list(REASONING_MODES)} 之一，"
                f"当前是 {self.reasoning_format!r}")
        if self.reasoning_passback not in PASSBACK_MODES:
            raise ConfigError(
                f"reasoning_passback 必须是 {list(PASSBACK_MODES)} 之一，"
                f"当前是 {self.reasoning_passback!r}")
        if self.max_concurrency < 1:
            raise ConfigError("max_concurrency 至少为 1")
        if self.max_retry_accounts < 1:
            raise ConfigError("max_retry_accounts 至少为 1")
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"port 越界: {self.port}")

        # 默认强制登录：没指定密码就自动生成一个存库，不存在裸奔的默认路径。
        if not self.auth_enabled:
            if _is_loopback(self.host):
                self.warnings.append("管理台登录已关闭, 本机任何程序都能访问管理接口。")
            elif self.allow_insecure:
                self.warnings.append(
                    f"危险: 绑定 {self.host} 且关闭了登录, 管理台(含账号池)对全网开放。")
            else:
                raise ConfigError(
                    f"绑定非回环地址 {self.host} 时不允许关闭登录。"
                    f"确实要裸奔请加 --allow-insecure。")

    def warn_if_open_proxy(self, auth_required: bool) -> None:
        if not auth_required:
            scope = "本机" if _is_loopback(self.host) else "全网"
            self.warnings.append(
                f"/v1/* 未启用调用方鉴权, 账号池对{scope}开放。"
                f"在管理台签发令牌, 或在配置文件里设 api_key。")


# ---------- 配置文件 ----------

def load_config_file(path: str) -> dict:
    """读配置文件, 返回以 Settings 字段名为键的字典。未知键会报错以便发现拼写错误。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".json":
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            import tomllib
            with open(path, "rb") as f:
                raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {path}")
    except (json.JSONDecodeError, ValueError) as e:
        raise ConfigError(f"配置文件 {path} 解析失败: {e}")

    if not isinstance(raw, dict):
        raw = {}

    known = {f.name for f in fields(Settings)}
    internal = {"warnings", "config_file", "set_password_mode"}
    out: dict = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            raise ConfigError(
                f"配置文件 {path} 里的 [{key}] 是分节写法，本项目用扁平结构，"
                f"直接写 {key} = ... 即可")
        target = CONFIG_ALIASES.get(key, key)
        if target not in known or target in internal:
            raise ConfigError(
                f"配置文件 {path} 里有无法识别的项 {key!r}。"
                f"可用项见 --init-config 生成的模板。")
        out[target] = value

    return out


def resolve_keys_file(settings: Settings) -> None:
    """keys_file 的三态：None=没配过(自动找默认文件)，""=显式关闭，其他=指定路径。"""
    if settings.keys_file is None:
        if os.path.exists(DEFAULT_KEYS_FILE):
            settings.keys_file = DEFAULT_KEYS_FILE
    elif not settings.keys_file.strip():
        settings.keys_file = None


def write_config_template(path: str) -> None:
    if os.path.exists(path):
        raise ConfigError(f"{path} 已存在，不覆盖")
    with open(path, "w", encoding="utf-8") as f:
        f.write(CONFIG_TEMPLATE)


def build_settings(file_values: dict | None = None, cli_values: dict | None = None,
                   env: dict | None = None) -> Settings:
    """按 命令行 > 环境变量 > 配置文件 > 默认值 的顺序合并。

    命令行那一层只传显式给了的项（argparse 里没给的一律是 None），所以这里不用
    区分"用户传了默认值"和"用户没传"。
    """
    env = os.environ if env is None else env
    settings = Settings()

    for source in (file_values or {},
                   {"admin_token": env.get("MISTRAL_POOL_ADMIN_TOKEN"),
                    "client_api_key": env.get("MISTRAL_POOL_API_KEY")},
                   cli_values or {}):
        for key, value in source.items():
            if value is not None and hasattr(settings, key):
                setattr(settings, key, value)
    return settings
