"""Protocol and access-control tests for the standalone Wiki MCP service."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from deerflow.config.app_config import reset_app_config

SERVICE = Path(__file__).resolve().parents[2] / "services" / "regional_wiki_mcp" / "server.py"
spec = importlib.util.spec_from_file_location("regional_wiki_mcp_service", SERVICE)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

TOKEN = "a" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def client():
    with TestClient(service.create_app(str(SERVICE.parent), TOKEN), base_url="http://127.0.0.1:8765") as client:
        yield client
    reset_app_config()


def rpc(client, method, params=None, headers=HEADERS):
    return client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_authentication_covers_all_requests(client):
    assert rpc(client, "tools/list", headers={"Accept": HEADERS["Accept"]}).status_code == 401
    assert rpc(client, "tools/list", headers={**HEADERS, "Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/mcp/").status_code == 401


def test_lists_and_calls_four_existing_tools(client, monkeypatch):
    names = ("search_llm_wiki", "read_llm_wiki_page", "read_llm_wiki_evidence", "get_llm_wiki_release_state")
    for name in names:
        monkeypatch.setattr(service.wiki, name, SimpleNamespace(ainvoke=AsyncMock(return_value=json.dumps({"status": "ok"}))))
    result = rpc(client, "tools/list").json()
    assert {item["name"] for item in result["result"]["tools"]} == set(names)
    arguments = [
        {"query": "产品"},
        {"release_id": "r", "page_id": "p"},
        {"release_id": "r", "page_id": "p", "evidence_id": "e"},
        {},
    ]
    for name, args in zip(names, arguments, strict=True):
        response = rpc(client, "tools/call", {"name": name, "arguments": args})
        assert response.status_code == 200
        assert json.loads(response.json()["result"]["content"][0]["text"]) == {"status": "ok"}
        getattr(service.wiki, name).ainvoke.assert_awaited_once()


def test_rejects_bad_configuration():
    with pytest.raises(ValueError):
        service.create_app("relative", TOKEN)
    with pytest.raises(ValueError):
        service.create_app(str(SERVICE.parent), "short")
