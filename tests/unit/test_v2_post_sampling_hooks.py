"""B-112: post-sampling hook framework + ExtractMemoriesHook.

Pins:
  * HookRegistry.dispatch fires every enabled hook concurrently
  * disabled hooks are skipped
  * a hook that raises does NOT abort the chain
  * ExtractMemoriesHook respects ``extract_memories.enabled`` gate
  * extracted facts land under ## Auto-extracted in MEMORY.md
  * empty / malformed LLM response → no write
  * B-168 ExtractLessonsHook routes three buckets to three files,
    default ON, respects per-bucket cap, and falls through cleanly
    on malformed payloads.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.daemon.post_sampling_hooks import (
    ExtractLessonsHook,
    ExtractMemoriesHook,
    HookContext,
    HookRegistry,
    PostSamplingHook,
    _strip_leading_date,
    build_default_registry,
)


class _StubLLM:
    def __init__(self, response: str) -> None:
        self._r = response
        self.call_count = 0

    async def complete(self, messages, tools=None):
        self.call_count += 1

        class _Resp:
            def __init__(self, c: str) -> None:
                self.content = c

        return _Resp(self._r)


def _ctx(persona_dir, cfg=None, llm=None) -> HookContext:
    return HookContext(
        session_id="s",
        agent_id="main",
        user_message="hi",
        assistant_response="hello",
        history=[],
        llm=llm or _StubLLM('{"facts": []}'),
        persona_dir=persona_dir,
        cfg=cfg or {},
    )


# ── HookRegistry ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_fires_enabled_hooks(tmp_path: Path) -> None:
    fired: list[str] = []

    class _A(PostSamplingHook):
        id = "a"
        async def run(self, ctx): fired.append("a")

    class _B(PostSamplingHook):
        id = "b"
        async def run(self, ctx): fired.append("b")

    reg = HookRegistry()
    reg.register(_A())
    reg.register(_B())
    await reg.dispatch(_ctx(tmp_path))
    assert sorted(fired) == ["a", "b"]


@pytest.mark.asyncio
async def test_dispatch_skips_disabled_hooks(tmp_path: Path) -> None:
    fired: list[str] = []

    class _A(PostSamplingHook):
        id = "a"
        def is_enabled(self, ctx): return False
        async def run(self, ctx): fired.append("a")

    class _B(PostSamplingHook):
        id = "b"
        async def run(self, ctx): fired.append("b")

    reg = HookRegistry()
    reg.register(_A())
    reg.register(_B())
    await reg.dispatch(_ctx(tmp_path))
    assert fired == ["b"]


@pytest.mark.asyncio
async def test_one_hook_raising_doesnt_break_others(tmp_path: Path) -> None:
    fired: list[str] = []

    class _Boom(PostSamplingHook):
        id = "boom"
        async def run(self, ctx): raise RuntimeError("kaboom")

    class _Good(PostSamplingHook):
        id = "good"
        async def run(self, ctx): fired.append("good")

    reg = HookRegistry()
    reg.register(_Boom())
    reg.register(_Good())
    await reg.dispatch(_ctx(tmp_path))
    assert fired == ["good"]


# ── ExtractMemoriesHook ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_disabled_when_config_off(tmp_path: Path) -> None:
    hook = ExtractMemoriesHook()
    ctx = _ctx(tmp_path, cfg={})
    assert hook.is_enabled(ctx) is False


@pytest.mark.asyncio
async def test_extract_disabled_without_persona_dir() -> None:
    hook = ExtractMemoriesHook()
    ctx = _ctx(None, cfg={"evolution": {"memory": {"extract_memories": {"enabled": True}}}})
    assert hook.is_enabled(ctx) is False


@pytest.mark.asyncio
async def test_extract_writes_facts_when_enabled(tmp_path: Path) -> None:
    """Happy path — LLM returns a fact list, hook appends to MEMORY.md
    under the auto-extracted section."""
    llm = _StubLLM('{"facts": ["user prefers tabs over spaces"]}')
    cfg = {"evolution": {"memory": {"extract_memories": {"enabled": True}}}}
    ctx = _ctx(tmp_path, cfg=cfg, llm=llm)
    hook = ExtractMemoriesHook()
    assert hook.is_enabled(ctx) is True
    await hook.run(ctx)
    assert llm.call_count == 1
    body = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "## Auto-extracted" in body
    assert "user prefers tabs over spaces" in body


@pytest.mark.asyncio
async def test_extract_empty_facts_no_write(tmp_path: Path) -> None:
    llm = _StubLLM('{"facts": []}')
    cfg = {"evolution": {"memory": {"extract_memories": {"enabled": True}}}}
    ctx = _ctx(tmp_path, cfg=cfg, llm=llm)
    hook = ExtractMemoriesHook()
    await hook.run(ctx)
    assert not (tmp_path / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_extract_malformed_llm_response_no_write(tmp_path: Path) -> None:
    llm = _StubLLM("not even json")
    cfg = {"evolution": {"memory": {"extract_memories": {"enabled": True}}}}
    ctx = _ctx(tmp_path, cfg=cfg, llm=llm)
    hook = ExtractMemoriesHook()
    await hook.run(ctx)
    assert not (tmp_path / "MEMORY.md").exists()


# ── ExtractLessonsHook (B-168) ────────────────────────────────────────


def _lessons_payload(
    *, workflow=None, tool_quirks=None, failure_modes=None,
) -> str:
    import json
    return json.dumps({
        "workflow": workflow or [],
        "tool_quirks": tool_quirks or [],
        "failure_modes": failure_modes or [],
    })


@pytest.mark.asyncio
async def test_lessons_default_enabled_with_persona(tmp_path: Path) -> None:
    """B-168: unlike ExtractMemoriesHook, this hook is ON by default —
    that's the whole point of the user complaint that prompted it
    (经验教训自己总结)."""
    hook = ExtractLessonsHook()
    ctx = _ctx(tmp_path, cfg={})
    assert hook.is_enabled(ctx) is True


@pytest.mark.asyncio
async def test_lessons_disabled_without_persona() -> None:
    hook = ExtractLessonsHook()
    ctx = _ctx(None)
    assert hook.is_enabled(ctx) is False


@pytest.mark.asyncio
async def test_lessons_disabled_via_config(tmp_path: Path) -> None:
    hook = ExtractLessonsHook()
    cfg = {"evolution": {"memory": {"extract_lessons": {"enabled": False}}}}
    ctx = _ctx(tmp_path, cfg=cfg)
    assert hook.is_enabled(ctx) is False


@pytest.mark.asyncio
async def test_lessons_routes_to_three_files(tmp_path: Path) -> None:
    """One LLM call → three files updated based on bucket key."""
    payload = _lessons_payload(
        workflow=["grep first to narrow scope, then read"],
        tool_quirks=["ruff lints static/ JS, pass --type=python"],
        failure_modes=["build always breaks if setuptools missing"],
    )
    llm = _StubLLM(payload)
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)

    assert llm.call_count == 1

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Auto-extracted" in agents
    assert "grep first to narrow scope" in agents

    tools = (tmp_path / "TOOLS.md").read_text(encoding="utf-8")
    assert "## Auto-extracted" in tools
    assert "ruff lints static/" in tools

    memory = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "## Failure Modes" in memory
    assert "build always breaks" in memory


@pytest.mark.asyncio
async def test_lessons_per_bucket_cap_enforced(tmp_path: Path) -> None:
    """A spammy LLM dumping 10 'workflow' lessons → only first 3 kept."""
    payload = _lessons_payload(
        workflow=[f"lesson #{i}" for i in range(10)],
    )
    llm = _StubLLM(payload)
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    kept = sum(1 for i in range(10) if f"lesson #{i}" in text)
    assert kept == hook.MAX_PER_BUCKET == 3


@pytest.mark.asyncio
async def test_lessons_empty_payload_no_write(tmp_path: Path) -> None:
    llm = _StubLLM(_lessons_payload())
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "TOOLS.md").exists()
    assert not (tmp_path / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_lessons_malformed_json_no_write(tmp_path: Path) -> None:
    llm = _StubLLM("not even json")
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)
    assert not (tmp_path / "AGENTS.md").exists()


@pytest.mark.asyncio
async def test_lessons_strips_json_fence(tmp_path: Path) -> None:
    """LLM might wrap output in ```json ... ``` — strip and parse."""
    payload = "```json\n" + _lessons_payload(
        workflow=["wrapped fine"],
    ) + "\n```"
    llm = _StubLLM(payload)
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "wrapped fine" in agents


@pytest.mark.asyncio
async def test_lessons_partial_buckets_only_writes_used(tmp_path: Path) -> None:
    """Only ``workflow`` populated → only AGENTS.md should appear."""
    payload = _lessons_payload(workflow=["only workflow today"])
    llm = _StubLLM(payload)
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)
    assert (tmp_path / "AGENTS.md").is_file()
    assert not (tmp_path / "TOOLS.md").exists()
    assert not (tmp_path / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_lessons_llm_failure_no_crash(tmp_path: Path) -> None:
    class _Boom:
        async def complete(self, messages, tools=None):
            raise RuntimeError("upstream LLM dead")
    ctx = _ctx(tmp_path, llm=_Boom())
    hook = ExtractLessonsHook()
    # Must NOT raise — chain integrity comes first.
    await hook.run(ctx)
    assert not (tmp_path / "AGENTS.md").exists()


# ── default registry wiring ───────────────────────────────────────────


def test_default_registry_has_both_hooks() -> None:
    """B-168: build_default_registry must include both extractors so
    the daemon's lifespan picks them up automatically."""
    reg = build_default_registry()
    ids = {h.id for h in reg.hooks()}
    assert ids == {"extract_memories", "extract_lessons"}


