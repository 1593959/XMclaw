"""B-300: turn-local skill_browse nudge when prefilter drops all skills.

Real-data 2026-05-07 (probe_b299_chain.py against the live daemon
with 404 installed skills + kimi k2.6):

  Bucket A (vague CJK):  0/4 turns invoked skill_browse
  Bucket B (clear keywords): 2/3 directly hit the matched skill_*

The static B-299 system-prompt mention of skill_browse sits ~5K tokens
deep in an 8K-token system prompt, fully cached by Anthropic's prompt
cache. By the time the LLM gets to tool selection it's been diluted by
everything else; on a vague query that yields zero token-overlap matches,
the LLM defaults to bash/list_dir.

B-300 fix: when the prefilter actually drops EVERY real skill (registry
has skills, but none scored > 0 against this query), append a short
turn-local hint to the user message pointing at skill_browse. Fires
only on the exact case where it matters.

These tests pin the trigger conditions so the nudge doesn't either
(a) miss the case it's there for, or (b) spam every turn.
"""
from __future__ import annotations

import pytest

from dataclasses import dataclass

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec
from xmclaw.providers.llm.base import (
    LLMProvider, LLMResponse, Message, Pricing,
)
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.daemon.agent_loop import AgentLoop


@dataclass
class _CaptureLLM(LLMProvider):
    """Capture the messages list passed to the LLM so we can inspect
    what nudge (if any) ended up in the user message. Returns a
    one-shot empty response so the agent loop exits after one hop."""

    model: str = "test"
    captured_user_content: str | None = None

    async def stream(self, messages, tools=None, *, cancel=None):  # pragma: no cover
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        for m in messages:
            if getattr(m, "role", None) == "user":
                # Last user message wins (multi-turn would have
                # multiple but we only run one turn).
                self.captured_user_content = getattr(m, "content", "") or ""
        return LLMResponse(content="ok", tool_calls=())

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


class _StaticToolProvider(ToolProvider):
    """ToolProvider that returns a fixed list of specs. Used to
    simulate prefilter-relevant scenarios."""

    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, ok=True, content=None,
            error=None, latency_ms=0.0,
        )


def _spec(name: str, desc: str = "") -> ToolSpec:
    return ToolSpec(
        name=name, description=desc,
        parameters_schema={"type": "object"},
    )


# ── trigger condition: prefilter drops all skill_* ──────────────────


@pytest.mark.asyncio
async def test_b300_hint_fires_when_no_skill_survives_prefilter() -> None:
    """Above min_skills_to_filter (30) registered skills + a query
    that scores 0 against all of them → prefilter returns no real
    skills → the nudge MUST land in the user message."""
    bus = InProcessEventBus()
    llm = _CaptureLLM()
    # 35 skills, all with English descriptions that won't match a
    # CJK query.
    specs: list[ToolSpec] = [
        _spec("bash", "execute shell command"),  # non-skill, always passes
        _spec("skill_browse", "meta discovery tool"),
        *[_spec(f"skill_eng-only-{i}", "English-only skill description")
          for i in range(35)],
    ]
    agent = AgentLoop(
        llm=llm, bus=bus, tools=_StaticToolProvider(specs),
    )
    await agent.run_turn("test-sess", "帮我看看怎么写文档")
    await bus.drain()

    assert llm.captured_user_content is not None
    assert "[turn hint]" in llm.captured_user_content, (
        "B-300 nudge missing — vague CJK query against English skill "
        "registry is exactly the case it should fire on"
    )
    assert "skill_browse" in llm.captured_user_content


@pytest.mark.asyncio
async def test_b300_hint_silent_when_skills_match_query() -> None:
    """Strong keyword match → prefilter keeps relevant skills →
    nudge MUST NOT fire (would just add noise to a turn that's
    already on the right track)."""
    bus = InProcessEventBus()
    llm = _CaptureLLM()
    specs: list[ToolSpec] = [
        _spec("bash", "execute shell command"),
        _spec("skill_browse", "meta discovery tool"),
        # 30+ skills so prefilter actually runs (below 30 it
        # short-circuits to no-op which would also pass this test
        # but for the wrong reason).
        *[_spec(f"skill_filler-{i}", "filler description") for i in range(30)],
        _spec("skill_git-commit", "Run git commit with conventional message"),
    ]
    agent = AgentLoop(
        llm=llm, bus=bus, tools=_StaticToolProvider(specs),
    )
    await agent.run_turn("test-sess", "git commit message please")
    await bus.drain()

    assert llm.captured_user_content is not None
    assert "[turn hint]" not in llm.captured_user_content


@pytest.mark.asyncio
async def test_b300_hint_silent_below_filter_threshold() -> None:
    """Tiny registry (< min_skills_to_filter=30) → prefilter is a
    no-op → no nudge. Even if NO skill matches, with so few skills
    the LLM can scan them itself without browsing."""
    bus = InProcessEventBus()
    llm = _CaptureLLM()
    specs: list[ToolSpec] = [
        _spec("bash", "execute shell command"),
        _spec("skill_browse", "meta discovery tool"),
        # Only 5 skills — prefilter will return all of them
        # untouched, regardless of query.
        *[_spec(f"skill-{i}", "filler") for i in range(5)],
    ]
    agent = AgentLoop(
        llm=llm, bus=bus, tools=_StaticToolProvider(specs),
    )
    await agent.run_turn("test-sess", "随便问一下")
    await bus.drain()

    assert llm.captured_user_content is not None
    # Either no hint, or some hint but NOT the B-300 one. The
    # specific marker we added shouldn't show up.
    assert "[turn hint]" not in llm.captured_user_content


@pytest.mark.asyncio
async def test_b300_hint_silent_with_zero_registered_skills() -> None:
    """Echo-mode / no SkillToolProvider in the agent's tool stack →
    registry_total = 0 → no nudge (telling the LLM about a
    discovery tool that doesn't help is just noise)."""
    bus = InProcessEventBus()
    llm = _CaptureLLM()
    specs: list[ToolSpec] = [
        _spec("bash", "execute shell command"),
        # No skill_browse, no skill_*. Pure echo-mode.
    ]
    agent = AgentLoop(
        llm=llm, bus=bus, tools=_StaticToolProvider(specs),
    )
    await agent.run_turn("test-sess", "anything")
    await bus.drain()

    assert llm.captured_user_content is not None
    assert "[turn hint]" not in llm.captured_user_content


@pytest.mark.asyncio
async def test_b300_hint_text_mentions_skill_count() -> None:
    """The nudge cites the actual registry size so the LLM sees
    'this is a 404-skill registry' specifically — concrete number
    is more compelling than 'you have many skills'."""
    bus = InProcessEventBus()
    llm = _CaptureLLM()
    skill_count = 35
    specs: list[ToolSpec] = [
        _spec("bash"),
        _spec("skill_browse", "meta"),
        *[_spec(f"skill_x-{i}", "irrelevant English desc")
          for i in range(skill_count)],
    ]
    agent = AgentLoop(
        llm=llm, bus=bus, tools=_StaticToolProvider(specs),
    )
    await agent.run_turn("test-sess", "完全不匹配的 CJK 查询")
    await bus.drain()

    assert llm.captured_user_content is not None
    assert str(skill_count) in llm.captured_user_content
