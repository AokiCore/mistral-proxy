# -*- coding: utf-8 -*-
"""管理台登录：密码存储、会话 Cookie、页面守卫、暴力破解节流。"""
import time

import httpx
import pytest

from core.auth import SESSION_COOKIE, AuthManager, hash_password
from core.store import UsageStore


def ok(request):
    return httpx.Response(200, json={"choices": []})


@pytest.fixture
def manager(tmp_path):
    store = UsageStore(str(tmp_path / "a.db"), start_writer=False)
    yield lambda **kw: AuthManager(store, **kw), store
    store.close()


# ---------- 密码 ----------

def test_first_run_generates_password(manager):
    make, store = manager
    auth = make()
    auth.load()
    assert auth.generated_password, "首次启动应生成密码"
    assert auth.verify_password(auth.generated_password)
    assert auth.password_source == "database"


def test_generated_password_is_hashed_at_rest(manager):
    make, store = manager
    auth = make()
    auth.load()
    stored = store.get_meta("admin_password")
    assert auth.generated_password not in stored
    assert hash_password(auth.generated_password, "wrong-salt") not in stored


def test_password_persists_across_restart(manager):
    make, _ = manager
    first = make()
    first.load()
    password = first.generated_password

    second = make()
    second.load()
    assert second.generated_password == "", "第二次启动不该重新生成"
    assert second.verify_password(password)


def test_fixed_token_takes_precedence(manager):
    make, _ = manager
    auth = make(admin_token="from-cli")
    auth.load()
    assert auth.password_source == "fixed"
    assert auth.verify_password("from-cli")
    assert not auth.verify_password("anything-else")
    assert auth.generated_password == ""
    assert auth.using_generated_password is False


def test_generated_flag_clears_after_user_sets_password(manager):
    """界面靠这个标记提示"你还在用初始密码"，用户改过之后必须消失。"""
    make, _ = manager
    auth = make()
    auth.load()
    assert auth.using_generated_password is True

    auth.set_password("chosen-by-me")
    assert auth.using_generated_password is False

    restarted = make()
    restarted.load()
    assert restarted.using_generated_password is False, "标记要持久化，重启后不能复原"


def test_source_label_reflects_config_file(manager):
    make, _ = manager
    auth = make(admin_token="x")
    auth.fixed_source = "配置文件 config.toml"
    auth.load()
    assert auth.password_source_label == "配置文件 config.toml"
    assert make().password_source_label.startswith("数据库")


def test_change_password(manager):
    make, _ = manager
    auth = make()
    auth.load()
    auth.set_password("brand-new-pass")
    assert auth.verify_password("brand-new-pass")
    assert not auth.verify_password(auth.generated_password)


def test_short_password_rejected(manager):
    make, _ = manager
    auth = make()
    auth.load()
    with pytest.raises(ValueError):
        auth.set_password("12345")


def test_disabled_auth_accepts_anything(manager):
    make, _ = manager
    auth = make(enabled=False)
    auth.load()
    assert auth.verify_password("") is True
    assert auth.verify_session("garbage") is True


# ---------- 会话 ----------

def test_session_roundtrip(manager):
    make, _ = manager
    auth = make(admin_token="pw")
    auth.load()
    cookie, max_age = auth.issue_session(ttl_days=1)
    assert max_age == 86400
    assert auth.verify_session(cookie)


def test_tampered_session_rejected(manager):
    make, _ = manager
    auth = make(admin_token="pw")
    auth.load()
    cookie, _ = auth.issue_session()
    payload, _, sig = cookie.rpartition(".")
    assert not auth.verify_session(payload + "." + "0" * len(sig))
    assert not auth.verify_session(payload)
    assert not auth.verify_session("")


def test_expired_session_rejected(manager):
    make, _ = manager
    auth = make(admin_token="pw")
    auth.load()
    cookie, _ = auth.issue_session(ttl_days=-1)
    assert not auth.verify_session(cookie)


def test_rotate_invalidates_all_sessions(manager):
    make, _ = manager
    auth = make(admin_token="pw")
    auth.load()
    cookie, _ = auth.issue_session()
    auth.rotate_sessions()
    assert not auth.verify_session(cookie)


def test_password_change_invalidates_sessions(manager):
    make, _ = manager
    auth = make()
    auth.load()
    cookie, _ = auth.issue_session()
    auth.set_password("something-else")
    assert not auth.verify_session(cookie)


def test_session_survives_restart(tmp_path):
    """签名密钥从库里的 salt 与密码散列派生，进程重启不该踢掉用户。"""
    store = UsageStore(str(tmp_path / "a.db"), start_writer=False)
    first = AuthManager(store)
    first.load()
    cookie, _ = first.issue_session()

    second = AuthManager(store)
    second.load()
    assert second.verify_session(cookie)
    store.close()


# ---------- HTTP 层 ----------

