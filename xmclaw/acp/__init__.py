"""ACP — Agent Client Protocol adapter for IDE integration.

Exposes XMclaw capabilities over JSON-RPC on stdio so VS Code / Zed /
JetBrains plugins can use XMclaw as a backend.
"""
from __future__ import annotations

from xmclaw.acp.server import AcpServer

__all__ = ["AcpServer"]
