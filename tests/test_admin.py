# -*- coding: utf-8 -*-
"""管理接口：鉴权、信息泄露、账号/密钥/模型管理。"""
import json

import httpx
import pytest

from core.config import ConfigError, Settings

AUTH = {"X-Admin-Token": "secret"}

ADMIN_ENDPOINTS = (
    ("GET", "/admin/stats"), ("GET", "/admin/logs"), ("GET", "/admin/logs/filters"),
    ("GET", "/admin/accounts"), ("POST", "/admin/accounts"),
    ("POST", "/admin/accounts/import"), ("POST", "/admin/accounts/action"),
    ("GET", "/admin/keys"), ("POST", "/admin/keys"), ("POST", "/admin/keys/action"),
    ("POST", "/admin/keys/update"), ("GET", "/admin/models"),
    ("POST", "/admin/models/sync"), ("POST", "/admin/models/alias"),
    ("GET", "/admin/config"), ("POST", "/admin/config"), ("POST", "/admin/password"),
    ("POST", "/admin/sessions/revoke"), ("GET", "/admin/export"), ("POST", "/admin/cleanup"),
)


def ok(request):
    return httpx.Response(200, json={"choices": []})


# ---------- 鉴权 ----------

def test_every_admin_endpoint_requires_auth(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="secret")
    with client:
        for method, path in ADMIN_ENDPOINTS:
            r = client.request(method, path, json={})
            assert r.status_code == 401, f"{method} {path} 竟然放行了"
            assert r.json()["error"]["type"] == "authentication_error"


def test_header_token_accepted(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="secret")
    with client:
        assert client.get("/admin/accounts", headers=AUTH).status_code == 200
        assert client.get("/admin/accounts",
                          headers={"X-Admin-Token": "nope"}).status_code == 401


def test_query_token_is_not_accepted(make_client):
    """?token= 会进浏览器历史、Referer 和访问日志，刻意不支持。"""
    client = make_client(ok, auth_enabled=True, admin_token="secret")
    with client:
        assert client.get("/admin/accounts?token=secret").status_code == 401


# ---------- 凭据泄露 ----------

def test_upstream_key_never_returned_by_default(make_client):
    client = make_client(ok)
    with client:
        r = client.get("/admin/accounts")
        acc = r.json()["accounts"][0]
        assert "api_key" not in acc
        assert all("api_key" not in o for o in acc["orgs"])
        assert "key-a@x.com" not in r.text


def test_reveal_requires_flag_and_auth(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="secret")
    with client:
        assert client.get("/admin/accounts?reveal=1").status_code == 401
        revealed = client.get("/admin/accounts?reveal=1", headers=AUTH).json()
        assert revealed["accounts"][0]["orgs"][0]["api_key"] == "key-a@x.com"


def test_client_key_plaintext_only_returned_at_creation(make_client):
    client = make_client(ok)
    with client:
        raw = client.post("/admin/keys", json={"name": "t"}).json()["key"]
        assert raw.startswith("sk-pool-")
        listed = client.get("/admin/keys")
        assert raw not in listed.text
        assert "key_hash" not in listed.text


# ---------- 任意文件读取 ----------

def test_import_rejects_server_side_path(make_client):
    """服务端不接受本地路径 —— 那等于开了个任意文件读取。"""
    client = make_client(ok)
    with client:
        r = client.post("/admin/accounts/import", json={"path": "mistral_keys.json"})
        assert r.status_code == 400 and "内容为空" in r.json()["error"]
        assert client.post("/admin/accounts/import", json={"content": "  "}).status_code == 400


def test_import_accepts_json_and_csv(make_client):
    client = make_client(ok)
    with client:
        r = client.post("/admin/accounts/import",
                        json={"content": '[{"email":"c@x.com","api_key":"kc"}]'})
        assert r.json()["added"] == 1
        r = client.post("/admin/accounts/import",
                        json={"content": "email,api_key\nd@x.com,kd\n"})
        assert r.json()["added"] == 1


