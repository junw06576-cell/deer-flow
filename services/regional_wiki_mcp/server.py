"""Standalone, read-only MCP adapter for the Regional LLM Wiki tools."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from deerflow.config.app_config import set_app_config
from deerflow.tools.builtins import regional_llm_wiki_tool as wiki


class BearerAuth:
    """Authenticate every HTTP request before it reaches the MCP server."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            values = [value for key, value in headers if key.lower() == b"authorization"]
            expected = b"Bearer " + self.token
            if len(values) != 1 or not secrets.compare_digest(values[0], expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(runtime_root: str, token: str) -> ASGIApp:
    if not Path(runtime_root).is_absolute():
        raise ValueError("WIKI_RUNTIME_ROOT must be an absolute path")
    if len(token) < 32 or not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("WIKI_MCP_TOKEN must contain at least 32 printable ASCII characters without spaces")

    # This process needs no DeerFlow secrets, models, or sandbox configuration.
    set_app_config(SimpleNamespace(regional_llm_wiki={"enabled": True, "runtime_root": runtime_root}))
    server = FastMCP(
        "regional-llm-wiki",
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "172.16.0.192:18765"],
            allowed_origins=[],
        ),
    )

    @server.tool()
    async def search_llm_wiki(query: str, domain: str | None = None, limit: int = 5) -> str:
        """Search the index; read the returned page before stating facts."""
        return await wiki.search_llm_wiki.ainvoke({"query": query, "domain": domain, "limit": limit})

    @server.tool()
    async def read_llm_wiki_page(release_id: str, page_id: str) -> str:
        """Read a page using IDs returned by search_llm_wiki."""
        return await wiki.read_llm_wiki_page.ainvoke({"release_id": release_id, "page_id": page_id})

    @server.tool()
    async def read_llm_wiki_evidence(release_id: str, page_id: str, evidence_id: str, locator: str | None = None) -> str:
        """Read evidence declared by the selected page, optionally at an allowed locator."""
        return await wiki.read_llm_wiki_evidence.ainvoke({"release_id": release_id, "page_id": page_id, "evidence_id": evidence_id, "locator": locator})

    @server.tool()
    async def get_llm_wiki_release_state() -> str:
        """Return basic metadata for the currently published release."""
        return await wiki.get_llm_wiki_release_state.ainvoke({})

    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app):
        async with server.session_manager.run():
            yield

    return BearerAuth(Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=lifespan), token)


def app_factory() -> ASGIApp:
    return create_app(os.environ.get("WIKI_RUNTIME_ROOT", ""), os.environ.get("WIKI_MCP_TOKEN", ""))
