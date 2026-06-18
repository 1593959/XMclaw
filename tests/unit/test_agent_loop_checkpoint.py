"""Unit tests for the incremental inflight checkpoint in AgentLoop.

Covers:
  1. Incremental write only contains new messages (after a full checkpoint)
  2. Full checkpoint is triggered every 10 turns
  3. Recovery merge of full + incremental is correct
  4. Background write uses asyncio.to_thread (non-blocking)
  5. mkdir is only called on first write
  6. Accuracy matches old full-write semantics
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Message, Pricing
from xmclaw.providers.llm.base import ToolCallShape


@dataclass
class _DummyLLM(LLMProvider):
    model: str = "dummy"

    async def complete(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=())

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        if False:
            yield  # type: ignore[unreachable]

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@pytest.fixture
def agent():
    bus = InProcessEventBus()
    llm = _DummyLLM()
    return AgentLoop(llm=llm, bus=bus)


@pytest.fixture
def tmp_data_dir(tmp_path):
    inflight_dir = tmp_path / "v2" / "inflight"
    with patch("xmclaw.utils.paths.data_dir", return_value=tmp_path):
        yield inflight_dir


# Helper to make asyncio.to_thread execute synchronously in tests
# so we can inspect file contents without spawning real threads.
def _sync_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


class TestIncrementalCheckpoint:
    async def test_incremental_only_new_messages(self, agent, tmp_data_dir):
        session_id = "test-sid"
        # Build up to turn 10 (no full checkpoint yet, so all messages are
        # written cumulatively into the incremental array).
        for i in range(1, 11):
            messages = [Message(role="user", content=f"msg{j}") for j in range(i)]
            await agent._write_inflight_checkpoint(session_id, messages)

        # Turn 10 is a full checkpoint → incremental should be empty
        main_path = tmp_data_dir / f"{session_id}.json"
        payload = json.loads(main_path.read_text())
        assert payload["checkpoint_at"] == 10
        assert payload["incremental"] == []

        # Turn 11: only 1 new message since the last full checkpoint
        messages = [Message(role="user", content=f"msg{j}") for j in range(11)]
        await agent._write_inflight_checkpoint(session_id, messages)

        payload = json.loads(main_path.read_text())
        assert payload["checkpoint_at"] == 10
        assert len(payload["incremental"]) == 1
        assert payload["incremental"][0]["content"] == "msg10"

        # Turn 12: 2 new messages since the last full checkpoint
        messages = [Message(role="user", content=f"msg{j}") for j in range(12)]
        await agent._write_inflight_checkpoint(session_id, messages)

        payload = json.loads(main_path.read_text())
        assert payload["checkpoint_at"] == 10
        assert len(payload["incremental"]) == 2
        assert payload["incremental"][0]["content"] == "msg10"
        assert payload["incremental"][1]["content"] == "msg11"

    async def test_full_checkpoint_every_10_turns(self, agent, tmp_data_dir):
        session_id = "test-sid"
        for i in range(1, 21):
            messages = [Message(role="user", content=f"msg{j}") for j in range(i)]
            await agent._write_inflight_checkpoint(session_id, messages)

        full_files = sorted(tmp_data_dir.glob(f"{session_id}.full.*.json"))
        assert len(full_files) == 2  # turn 10 and turn 20

        # Verify turn 10 full snapshot
        snap10 = json.loads(full_files[0].read_text())
        assert len(snap10) == 10

        # Verify turn 20 full snapshot
        snap20 = json.loads(full_files[1].read_text())
        assert len(snap20) == 20

        # Current main file should point to turn 20
        main_path = tmp_data_dir / f"{session_id}.json"
        payload = json.loads(main_path.read_text())
        assert payload["checkpoint_at"] == 20
        assert payload["full_checkpoint"] == str(full_files[1])

    async def test_recovery_merge_correct(self, agent, tmp_data_dir):
        session_id = "test-sid"
        for i in range(1, 13):
            messages = [Message(role="user", content=f"msg{j}") for j in range(i)]
            await agent._write_inflight_checkpoint(session_id, messages)

        main_path = tmp_data_dir / f"{session_id}.json"
        payload = json.loads(main_path.read_text())

        # Recovery: full snapshot + incremental messages
        full_path = Path(payload["full_checkpoint"])
        full_snapshot = json.loads(full_path.read_text())
        recovered = full_snapshot + payload["incremental"]

        assert len(recovered) == 12
        for i in range(12):
            assert recovered[i]["content"] == f"msg{i}"

    async def test_background_write_uses_to_thread(self, agent, tmp_data_dir):
        session_id = "test-sid"
        messages = [Message(role="user", content="hello")]

        with patch("xmclaw.daemon.agent_loop.asyncio.to_thread") as mock_to_thread:
            mock_to_thread.return_value = None
            await agent._write_inflight_checkpoint(session_id, messages)
            # Should call to_thread for: mkdir, write_text (full or main), replace (full or main)
            assert mock_to_thread.call_count >= 2

    async def test_mkdir_only_once(self, agent, tmp_data_dir):
        session_id = "test-sid"
        with patch("xmclaw.daemon.agent_loop.asyncio.to_thread", side_effect=_sync_to_thread):
            with patch.object(Path, "mkdir") as mock_mkdir:
                await agent._write_inflight_checkpoint(
                    session_id, [Message(role="user", content="1")]
                )
                assert mock_mkdir.call_count == 1

                await agent._write_inflight_checkpoint(
                    session_id, [Message(role="user", content="2")]
                )
                assert mock_mkdir.call_count == 1

    async def test_accuracy_matches_old_full_write(self, agent, tmp_data_dir):
        """When merged, the incremental payload must equal the old full-write format."""
        session_id = "test-sid"
        messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="u1"),
            Message(role="assistant", content="a1"),
            Message(role="tool", content="t1", tool_call_id="tc1"),
        ]

        # Turn 1 is incremental (checkpoint_at=0, full_checkpoint=None)
        await agent._write_inflight_checkpoint(session_id, messages)

        main_path = tmp_data_dir / f"{session_id}.json"
        payload = json.loads(main_path.read_text())
        if payload["full_checkpoint"]:
            full_snapshot = json.loads(Path(payload["full_checkpoint"]).read_text())
            merged = full_snapshot + payload["incremental"]
        else:
            merged = payload["incremental"]

        # Build the old-style full snapshot for comparison
        from xmclaw.daemon.session_store import _message_to_dict

        old_style = [_message_to_dict(m) for m in messages]

        assert merged == old_style

    async def test_full_and_incremental_accuracy(self, agent, tmp_data_dir):
        """After a full checkpoint, full + incremental must reconstruct the
        complete message list exactly."""
        session_id = "test-sid"

        # 10 turns to trigger a full checkpoint
        for i in range(1, 11):
            messages = [Message(role="user", content=f"msg{j}") for j in range(i)]
            await agent._write_inflight_checkpoint(session_id, messages)

        # 3 more turns
        for i in range(11, 14):
            messages = [Message(role="user", content=f"msg{j}") for j in range(i)]
            await agent._write_inflight_checkpoint(session_id, messages)

        # Compare merged result against the old-style full snapshot
        from xmclaw.daemon.session_store import _message_to_dict

        old_style = [_message_to_dict(m) for m in messages]

        main_path = tmp_data_dir / f"{session_id}.json"
        payload = json.loads(main_path.read_text())
        full_path = Path(payload["full_checkpoint"])
        full_snapshot = json.loads(full_path.read_text())
        merged = full_snapshot + payload["incremental"]

        assert merged == old_style