def test_import_accepts_register_script_output(make_client):
    """mistral_register.py 的产物必须能原样导入，两种格式都要。"""
    import csv
    import io
    import json

    fields = ["email", "email_password", "mistral_password", "org_id", "org_tier",
              "workspace_id", "key_id", "api_key", "created_at"]
    records = [{f: f"{f}-{i}" for f in fields} | {"email": f"reg{i}@x.com",
                                                  "api_key": f"key{i}"}
               for i in range(3)]

    client = make_client(ok)
    with client:
        r = client.post("/admin/accounts/import",
                        json={"content": json.dumps(records, ensure_ascii=False, indent=2)})
        assert r.json()["added"] == 3

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([{**rec, "email": f"csv{i}@x.com", "api_key": f"ck{i}"}
                          for i, rec in enumerate(records)])
        r = client.post("/admin/accounts/import", json={"content": buf.getvalue()})
        assert r.json()["added"] == 3

        # 凭据字段要跟着一起进库，不能只留 email/api_key
        revealed = client.get("/admin/accounts?reveal=1").json()["accounts"]
        one = next(a for a in revealed if a["email"] == "reg0@x.com")
        org = one["orgs"][0]
        assert org["api_key"] == "key0" and org["org_id"] == "org_id-0"
        assert org["org_tier"] == "org_tier-0"


def test_import_reports_skipped_and_blocked(make_client):
    client = make_client(ok)
    with client:
        client.post("/admin/accounts/action", json={"email": "a@x.com", "action": "remove"})
        r = client.post("/admin/accounts/import", json={"content": json.dumps([
            {"email": "a@x.com", "api_key": "k"},      # 有墓碑，会被挡
            {"email": "fresh@x.com", "api_key": "k2"},  # 正常
            {"email": "nokey@x.com"},                   # 没有 api_key
        ])}).json()
        assert r["added"] == 1 and r["blocked"] == 1 and r["skipped"] == 1


def test_import_rejects_content_without_any_key(make_client):
    client = make_client(ok)
    with client:
        r = client.post("/admin/accounts/import",
                        json={"content": '[{"email":"x@y.com"}]'})
        assert r.status_code == 400 and "api_key" in r.json()["error"]
        r = client.post("/admin/accounts/import", json={"content": "这不是账号文件"})
        assert r.status_code == 400


# ---------- 账号操作 ----------

def test_account_actions(make_client):
    client = make_client(ok)
    with client:
        assert client.post("/admin/accounts/action",
                           json={"emails": ["a@x.com"], "action": "disable"}).json()["ok"] == 1
        accounts = {a["email"]: a for a in client.get("/admin/accounts").json()["accounts"]}
        assert accounts["a@x.com"]["enabled"] is False
        assert client.post("/admin/accounts/action",
                           json={"email": "a@x.com", "action": "enable"}).json()["ok"] == 1
        assert client.post("/admin/accounts/action",
                           json={"emails": ["a@x.com"], "action": "remove"}).json()["total"] == 1


def test_account_action_validates(make_client):
    client = make_client(ok)
    with client:
        assert client.post("/admin/accounts/action",
                           json={"emails": ["a@x.com"], "action": "nuke"}).status_code == 400
        assert client.post("/admin/accounts/action", json={"action": "enable"}).status_code == 400
        assert client.post("/admin/accounts", json={"email": "e@x.com"}).status_code == 400


def test_removed_account_can_be_restored(make_client):
    client = make_client(ok)
    with client:
        client.post("/admin/accounts/action", json={"email": "a@x.com", "action": "remove"})
        r = client.post("/admin/accounts/import",
                        json={"content": '[{"email":"a@x.com","api_key":"k"}]'})
        assert r.json()["added"] == 0  # 墓碑挡住了
        client.post("/admin/accounts/action", json={"email": "a@x.com", "action": "restore"})
        r = client.post("/admin/accounts/import",
                        json={"content": '[{"email":"a@x.com","api_key":"k"}]'})
        assert r.json()["added"] == 1


# ---------- 密钥管理 ----------

def test_key_lifecycle(make_client):
    client = make_client(ok)
    with client:
        created = client.post("/admin/keys", json={"name": "cli", "rpm_limit": 10}).json()
        key_id = created["info"]["id"]
        assert created["info"]["rpm_limit"] == 10

        client.post("/admin/keys/update", json={"id": key_id, "name": "renamed",
                                                "daily_token_limit": 500})
        listed = client.get("/admin/keys").json()
        assert listed["keys"][0]["name"] == "renamed"
        assert listed["keys"][0]["daily_token_limit"] == 500
        assert listed["auth_required"] is True

        assert client.post("/admin/keys/action",
                           json={"id": key_id, "action": "disable"}).json()["ok"] == 1
        assert client.post("/admin/keys/action",
                           json={"id": key_id, "action": "revoke"}).json()["total"] == 0


