# -*- coding: utf-8 -*-
"""models / embeddings / moderations 端点，以及调用方鉴权与配额。"""
import base64
import json
import struct

import httpx

from tests.conftest import RATE_HEADERS

EMBED_BODY = {"model": "mistral-embed", "input": "hi"}


def embed_response(vec=(1.0, 2.0, 3.0, 4.0)):
    return httpx.Response(200, headers=RATE_HEADERS, json={
        "id": "e1", "object": "list", "model": "mistral-embed",
        "data": [{"object": "embedding", "index": 0, "embedding": list(vec)}],
        "usage": {"prompt_tokens": 2, "total_tokens": 2, "completion_tokens": 0,
                  "prompt_audio_seconds": None, "request_count": None,
                  "prompt_token_details": None}})


def moderation_response():
    return httpx.Response(200, headers=RATE_HEADERS, json={
        "id": "m1", "model": "mistral-moderation-2603", "results": [{
            "categories": {"violence_and_threats": True, "sexual": False},
            "category_scores": {"violence_and_threats": 0.95, "sexual": 0.01}}],
        "usage": {"prompt_tokens": 5, "total_tokens": 5, "request_count": 1}})


# ---------- models ----------

def test_models_listing(make_client):
    client = make_client(lambda rq: httpx.Response(404))
    with client:
        body = client.get("/v1/models").json()
    ids = [m["id"] for m in body["data"]]
    assert body["object"] == "list"
    assert "mistral-small-latest" in ids
    assert "mistral-embed" not in ids  # 非对话模型不出现在 chat 模型列表里


def test_retrieve_model(make_client):
    client = make_client(lambda rq: httpx.Response(404))
    with client:
        client.get("/v1/models")
        body = client.get("/v1/models/mistral-small-latest").json()
        assert body["id"] == "mistral-small-latest"
        assert body["capabilities"]["reasoning"] is True
        assert client.get("/v1/models/nope").status_code == 404


def test_retrieve_model_via_alias(make_client):
    client = make_client(lambda rq: httpx.Response(404))
    with client:
        client.get("/v1/models")
        body = client.get("/v1/models/mistral-small-2603").json()
    assert body["alias_of"] == "mistral-small-latest"


# ---------- embeddings ----------

def test_embeddings_basic(make_client):
    client = make_client(lambda rq: embed_response())
    with client:
        body = client.post("/v1/embeddings", json=EMBED_BODY).json()
    assert body["data"][0]["embedding"] == [1.0, 2.0, 3.0, 4.0]
    assert set(body["usage"]) == {"prompt_tokens", "total_tokens"}


def test_embeddings_base64(make_client):
    client = make_client(lambda rq: embed_response())
    with client:
        body = client.post("/v1/embeddings",
                           json={**EMBED_BODY, "encoding_format": "base64"}).json()
    raw = base64.b64decode(body["data"][0]["embedding"])
    assert struct.unpack("<4f", raw) == (1.0, 2.0, 3.0, 4.0)


def test_embeddings_dimensions_truncated(make_client):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return embed_response()

    client = make_client(handler)
    with client:
        body = client.post("/v1/embeddings", json={**EMBED_BODY, "dimensions": 2}).json()
    assert body["data"][0]["embedding"] == [1.0, 2.0]
    assert "dimensions" not in seen  # 上游会 422，不能透传


def test_embeddings_rejects_token_ids(make_client):
    client = make_client(lambda rq: embed_response())
    with client:
        r = client.post("/v1/embeddings", json={"model": "mistral-embed", "input": [[1, 2]]})
        assert r.status_code == 400
        assert "token-id" in r.json()["error"]["message"]


def test_embeddings_failover(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        return embed_response() if len(calls) > 1 else httpx.Response(429, json={})

    client = make_client(handler)
    with client:
        assert client.post("/v1/embeddings", json=EMBED_BODY).status_code == 200
    assert len(calls) == 2


# ---------- moderations ----------

def test_moderations_adds_flagged_and_aliases(make_client):
    client = make_client(lambda rq: moderation_response())
    with client:
        body = client.post("/v1/moderations", json={"input": "I want to hurt people"}).json()
    result = body["results"][0]
    assert result["flagged"] is True
    assert result["categories"]["violence"] is True
    assert result["category_scores"]["violence"] == 0.95


def test_moderations_requires_input(make_client):
    client = make_client(lambda rq: moderation_response())
    with client:
        assert client.post("/v1/moderations", json={}).status_code == 400


# ---------- 调用方鉴权与配额 ----------

def test_static_api_key_enforced(make_client):
    client = make_client(lambda rq: embed_response(), **{"client_api_key": "sk-test"})
    with client:
        assert client.post("/v1/embeddings", json=EMBED_BODY).status_code == 401
        assert client.post("/v1/embeddings", json=EMBED_BODY,
                           headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/v1/embeddings", json=EMBED_BODY,
                           headers={"Authorization": "Bearer sk-test"}).status_code == 200


def test_issued_key_enables_auth_and_works(make_client):
    client = make_client(lambda rq: embed_response())
    with client:
        assert client.post("/v1/embeddings", json=EMBED_BODY).status_code == 200  # 无密钥时放行
        raw = client.post("/admin/keys", json={"name": "t"}).json()["key"]
        assert client.post("/v1/embeddings", json=EMBED_BODY).status_code == 401
        assert client.post("/v1/embeddings", json=EMBED_BODY,
                           headers={"Authorization": f"Bearer {raw}"}).status_code == 200


def test_missing_key_message_is_actionable(make_client):
    client = make_client(lambda rq: embed_response(), **{"client_api_key": "sk-test"})
    with client:
        err = client.post("/v1/embeddings", json=EMBED_BODY).json()["error"]
    assert "Authorization: Bearer" in err["message"]
    assert err["type"] == "authentication_error"


def test_key_model_allowlist_enforced(make_client):
    client = make_client(lambda rq: embed_response())
    with client:
        created = client.post("/admin/keys", json={
            "name": "t", "allowed_models": ["mistral-small-latest"]}).json()
        raw = created["key"]
        auth = {"Authorization": f"Bearer {raw}"}
        r = client.post("/v1/embeddings", json=EMBED_BODY, headers=auth)
        assert r.status_code == 403
        assert "not allowed" in r.json()["error"]["message"]


def test_key_rpm_limit_enforced(make_client):
    client = make_client(lambda rq: embed_response())
    with client:
        raw = client.post("/admin/keys", json={"name": "t", "rpm_limit": 2}).json()["key"]
        auth = {"Authorization": f"Bearer {raw}"}
        assert client.post("/v1/embeddings", json=EMBED_BODY, headers=auth).status_code == 200
        assert client.post("/v1/embeddings", json=EMBED_BODY, headers=auth).status_code == 200
        r = client.post("/v1/embeddings", json=EMBED_BODY, headers=auth)
        assert r.status_code == 429
        assert r.json()["error"]["type"] == "rate_limit_error"


def test_usage_attributed_to_client_key(make_client):
    client = make_client(lambda rq: embed_response())
    with client:
        created = client.post("/admin/keys", json={"name": "t"}).json()
        raw, key_id = created["key"], created["info"]["id"]
        client.post("/v1/embeddings", json=EMBED_BODY,
                    headers={"Authorization": f"Bearer {raw}"})
        store = client.app.state.ctx.store
        store.flush()
        rows = [r for r in store.export_rows(1) if r["client_key"] == key_id]
    assert len(rows) == 1 and rows[0]["endpoint"] == "/v1/embeddings"
