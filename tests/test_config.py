# -*- coding: utf-8 -*-
"""配置文件加载与 命令行 > 环境变量 > 配置文件 > 默认值 的优先级。"""
import json

import pytest

import app as app_module
from core.config import (ConfigError, Settings, build_settings, load_config_file,
                         resolve_keys_file, write_config_template)


def write(tmp_path, text, name="config.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------- 解析 ----------

def test_toml_keys_map_to_settings_fields(tmp_path):
    path = write(tmp_path, '''
host = "0.0.0.0"
port = 9000
admin_password = "my-secret"
api_key = "sk-fixed"
db = "/tmp/x.db"
keys_file = "/tmp/keys.json"
max_concurrency = 64
reasoning_format = "think_tags"
''')
    values = load_config_file(path)
    assert values == {
        "host": "0.0.0.0", "port": 9000, "admin_token": "my-secret",
        "client_api_key": "sk-fixed", "db_path": "/tmp/x.db",
        "keys_file": "/tmp/keys.json", "max_concurrency": 64,
        "reasoning_format": "think_tags",
    }


def test_internal_field_names_also_accepted(tmp_path):
    path = write(tmp_path, 'admin_token = "a"\nclient_api_key = "b"\ndb_path = "c"')
    assert load_config_file(path) == {"admin_token": "a", "client_api_key": "b",
                                      "db_path": "c"}


def test_json_config_supported(tmp_path):
    path = write(tmp_path, json.dumps({"port": 1234, "admin_password": "x"}),
                 name="config.json")
    assert load_config_file(path) == {"port": 1234, "admin_token": "x"}


def test_unknown_key_is_rejected_with_hint(tmp_path):
    """拼错的配置项必须报错，静默忽略会让人以为设置生效了。"""
    path = write(tmp_path, 'admin_passwrod = "oops"')
    with pytest.raises(ConfigError, match="admin_passwrod"):
        load_config_file(path)


def test_internal_fields_not_settable(tmp_path):
    for key in ("warnings", "config_file", "set_password_mode"):
        with pytest.raises(ConfigError):
            load_config_file(write(tmp_path, f'{key} = "x"', name=f"{key}.toml"))


def test_section_syntax_gets_clear_message(tmp_path):
    path = write(tmp_path, "[server]\nport = 1")
    with pytest.raises(ConfigError, match="扁平结构"):
        load_config_file(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config_file(str(tmp_path / "nope.toml"))


def test_malformed_toml_raises(tmp_path):
    with pytest.raises(ConfigError, match="解析失败"):
        load_config_file(write(tmp_path, "port = = 1"))


# ---------- 优先级 ----------

def test_precedence_cli_over_env_over_file():
    merged = build_settings(
        file_values={"admin_token": "from-file", "port": 1111, "host": "1.1.1.1"},
        cli_values={"admin_token": "from-cli"},
        env={"MISTRAL_POOL_ADMIN_TOKEN": "from-env",
             "MISTRAL_POOL_API_KEY": "key-from-env"})
    assert merged.admin_token == "from-cli"      # 命令行赢
    assert merged.client_api_key == "key-from-env"  # 环境变量赢过默认
    assert merged.port == 1111                    # 配置文件赢过默认
    assert merged.max_concurrency == 32           # 谁都没给, 用默认


def test_env_beats_file():
    merged = build_settings(file_values={"admin_token": "from-file"}, cli_values={},
                            env={"MISTRAL_POOL_ADMIN_TOKEN": "from-env"})
    assert merged.admin_token == "from-env"


def test_none_cli_values_do_not_clobber():
    """argparse 没给的项是 None，不能把配置文件的值覆盖掉。"""
    merged = build_settings(file_values={"port": 1111},
                            cli_values={"port": None, "host": None}, env={})
    assert merged.port == 1111
    assert merged.host == "127.0.0.1"


# ---------- keys_file 三态 ----------

def test_keys_file_states(tmp_path, monkeypatch):
    fake = tmp_path / "mistral_keys.json"
    fake.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("core.config.DEFAULT_KEYS_FILE", str(fake))

    auto = Settings(keys_file=None)
    resolve_keys_file(auto)
    assert auto.keys_file == str(fake), "没配过就自动找默认文件"

    off = Settings(keys_file="")
    resolve_keys_file(off)
    assert off.keys_file is None, "空串表示显式关闭"

    explicit = Settings(keys_file="/tmp/other.json")
    resolve_keys_file(explicit)
    assert explicit.keys_file == "/tmp/other.json"


# ---------- 模板 ----------

def test_template_is_valid_and_round_trips(tmp_path):
    path = str(tmp_path / "config.toml")
    write_config_template(path)
    values = load_config_file(path)
    assert values["host"] == "127.0.0.1"
    assert values["reasoning_format"] == "reasoning_content"
    Settings(**{k: v for k, v in values.items() if v not in ("", None)}).validate()


def test_template_refuses_to_overwrite(tmp_path):
    path = str(tmp_path / "config.toml")
    write_config_template(path)
    with pytest.raises(ConfigError, match="已存在"):
        write_config_template(path)


# ---------- 命令行入口 ----------

def test_parse_args_reads_config(tmp_path, monkeypatch):
    path = write(tmp_path, 'port = 8899\nadmin_password = "pw-from-file"\nkeys_file = ""')
    monkeypatch.setattr("core.config.DEFAULT_KEYS_FILE", str(tmp_path / "none.json"))
    s = app_module.parse_args(["--config", path])
    assert s.port == 8899
    assert s.admin_token == "pw-from-file"
    assert s.keys_file is None
    assert s.config_file == path


def test_parse_args_cli_overrides_config(tmp_path, monkeypatch):
    path = write(tmp_path, 'port = 8899\nreasoning_format = "strip"')
    monkeypatch.setattr("core.config.DEFAULT_KEYS_FILE", str(tmp_path / "none.json"))
    s = app_module.parse_args(["--config", path, "--port", "7000",
                               "--reasoning-format", "think_tags"])
    assert s.port == 7000
    assert s.reasoning_format == "think_tags"


def test_parse_args_no_config_ignores_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.DEFAULT_CONFIG_FILE", write(tmp_path, "port = 8899"))
    monkeypatch.setattr("core.config.DEFAULT_KEYS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr("app.DEFAULT_CONFIG_FILE", str(tmp_path / "config.toml"))
    assert app_module.parse_args(["--no-config"]).port == 8787


def test_parse_args_validates_config_values(tmp_path, monkeypatch):
    path = write(tmp_path, 'reasoning_format = "nope"')
    monkeypatch.setattr("core.config.DEFAULT_KEYS_FILE", str(tmp_path / "none.json"))
    with pytest.raises(ConfigError, match="reasoning_format"):
        app_module.parse_args(["--config", path])


def test_admin_password_alias_on_cli(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.DEFAULT_KEYS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr("app.DEFAULT_CONFIG_FILE", str(tmp_path / "config.toml"))
    assert app_module.parse_args(["--admin-password", "x"]).admin_token == "x"
    assert app_module.parse_args(["--admin-token", "y"]).admin_token == "y"


# ---------- --set-password ----------

def test_set_password_cli_updates_database(tmp_path, monkeypatch):
    from core.auth import AuthManager
    from core.store import UsageStore

    db = str(tmp_path / "pw.db")
    settings = Settings(db_path=db, admin_token="")
    monkeypatch.setattr("getpass.getpass", lambda *_: "chosen-password")
    assert app_module.set_password_cli(settings) == 0

    store = UsageStore(db, start_writer=False)
    auth = AuthManager(store)
    auth.load()
    assert auth.verify_password("chosen-password")
    assert auth.using_generated_password is False
    store.close()


def test_set_password_cli_rejects_mismatch(tmp_path, monkeypatch):
    answers = iter(["aaaaaa", "bbbbbb"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(answers))
    assert app_module.set_password_cli(Settings(db_path=str(tmp_path / "p.db"))) == 1


def test_set_password_cli_rejects_short(tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda *_: "abc")
    assert app_module.set_password_cli(Settings(db_path=str(tmp_path / "p.db"))) == 1


def test_set_password_cli_refuses_when_password_is_fixed(tmp_path):
    settings = Settings(db_path=str(tmp_path / "p.db"), admin_token="fixed",
                        config_file="config.toml")
    assert app_module.set_password_cli(settings) == 2