def test_key_action_validates(make_client):
    client = make_client(ok)
    with client:
        assert client.post("/admin/keys/action",
                           json={"id": "x", "action": "nuke"}).status_code == 400
        assert client.post("/admin/keys/action", json={"action": "revoke"}).status_code == 400
        assert client.post("/admin/keys/update", json={"id": "nope"}).status_code == 404


# ---------- 模型管理 ----------

def test_model_sync_and_alias(make_client):
    client = make_client(ok)
    with client:
        r = client.post("/admin/models/sync").json()
        assert r["synced"] == 3
        assert client.post("/admin/models/alias",
                           json={"alias": "gpt-4o", "target": "mistral-large-latest"}
                           ).json()["ok"] is True
        assert client.get("/admin/models").json()["aliases"]["gpt-4o"] == "mistral-large-latest"
        assert client.post("/admin/models/alias",
                           json={"action": "remove", "alias": "gpt-4o"}).json()["ok"] is True


def test_alias_shadowing_real_model_rejected(make_client):
    client = make_client(ok)
    with client:
        client.post("/admin/models/sync")
        r = client.post("/admin/models/alias",
                        json={"alias": "mistral-small-latest", "target": "mistral-large-latest"})
        assert r.status_code == 400


# ---------- 运行配置 ----------

def test_config_hot_update(make_client):
    client = make_client(ok)
    with client:
        assert client.get("/admin/config").json()["reasoning_format"] == "reasoning_content"
        assert client.post("/admin/config", json={"reasoning_format": "think_tags"}
                           ).json()["changed"]["reasoning_format"] == "think_tags"
        assert client.app.state.ctx.settings.reasoning_format == "think_tags"
        assert client.post("/admin/config", json={"reasoning_format": "bogus"}).status_code == 400


# ---------- 导出与页面 ----------

# ---------- 调用日志 ----------

def seed_logs(client):
    store = client.app.state.ctx.store
    store.record("a@x.com", "m1", "/v1/chat/completions", 200, prompt_tokens=10,
                 completion_tokens=5, requested_model="gpt-4o", client_key="ck1",
                 stream=True, ttft_ms=120, duration_ms=800)
    store.record("b@x.com", "m2", "/v1/chat/completions", 429, requested_model="m2",
                 error="rate limited", duration_ms=50, attempts=3)
    store.record("a@x.com", "m1", "/v1/embeddings", 200, prompt_tokens=3,
                 requested_model="mistral-embed", duration_ms=90)
    store.flush()


def test_logs_pagination(make_client):
    client = make_client(ok)
    with client:
        seed_logs(client)
        page1 = client.get("/admin/logs?limit=2&page=1").json()
        assert page1["total"] == 3 and page1["pages"] == 2 and len(page1["rows"]) == 2
        page2 = client.get("/admin/logs?limit=2&page=2").json()
        assert len(page2["rows"]) == 1
        assert page1["rows"][0]["id"] > page2["rows"][0]["id"], "应按时间倒序"


def test_logs_filters(make_client):
    client = make_client(ok)
    with client:
        seed_logs(client)
        assert client.get("/admin/logs?status=error").json()["total"] == 1
        assert client.get("/admin/logs?status=200").json()["total"] == 2
        assert client.get("/admin/logs?account=b@x").json()["total"] == 1
        assert client.get("/admin/logs?client_key=ck1").json()["total"] == 1
        assert client.get("/admin/logs?endpoint=/v1/embeddings").json()["total"] == 1
        assert client.get("/admin/logs?model=gpt-4o").json()["total"] == 1
        assert client.get("/admin/logs?stream=1").json()["total"] == 1
        assert client.get("/admin/logs?search=rate").json()["total"] == 1
        assert client.get("/admin/logs?hours=1&status=error&stream=1").json()["total"] == 0


def test_logs_rows_carry_detail_fields(make_client):
    client = make_client(ok)
    with client:
        seed_logs(client)
        row = client.get("/admin/logs?stream=1").json()["rows"][0]
        for field in ("id", "ts", "account", "client_key", "requested_model", "endpoint",
                      "prompt_tokens", "reasoning_tokens", "cached_tokens", "ttft_ms",
                      "attempts", "finish_reason"):
            assert field in row, field


