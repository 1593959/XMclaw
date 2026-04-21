"""ToolProvider ABC."""
from __future__ import annotations

import abc

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec


class ToolProvider(abc.ABC):
    @abc.abstractmethod
    def list_tools(self) -> list[ToolSpec]: ...

    @abc.abstractmethod
    async def invoke(self, call: ToolCall) -> ToolResult: ...
