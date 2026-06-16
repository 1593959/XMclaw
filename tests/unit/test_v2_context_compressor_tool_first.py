"""Wave-33: ContextCompressor ``tool_first`` strategy tests."""
from __future__ import annotations

from typing import Optional

import pytest

from xmclaw.context.compressor import ContextCompressor
from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.base import Message


def _tc(name: str, id: str) -> ToolCall:
    return ToolCall(
        name=name, args={"path": f"{name}.txt"}, provenance="synthetic", id=id,
    )


async def _stub_summarize(prompt: str, max_tokens: int) -> Optional[str]:
    return "SUMMARY"


@pytest.mark.asyncio
async def test_tool_first_collapses_middle_tool_group() -> None:
    """``_compress_tool_pairs_first`` replaces a tool-use pair inside the
    compressible range with a single synthetic assistant message.
    """
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="task 1"),
        Message(role="assistant", content="call 1", tool_calls=(_tc("file_read", "c1"),)),
        Message(role="tool", content="ONE" * 2000, tool_call_id="c1"),
        Message(role="user", content="task 2"),
        Message(role="assistant", content="final answer"),
    ]

    cc = ContextCompressor(
        model="test",
        summarize_call=_stub_summarize,
        context_length=4096,
        strategy="tool_first",
    )
    out = cc._compress_tool_pairs_first(messages, start=1, end=5)

    # The assistant + tool pair at indices 2-3 should become one message.
    assert len(out) == len(messages) - 1
    assert out[2].role == "assistant"
    assert "[Earlier tool batch]" in (out[2].content or "")
    assert not out[2].tool_calls
    # User messages and final assistant are untouched.
    assert out[1].content == "task 1"
    assert out[3].content == "task 2"
    assert out[4].content == "final answer"