def test_logs_totals_and_key_names(make_client):
    client = make_client(ok)
    with client:
        created = client.post("/admin/keys", json={"name": "我的脚本"}).json()
        key_id = created["info"]["id"]
        store = client.app.state.ctx.store
        store.record("a@x.com", "m", "/v1/chat/completions", 200, prompt_tokens=7,
                     completion_tokens=3, client_key=key_id)
        store.flush()
        body = client.get("/admin/logs").json()
        assert body["sum_tokens"] == 10 and body["ok"] == 1
        assert body["rows"][0]["client_name"] == "我的脚本"

        # 吊销后名字查不到，得给个能看懂的说法而不是裸 id
        client.post("/admin/keys/action", json={"id": key_id, "action": "revoke"})
        revoked = client.get("/admin/logs").json()["rows"][0]["client_name"]
        assert revoked.startswith("已吊销") and key_id not in revoked


def test_logs_anonymous_calls_have_blank_key_name(make_client):
    client = make_client(ok)
    with client:
        client.app.state.ctx.store.record("a@x.com", "m", "/v1/chat/completions", 200)
        client.app.state.ctx.store.flush()
        assert client.get("/admin/logs").json()["rows"][0]["client_name"] == ""


def test_logs_filter_options(make_client):
    client = make_client(ok)
    with client:
        seed_logs(client)
        f = client.get("/admin/logs/filters").json()
        assert "/v1/embeddings" in f["endpoints"]
        assert "gpt-4o" in f["models"]
        assert isinstance(f["keys"], list)


def test_logs_limit_is_capped(make_client):
    client = make_client(ok)
    with client:
        r = client.get("/admin/logs?limit=9999")
        assert r.status_code == 400
        assert r.json()["error"]["param"] == "limit"
        assert client.get("/admin/logs?limit=200").status_code == 200


def test_export_csv(make_client):
    client = make_client(ok)
    with client:
        client.app.state.ctx.store.record("a@x.com", "m", "/v1/chat/completions", 200,
                                          prompt_tokens=1, completion_tokens=2)
        client.app.state.ctx.store.flush()
        r = client.get("/admin/export?hours=24")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert "a@x.com" in r.text


def test_pages_render(make_client):
    client = make_client(ok)
    with client:
        for path in ("/", "/channels", "/tokens", "/models", "/logs", "/settings"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert "<!DOCTYPE html>" in r.text
        for asset in ("/static/app.css", "/static/common.js", "/static/icons.js",
                      "/static/ui.js", "/static/charts.js", "/static/dashboard.js",
                      "/static/channels.js", "/static/tokens.js", "/static/models.js",
                      "/static/logs.js", "/static/settings.js"):
            assert client.get(asset).status_code == 200, asset


def test_legacy_paths_redirect(make_client):
    client = make_client(ok)
    with client:
        assert client.get("/admin", follow_redirects=False).headers["location"] == "/channels"
        assert client.get("/keys", follow_redirects=False).headers["location"] == "/tokens"


def test_health_hides_details_when_anonymous(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="secret")
    with client:
        anon = client.get("/health").json()
        assert anon == {"status": "ok"}
        full = client.get("/health", headers=AUTH).json()
        assert full["accounts"] == 2 and full["inflight"] == 0


def test_health_full_when_auth_disabled(make_client):
    client = make_client(ok)
    with client:
        h = client.get("/health").json()
        assert h["accounts"] == 2 and h["client_auth"] is False


# ---------- 启动配置守卫 ----------

def test_public_bind_with_no_auth_refused():
    with pytest.raises(ConfigError, match="不允许关闭登录"):
        Settings(host="0.0.0.0", auth_enabled=False).validate()


def test_public_bind_with_auth_is_fine():
    """默认强制登录，所以绑公网本身不再需要额外开关。"""
    s = Settings(host="0.0.0.0")
    s.validate()
    assert not any("危险" in w for w in s.warnings)


def test_public_bind_no_auth_allowed_with_explicit_flag():
    s = Settings(host="0.0.0.0", auth_enabled=False, allow_insecure=True)
    s.validate()
    assert any("危险" in w for w in s.warnings)


def test_loopback_no_auth_only_warns():
    s = Settings(host="127.0.0.1", auth_enabled=False)
    s.validate()
    assert any("登录已关闭" in w for w in s.warnings)


def test_open_proxy_warning():
    s = Settings()
    s.warn_if_open_proxy(auth_required=False)
    assert any("未启用调用方鉴权" in w for w in s.warnings)
    s2 = Settings()
    s2.warn_if_open_proxy(auth_required=True)
    assert not s2.warnings


def test_bad_reasoning_format_refused():
    with pytest.raises(ConfigError):
        Settings(reasoning_format="nope").validate()
