"""Built-in tools — ``file_read`` and ``file_write`` for the Phase 1 demo.

Phase 1: stub. Full impl lands with the ``read_and_summarize`` demo skill.
"""
from __future__ import annotations

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class BuiltinTools(ToolProvider):
    def list_tools(self) -> list[ToolSpec]:
        # Phase 1: expose file_read + file_write once implemented.
        return []

    async def invoke(self, call: ToolCall) -> ToolResult:  # noqa: ARG002
        raise NotImplementedError("Phase 1")
