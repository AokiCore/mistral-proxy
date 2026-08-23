# -*- coding: utf-8 -*-
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from core.config import Settings  # noqa: E402
from core.store import UsageStore  # noqa: E402

RATE_HEADERS = {
    "x-ratelimit-limit-tokens-minute": "50000",
    "x-ratelimit-remaining-tokens-minute": "49000",
    "x-ratelimit-limit-req-minute": "50",
    "x-ratelimit-remaining-req-minute": "49",
}

MODEL_LIST = {"object": "list", "data": [
    {"id": "mistral-small-latest", "object": "model", "created": 1, "owned_by": "mistralai",
     "name": "mistral-small-2603", "description": "Mistral Small 4.",
     "max_context_length": 262144, "aliases": ["mistral-small-2603"], "deprecation": None,
     "capabilities": {"completion_chat": True, "reasoning": True, "vision": True,
                      "function_calling": True, "ocr": False, "moderation": False}},
    {"id": "mistral-large-latest", "object": "model", "created": 1, "owned_by": "mistralai",
     "name": "mistral-large", "description": "", "max_context_length": 131072,
     "aliases": [], "deprecation": None,
     "capabilities": {"completion_chat": True, "reasoning": False, "vision": False,
                      "function_calling": True}},
    {"id": "mistral-embed", "object": "model", "created": 1, "owned_by": "mistralai",
     "name": "mistral-embed", "description": "", "max_context_length": 8192,
     "aliases": [], "deprecation": None,
     "capabilities": {"completion_chat": False, "reasoning": False}},
]}


def seed_accounts(db_path: str, emails) -> None:
    """按当前 schema 播种：账号凭据 + 每号一个组织（api_key 在 Org 上）。"""
    store = UsageStore(db_path, start_writer=False)
    store.save_account_records([{"email": e} for e in emails])
    store.save_org_records([
        {"email": e, "org_id": f"org-{i}", "api_key": f"key-{e}"}
        for i, e in enumerate(emails)])
    store.close()


@pytest.fixture
def make_client(tmp_path):
    """构造一个把上游打到 MockTransport 的 TestClient。"""
    created = []

    def _make(handler, emails=("a@x.com", "b@x.com"), **overrides):
        db_path = str(tmp_path / f"test{len(created)}.db")
        seed_accounts(db_path, emails)
        # 大多数用例不关心管理台登录，默认关掉；测认证的用例显式打开
        overrides.setdefault("auth_enabled", False)
        overrides.setdefault("keys_file", None)
        settings = Settings(db_path=db_path, state_save_interval=3600, **overrides)
        settings.validate()

        def routed(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models") and request.method == "GET":
                return httpx.Response(200, json=MODEL_LIST)
            return handler(request)

        transport = httpx.MockTransport(routed)
        app = create_app(settings,
                         client_factory=lambda s: httpx.AsyncClient(transport=transport))
        client = TestClient(app)
        created.append(client)
        return client

    yield _make
    for c in created:
        c.close()


def sse(*events: str) -> bytes:
    return ("".join(f"data: {e}\n\n" for e in events) + "data: [DONE]\n\n").encode("utf-8")


def thinking_chunk(text: str) -> dict:
    return {"type": "thinking", "thinking": [{"type": "text", "text": text}], "closed": True}
