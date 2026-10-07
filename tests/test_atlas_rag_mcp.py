"""Opt-in cross-process integration test for the standalone Atlas RAG MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.mcp import client as mcp_client

ATLAS_ROOT = Path(os.environ.get("PRAXIS_ATLAS_RAG_ROOT", ""))
ATLAS_PYTHON = ATLAS_ROOT / ".venv" / "Scripts" / "python.exe"

pytestmark = pytest.mark.skipif(
    not (ATLAS_ROOT / "src" / "rag_mcp" / "server.py").is_file() or not ATLAS_PYTHON.is_file(),
    reason="Set PRAXIS_ATLAS_RAG_ROOT to an Atlas RAG checkout with its .venv",
)


def test_atlas_rag_stdio_server_is_discovered_and_called() -> None:
    resources = mcp_client.start_external_tools(
        (
            mcp_client.McpServerConfig(
                name="atlas",
                command=str(ATLAS_PYTHON),
                args=("-m", "rag_mcp.server"),
                cwd=str(ATLAS_ROOT),
                env={"RAG_BACKEND": "mock", "RAG_NAMESPACE": "default"},
                timeout=20.0,
            ),
        )
    )
    try:
        assert [tool.name for tool in resources] == ["mcp_atlas_search"]
        tool = resources[0]
        assert tool.trace_origin == {"kind": "mcp", "server": "atlas", "tool": "search"}

        rendered = tool.handler(
            query="RRF 怎么融合 BM25 和 Dense？",
            retrieval="hybrid",
            rerank="none",
            top_k=2,
        )
        result = json.loads(rendered)

        assert result["evidence_status"] == "found"
        assert result["results"]
        assert result["results"][0]["source"]
        assert result["results"][0]["chunk_id"]
    finally:
        resources.close()
