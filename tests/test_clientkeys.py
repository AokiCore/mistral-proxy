# -*- coding: utf-8 -*-
"""下游客户端密钥：签发、校验、限速、配额、白名单。"""
import time

import pytest

from core.clientkeys import ClientKeyStore, QuotaError, hash_key
from core.store import UsageStore


@pytest.fixture
def keys(tmp_path):
    store = UsageStore(str(tmp_path / "k.db"), start_writer=False)
    ks = ClientKeyStore(store)
    ks.load()
    yield ks
    store.close()


def test_created_key_is_returned_once_and_hashed_at_rest(keys):
    key, raw = keys.create(name="cherry")
    assert raw.startswith("sk-pool-")
    assert key.key_hash == hash_key(raw)
    assert raw not in str(key.to_dict())
    assert "key_hash" not in key.to_dict()


def test_verify_roundtrip(keys):
    _, raw = keys.create()
    assert keys.verify(raw) is not None
    assert keys.verify(raw + "x") is None
    assert keys.verify("") is None


def test_keys_survive_restart(tmp_path):
    store = UsageStore(str(tmp_path / "k.db"), start_writer=False)
    ks = ClientKeyStore(store)
    _, raw = ks.create(name="persisted", rpm_limit=5)

    fresh = ClientKeyStore(store)
    fresh.load()
    found = fresh.verify(raw)
    assert found is not None and found.name == "persisted" and found.rpm_limit == 5
    store.close()


def test_revoke_denies_immediately(keys):
    key, raw = keys.create()
    assert keys.revoke(key.id) is True
    assert keys.verify(raw) is None
    assert keys.revoke(key.id) is False


def test_auth_required_flips_on_first_key(keys):
    assert keys.auth_required is False
    key, _ = keys.create()
    assert keys.auth_required is True
    keys.update(key.id, enabled=False)
    assert keys.auth_required is False


def test_static_key_enables_auth(keys):
    keys.set_static_key("sk-static")
    assert keys.auth_required is True
    assert keys.verify("sk-static").id == "static"
    assert keys.verify("sk-wrong") is None


def test_disabled_key_rejected(keys):
    key, _ = keys.create()
    keys.update(key.id, enabled=False)
    with pytest.raises(QuotaError) as e:
        keys.check(key, "m")
    assert e.value.status == 403


def test_expired_key_rejected(keys):
    key, _ = keys.create(ttl_days=1)
    key.expires_at = time.time() - 1
    with pytest.raises(QuotaError, match="expired"):
        keys.check(key, "m")
    assert key.to_dict()["expired"] is True


def test_model_allowlist(keys):
    key, _ = keys.create(allowed_models=["mistral-small-latest"])
    keys.check(key, "mistral-small-latest")
    with pytest.raises(QuotaError, match="not allowed"):
        keys.check(key, "mistral-large-latest")


def test_empty_allowlist_permits_everything(keys):
    key, _ = keys.create()
    keys.check(key, "anything")


def test_rpm_limit_enforced(keys):
    key, _ = keys.create(rpm_limit=3)
    for _ in range(3):
        keys.check(key, "m")
    with pytest.raises(QuotaError, match="3 requests/min"):
        keys.check(key, "m")


def test_rpm_window_slides(keys):
    key, _ = keys.create(rpm_limit=2)
    keys.check(key, "m")
    keys.check(key, "m")
    keys._recent[key.id].clear()  # 模拟 60 秒过去
    keys.check(key, "m")


def test_daily_token_quota(tmp_path):
    store = UsageStore(str(tmp_path / "k.db"), start_writer=False)
    ks = ClientKeyStore(store)
    key, _ = ks.create(daily_token_limit=100)
    ks.check(key, "m")

    store.record("acc", "m", "/v1/chat/completions", 200, prompt_tokens=60,
                 completion_tokens=60, client_key=key.id)
    store.flush()
    with pytest.raises(QuotaError, match="Daily token quota"):
        ks.check(key, "m")
    store.close()


def test_note_usage_accumulates(keys):
    key, _ = keys.create()
    keys.note_usage(key, 100)
    keys.note_usage(key, 50)
    assert key.total_requests == 2 and key.total_tokens == 150


def test_static_key_usage_not_tracked(keys):
    keys.set_static_key("sk-static")
    static = keys.verify("sk-static")
    keys.note_usage(static, 100)
    assert static.total_tokens == 0
