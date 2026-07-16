"""Compatibility shim — prefer ``mcp_server.agent``.

The LangGraph graph now lives under ``mcp_server/agent/``. This package
re-exports the public entrypoint for older imports.
"""

from mcp_server.agent.graph import run_analysis

__all__ = ["run_analysis"]
