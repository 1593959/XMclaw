from __future__ import annotations

from xmclaw.cognition.tool_history import ToolHistoryProcessor


def test_tool_history_processor_compacts_large_content() -> None:
    processor = ToolHistoryProcessor(max_preview_chars=20)
    content = "x" * 250
    entry = processor.from_output(
        step_id="s1",
        step_index=0,
        tool_name="bash",
        ok=True,
        output={"content": content},
        latency_ms=12.5,
    )

    assert entry.step_id == "s1"
    assert entry.tool_name == "bash"
    assert entry.content_chars == 250
    assert entry.truncated is True
    assert entry.content_preview == ("x" * 200) + "...[truncated]"
    assert entry.latency_ms == 12.5


def test_tool_history_processor_json_fallback() -> None:
    entry = ToolHistoryProcessor().from_output(
        step_id="s1",
        step_index=0,
        tool_name="custom",
        ok=False,
        output={"rows": [1, 2]},
        error="boom",
    )

    d = entry.to_dict()
    assert d["ok"] is False
    assert d["error"] == "boom"
    assert '"rows": [1, 2]' in d["content_preview"]