# ── B-179 _strip_leading_date helper ────────────────────────────────


def test_strip_leading_date_removes_iso_prefix() -> None:
    assert _strip_leading_date("2026-05-02: real content") == "real content"
    assert _strip_leading_date("2026-05-02 real content") == "real content"


def test_strip_leading_date_handles_double_prefix() -> None:
    """The exact bug joint audit caught: LLM extractor includes its
    own date inside the lesson string, then we prepend ours. Without
    stripping the result is '- 2026-05-02: 2026-05-02: foo'."""
    assert (
        _strip_leading_date("2026-05-02: 2026-05-02: foo bar")
        == "foo bar"
    )


def test_strip_leading_date_handles_parenthetical_tag() -> None:
    """LLM has been seen producing '2026-05-02 (精炼): real content'."""
    assert (
        _strip_leading_date("2026-05-02 (精炼): real content")
        == "real content"
    )


def test_strip_leading_date_preserves_non_dated_text() -> None:
    assert _strip_leading_date("regular lesson text") == "regular lesson text"
    assert _strip_leading_date("") == ""


@pytest.mark.asyncio
async def test_lessons_double_date_prefix_collapses(tmp_path: Path) -> None:
    """End-to-end: LLM returns lessons that include a ``YYYY-MM-DD:``
    prefix in the value. The hook must strip it so the on-disk bullet
    has only one date prefix (the canonical one we prepend)."""
    payload = _lessons_payload(
        workflow=["2026-05-02: this is the actual lesson body"],
    )
    llm = _StubLLM(payload)
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # Exactly ONE date prefix per bullet line.
    bullet_lines = [
        ln for ln in text.splitlines() if ln.startswith("- ")
    ]
    for line in bullet_lines:
        # Count "2026-" occurrences in the bullet — must be 1.
        assert line.count("2026-") == 1, f"double-date in: {line}"
    assert "this is the actual lesson body" in text
