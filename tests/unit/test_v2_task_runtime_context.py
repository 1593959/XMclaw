from dataclasses import dataclass, field

import pytest

from xmclaw.cognition.artifact_ledger import ArtifactLedgerStore, event_to_artifacts
from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.task_runtime_context import build_task_runtime_context
from xmclaw.providers.llm.base import LLMProvider, LLMResponse, Message, Pricing


@dataclass
class _RecordingLLM(LLMProvider):
    last_messages: list[Message] = field(default_factory=list)

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.last_messages = list(messages)
        return LLMResponse(content="ok", tool_calls=())

    async def stream(self, messages, tools=None, *, cancel=None):
        yield  # type: ignore[misc]

    @property
    def tool_call_shape(self):
        from xmclaw.core.ir import ToolCallShape
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


def test_task_runtime_context_renders_artifacts_and_strategy(tmp_path) -> None:
    store = ArtifactLedgerStore(tmp_path / "artifacts.db")
    store.add_many(event_to_artifacts(make_event(
        session_id="s1",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "browser_download",
            "ok": True,
            "expected_side_effects": ["E:\\Downloads\\WeChatSetup.exe"],
        },
    )))

    class _Bus:
        def query(self, **_kwargs):
            return [make_event(
                session_id="s1",
                agent_id="agent",
                type=EventType.ANTI_REQ_VIOLATION,
                payload={
                    "kind": "stuck_loop",
                    "tool": "powershell",
                    "error_signature": "not found",
                    "strategy_decision": "change_plan",
                    "should_retry_same": False,
                    "recommended_action": "try a different search scope",
                },
            )]

    block = build_task_runtime_context(
        session_id="s1",
        artifact_store=store,
        bus=_Bus(),
    )

    assert "<task-runtime-context>" in block
    assert "WeChatSetup.exe" in block
    assert "drive=E:" in block
    assert "decision=change_plan" in block
    assert "retry_same=False" in block


def test_turn_graph_state_carries_skill_discovery_metadata(tmp_path) -> None:
    from xmclaw.daemon.turn_graph_state import build_turn_graph_state
    from xmclaw.skills.discovery import SkillCandidate, SkillDiscoveryDecision

    decision = SkillDiscoveryDecision(
        candidates=(
            SkillCandidate(
                skill_id="frontend.ui-review",
                tool_name="skill_frontend__ui-review",
                title="Frontend UI review",
                description="Review UI",
            ),
        ),
        tool_specs=(),
        events=(),
        system_block="",
        skip_reasons=("candidate_not_applicable_to_task",),
        recommended_browse_query="review React UI",
    )

    state = build_turn_graph_state(
        session_id="s1",
        run_id="r1",
        user_message="review React UI",
        skill_discovery=decision,
        prompt_memory_pack_present=True,
    )

    meta = state.snapshot()["metadata"]["skill_discovery"]
    assert meta["matched"] is True
    assert meta["candidate_count"] == 1
    assert meta["candidates"][0]["skill_id"] == "frontend.ui-review"
    assert meta["skip_reasons"] == ["candidate_not_applicable_to_task"]
    assert meta["required_action"] == "call_skill_decision_then_use_or_skip"
    assert meta["must_browse_catalog"] is False


def test_turn_graph_state_carries_memory_and_tool_review_metadata() -> None:
    from xmclaw.daemon.turn_graph_state import build_turn_graph_state

    state = build_turn_graph_state(
        session_id="s1",
        run_id="r1",
        user_message="继续任务",
        memory_decisions=[{
            "action": "search",
            "query": "历史失败",
            "reason": "路径不确定",
        }],
        tool_reviews=[{
            "tool": "bash",
            "strategy_decision": "query_memory",
            "should_retry_same": False,
        }],
    )

    meta = state.snapshot()["metadata"]
    assert meta["memory_decisions"][0]["action"] == "search"
    assert meta["tool_reviews"][0]["strategy_decision"] == "query_memory"


@pytest.mark.asyncio
async def test_agent_loop_injects_task_runtime_context(tmp_path) -> None:
    store = ArtifactLedgerStore(tmp_path / "artifacts.db")
    store.add_many(event_to_artifacts(make_event(
        session_id="s-runtime",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "browser_download",
            "ok": True,
            "expected_side_effects": ["E:\\Downloads\\installer.exe"],
        },
    )))
    bus = InProcessEventBus()
    graph_events = []

    async def _capture(event):
        graph_events.append(event)

    bus.subscribe(lambda e: e.type == EventType.GRAPH_STATE_UPDATED, _capture)
    llm = _RecordingLLM()
    loop = AgentLoop(llm=llm, bus=bus, memory=None, agent_id="test")
    loop._artifact_ledger_store = store

    result = await loop.run_turn("s-runtime", "继续安装")

    await bus.drain()

    assert result.ok
    system_msg = next(m for m in llm.last_messages if m.role == "system")
    user_msg = next(m for m in llm.last_messages if m.role == "user")
    assert "<prompt-memory-pack>" in system_msg.content
    assert "<task-runtime-context>" in system_msg.content
    assert "installer.exe" in system_msg.content
    assert "继续安装" in user_msg.content
    assert "<task-runtime-context>" not in user_msg.content
    assert graph_events
    assert graph_events[0].payload["phase"] == "turn_started"
    assert graph_events[0].payload["artifacts"] == 1
    assert graph_events[0].payload["metadata"]["prompt_memory_pack_present"] is True
