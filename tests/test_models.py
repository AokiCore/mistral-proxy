# -*- coding: utf-8 -*-
"""模型注册表：同步、别名解析、能力过滤。"""
import pytest

from core.models import ModelRegistry
from core.store import UsageStore
from tests.conftest import MODEL_LIST

RAW = MODEL_LIST["data"]


@pytest.fixture
def registry(tmp_path):
    store = UsageStore(str(tmp_path / "m.db"), start_writer=False)
    reg = ModelRegistry(store)
    reg.update(RAW)
    yield reg
    store.close()


def test_update_indexes_models(registry):
    assert len(registry.models) == 3
    assert registry.resolve("mistral-small-latest").max_context_length == 262144


def test_upstream_aliases_resolve(registry):
    assert registry.resolve("mistral-small-2603").id == "mistral-small-latest"


def test_resolution_is_case_insensitive(registry):
    assert registry.resolve("MISTRAL-Small-Latest").id == "mistral-small-latest"


def test_unknown_model_passes_through(registry):
    assert registry.resolve("nope") is None
    assert registry.resolve_id("nope") == "nope"


def test_capability_flags(registry):
    assert registry.resolve("mistral-small-latest").supports_reasoning is True
    assert registry.resolve("mistral-large-latest").supports_reasoning is False
    assert registry.resolve("mistral-embed").supports_chat is False
    assert registry.resolve("mistral-embed").supports_embeddings is True


def test_custom_alias(registry):
    registry.set_alias("gpt-4o", "mistral-large-latest")
    assert registry.resolve_id("gpt-4o") == "mistral-large-latest"
    assert registry.remove_alias("gpt-4o") is True
    assert registry.resolve_id("gpt-4o") == "gpt-4o"


def test_alias_cannot_shadow_real_model(registry):
    with pytest.raises(ValueError, match="真实模型名"):
        registry.set_alias("mistral-small-latest", "mistral-large-latest")


def test_alias_requires_both_sides(registry):
    with pytest.raises(ValueError):
        registry.set_alias("", "x")
    with pytest.raises(ValueError):
        registry.set_alias("x", "")


def test_list_openai_filters_by_capability(registry):
    ids = [m["id"] for m in registry.list_openai()]
    assert "mistral-embed" not in ids
    assert "mistral-small-latest" in ids
    assert all(m["object"] == "model" for m in registry.list_openai())


def test_aliases_appear_in_listing(registry):
    registry.set_alias("gpt-4o", "mistral-large-latest")
    entry = next(m for m in registry.list_openai() if m["id"] == "gpt-4o")
    assert entry["alias_of"] == "mistral-large-latest"


def test_registry_persists(tmp_path):
    store = UsageStore(str(tmp_path / "m.db"), start_writer=False)
    reg = ModelRegistry(store)
    reg.update(RAW)
    reg.set_alias("gpt-4o", "mistral-large-latest")

    fresh = ModelRegistry(store)
    fresh.load()
    assert len(fresh.models) == 3
    assert fresh.resolve_id("gpt-4o") == "mistral-large-latest"
    store.close()


def test_empty_sync_keeps_previous_models(registry):
    assert registry.update([]) == 0
    assert len(registry.models) == 3


def test_stale_detection(registry):
    assert registry.stale() is False
    registry.synced_at = 0
    assert registry.stale() is True