def test_login_flow(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        assert client.get("/admin/config").status_code == 401

        bad = client.post("/auth/login", json={"password": "wrong"})
        assert bad.status_code == 401

        good = client.post("/auth/login", json={"password": "s3cret", "next": "/logs"})
        assert good.status_code == 200
        assert good.json()["next"] == "/logs"
        assert SESSION_COOKIE in good.cookies or SESSION_COOKIE in client.cookies

        assert client.get("/admin/config").status_code == 200


def test_logout_clears_session(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        client.post("/auth/login", json={"password": "s3cret"})
        assert client.get("/admin/config").status_code == 200
        client.post("/auth/logout")
        assert client.get("/admin/config").status_code == 401


def test_session_cookie_is_httponly(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        r = client.post("/auth/login", json={"password": "s3cret"})
        raw = r.headers["set-cookie"].lower()
        assert "httponly" in raw
        assert "samesite=lax" in raw
        assert "s3cret" not in raw


def test_pages_redirect_to_login(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        for path in ("/", "/channels", "/tokens", "/models", "/logs", "/settings"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 303, path
            assert r.headers["location"].startswith("/login?next=")

        client.post("/auth/login", json={"password": "s3cret"})
        assert client.get("/").status_code == 200


def test_login_page_redirects_when_already_in(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        assert client.get("/login", follow_redirects=False).status_code == 200
        client.post("/auth/login", json={"password": "s3cret"})
        r = client.get("/login", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"


def test_open_redirect_blocked(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        # 未登录时打开带外部 next 的登录页，页面里的 next 必须被清成站内路径
        page = client.get("/login?next=https://evil.example.com", follow_redirects=False)
        assert page.status_code == 200
        assert "evil.example.com" not in page.text

        for hostile in ("//evil.example.com/x", "https://evil.example.com", "javascript:x"):
            r = client.post("/auth/login", json={"password": "s3cret", "next": hostile})
            assert r.json()["next"] == "/", hostile


def test_brute_force_throttled(make_client):
    # 节流状态挂在应用上下文上，每个 app 实例独立，测试之间无须手动清理
    from core.auth import LOGIN_FAIL_LIMIT
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        for _ in range(LOGIN_FAIL_LIMIT):
            assert client.post("/auth/login", json={"password": "x"}).status_code == 401
        blocked = client.post("/auth/login", json={"password": "x"})
        assert blocked.status_code == 429
        # 锁定期间即使密码正确也不放行
        assert client.post("/auth/login", json={"password": "s3cret"}).status_code == 429


def test_successful_login_clears_failures(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        for _ in range(3):
            client.post("/auth/login", json={"password": "x"})
        assert client.post("/auth/login", json={"password": "s3cret"}).status_code == 200
        # TestClient 的来源 IP 固定是 testclient
        throttle = client.app.state.ctx.login_throttle
        assert throttle.failure_count("testclient") == 0


def test_auth_status_endpoint(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        s = client.get("/auth/status").json()
        assert s == {"auth_enabled": True, "logged_in": False, "password_source": "fixed"}
        client.post("/auth/login", json={"password": "s3cret"})
        assert client.get("/auth/status").json()["logged_in"] is True


def test_default_password_flag_surfaces_in_api(make_client):
    client = make_client(ok, auth_enabled=True)
    with client:
        password = client.app.state.ctx.auth.generated_password
        client.post("/auth/login", json={"password": password})
        assert client.get("/health").json()["default_password"] is True
        assert client.get("/admin/config").json()["default_password"] is True

        client.post("/admin/password", json={"current": password, "new": "chosen-pw"})
        client.post("/auth/login", json={"password": "chosen-pw"})
        assert client.get("/health").json()["default_password"] is False


def test_fixed_password_is_never_flagged_as_default(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        client.post("/auth/login", json={"password": "s3cret"})
        cfg = client.get("/admin/config").json()
        assert cfg["default_password"] is False
        assert cfg["password_source"] == "fixed"


def test_no_auth_mode_skips_login(make_client):
    client = make_client(ok)  # 夹具默认 auth_enabled=False
    with client:
        assert client.get("/").status_code == 200
        assert client.get("/admin/config").status_code == 200
        assert client.get("/login", follow_redirects=False).status_code == 303


def test_password_change_over_http(make_client):
    client = make_client(ok, auth_enabled=True)
    with client:
        password = client.app.state.ctx.auth.generated_password
        client.post("/auth/login", json={"password": password})

        assert client.post("/admin/password",
                           json={"current": "wrong", "new": "newpass123"}).status_code == 401
        assert client.post("/admin/password",
                           json={"current": password, "new": "short"}).status_code == 400

        r = client.post("/admin/password", json={"current": password, "new": "newpass123"})
        assert r.status_code == 200
        # 改完密码旧会话失效
        assert client.get("/admin/config").status_code == 401
        client.post("/auth/login", json={"password": "newpass123"})
        assert client.get("/admin/config").status_code == 200


def test_password_change_blocked_for_cli_token(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        client.post("/auth/login", json={"password": "s3cret"})
        r = client.post("/admin/password", json={"current": "s3cret", "new": "newpass123"})
        assert r.status_code == 400
        assert "--admin-token" in r.json()["error"]


def test_revoke_sessions_over_http(make_client):
    client = make_client(ok, auth_enabled=True, admin_token="s3cret")
    with client:
        client.post("/auth/login", json={"password": "s3cret"})
        assert client.post("/admin/sessions/revoke").status_code == 200
        assert client.get("/admin/config").status_code == 401
