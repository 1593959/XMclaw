"""Internal Tool-Call IR — the single format every provider translates to/from.

See docs/V2_DEVELOPMENT.md §1.4. ``decode_from_provider`` MUST return a
structured ``ToolCall`` or ``None`` — never a string that looks like one.
That rule is anti-requirement #1 in code form.
"""
from xmclaw.core.ir.toolcall import ToolCall, ToolCallShape, ToolResult, ToolSpec

__all__ = ["ToolCall", "ToolCallShape", "ToolResult", "ToolSpec"]
