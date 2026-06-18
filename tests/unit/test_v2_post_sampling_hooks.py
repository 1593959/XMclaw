"""B-112: post-sampling hook framework + Extract*Hook.

Pins:
  * HookRegistry.dispatch fires every enabled hook concurrently
  * disabled hooks are skipped
  * a hook that raises does NOT abort the chain
  * ExtractMemoriesHook respects ``extract_memories.enabled`` gate
  * extracted facts land under ## Auto-extracted in MEMORY.md
  * empty / malformed LLM response → no write
  * B-168 ExtractLessonsHook routes the original three buckets to
    three files, default ON, respects per-bucket cap, and falls
    through cleanly on malformed payloads.
  * B-303 added ``values`` (SOUL.md) + ``rules`` (LEARNING.md).
  * B-319 added ``preferences`` (USER.md) and re-exported the hook
    as :class:`ExtractFactsHook`. The default registry now ships
    only ``extract_lessons`` (≡ ``extract_facts``); the legacy
    ``extract_memories`` hook is opt-in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.daemon.post_sampling_hooks import (
    ExtractFactsHook,
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
    values=None, rules=None, preferences=None,
) -> str:
    import json
    return json.dumps({
        "workflow": workflow or [],
        "tool_quirks": tool_quirks or [],
        "failure_modes": failure_modes or [],
        # B-303: extended buckets — empty by default keeps existing
        # tests valid (they don't pass values/rules so they stay []).
        "values": values or [],
        "rules": rules or [],
        # B-319: ``preferences`` absorbs the USER.md write path.
        "preferences": preferences or [],
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


def test_default_registry_only_has_extract_lessons() -> None:
    """B-319: build_default_registry now ships ONLY the unified
    ExtractFactsHook (a.k.a. extract_lessons). The legacy
    ExtractMemoriesHook is opt-in — its only output (durable
    failure_modes facts) is now produced by the failure_modes
    bucket of the unified extractor in the same LLM round-trip,
    so leaving it default-on costs an extra LLM call for zero
    new coverage. Pin the change so a future revert lands with a
    clear failure rather than a silent +1 LLM call per turn."""
    reg = build_default_registry()
    ids = {h.id for h in reg.hooks()}
    assert ids == {"extract_lessons"}


def test_extract_facts_hook_alias_points_to_extract_lessons() -> None:
    """B-319: ExtractFactsHook is the forward-compat name. New code
    should reference it; legacy code keeps working via the alias."""
    assert ExtractFactsHook is ExtractLessonsHook


@pytest.mark.asyncio
async def test_lessons_routes_preferences_to_user_md(tmp_path: Path) -> None:
    """B-319: ``preferences`` bucket lands in USER.md under the
    Auto-extracted preferences heading. Validates the legacy-markdown
    code path (persona_store=None) — the DB path is exercised by
    persona_store-level tests."""
    payload = _lessons_payload(
        preferences=[
            "user prefers Chinese for casual chat, English for code",
            "user wants concise answers, no preamble",
        ],
    )
    llm = _StubLLM(payload)
    ctx = _ctx(tmp_path, llm=llm)
    hook = ExtractLessonsHook()
    await hook.run(ctx)

    user_md = (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert "## Auto-extracted preferences" in user_md
    assert "user prefers Chinese for casual chat" in user_md
    assert "user wants concise answers" in user_md


@pytest.mark.asyncio
async def test_lessons_writes_preferences_with_preference_kind(
    tmp_path: Path,
) -> None:
    """B-319: preferences must be DB-tagged ``kind=preference`` (not
    ``kind=lesson``) so the persona_store renderer matches USER.md's
    AUTO_SECTIONS row. We capture the calls to a stub memory provider
    and assert the kind on the metadata."""
    payload = _lessons_payload(
        preferences=["user prefers ruff over black"],
        workflow=["grep before reading huge files"],
    )

    captured: list[dict] = []

    class _StubMem:
        async def upsert_fact(self, *, text, embedding, layer, metadata):
            captured.append({"text": text, "metadata": dict(metadata)})
            return ("row-" + str(len(captured)), False)

    llm = _StubLLM(payload)
    ctx = HookContext(
        session_id="s",
        agent_id="main",
        user_message="hi",
        assistant_response="hello",
        history=[],
        llm=llm,
        persona_dir=tmp_path,
        cfg={},
        memory_provider=_StubMem(),
    )
    hook = ExtractLessonsHook()
    await hook.run(ctx)

    by_kind: dict[str, list[dict]] = {}
    for c in captured:
        by_kind.setdefault(c["metadata"]["kind"], []).append(c)

    # Preference row(s) — kind="preference", no bucket field.
    pref_rows = by_kind.get("preference", [])
    assert pref_rows, f"no preference rows captured: {captured!r}"
    assert any("ruff over black" in r["text"] for r in pref_rows)
    for r in pref_rows:
        assert "bucket" not in r["metadata"], (
            "preference rows must NOT carry a bucket field — USER.md's "
            "AUTO_SECTIONS bucket_filter=None matches unscoped rows"
        )

    # Lesson row(s) — kind="lesson", bucket="workflow".
    lesson_rows = by_kind.get("lesson", [])
    assert lesson_rows
    assert any(
        r["metadata"].get("bucket") == "workflow"
        and "grep before reading" in r["text"]
        for r in lesson_rows
    )


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
async def test_lessons_dual_write_to_v2_facts(tmp_path: Path) -> None:
    """Wave-27 follow-up: when ``memory_v2_service`` is attached,
    extracted lessons also flow into the v2 facts store via
    ``MemoryService.remember()`` so the LanceDB dedup pipeline covers
    them. The memory.db side (``memory_provider``) is left None here
    so we observe ONLY the v2 path firing.
    """
    from xmclaw.daemon.post_sampling_hooks import _write_facts_to_memory
    from xmclaw.memory.v2 import (
        EmbeddingService,
        InMemoryGraphBackend,
        InMemoryVectorBackend,
        MemoryService,
        StubEmbedder,
    )

    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    ctx = HookContext(
        session_id="s-lesson-dual",
        agent_id="main",
        user_message="hi",
        assistant_response="hello",
        history=[],
        llm=_StubLLM('{"facts": []}'),
        persona_dir=tmp_path,
        cfg={},
        memory_provider=None,        # legacy path silent
        embedder=None,
        persona_store=None,
        memory_v2_service=svc,        # new path active
    )
    await _write_facts_to_memory(
        ctx,
        ["grep before reading huge files", "prefer surgical edits"],
        kind="lesson",
        bucket="workflow",
    )
    hits = await svc.recall(
        None, kinds=["lesson"], k=10, min_confidence=0.0,
    )
    texts = {h.fact.text for h in hits}
    assert texts == {
        "grep before reading huge files",
        "prefer surgical edits",
    }
    for h in hits:
        assert h.fact.scope == "project"


@pytest.mark.asyncio
async def test_phase3a_v2_handled_skips_memory_db_when_wired(
    tmp_path: Path,
) -> None:
    """Phase 3a: when v2 service is wired AND accepts the write,
    the legacy memory.db dual-write is SKIPPED entirely. The v2
    path becomes the single source of truth for lessons /
    preferences. Pre-fix: both stores got the row → memory_search
    could surface stale lessons that v2's dedup had already
    superseded.
    """
    from unittest.mock import AsyncMock, MagicMock
    from xmclaw.daemon.post_sampling_hooks import _write_facts_to_memory
    from xmclaw.memory.v2 import (
        EmbeddingService,
        InMemoryGraphBackend,
        InMemoryVectorBackend,
        MemoryService,
        StubEmbedder,
    )

    svc = MemoryService(
        vector_backend=InMemoryVectorBackend(),
        graph_backend=InMemoryGraphBackend(),
        embedder=EmbeddingService(StubEmbedder(dim=4)),
    )
    # Mock memory.db + persona_store so we can assert they were
    # NEVER touched when v2 absorbed the write.
    memory_provider = MagicMock()
    memory_provider.upsert_fact = AsyncMock()
    memory_provider.put = AsyncMock()
    persona_store = MagicMock()
    persona_store.render_to_disk = AsyncMock()

    ctx = HookContext(
        session_id="s-3a",
        agent_id="main",
        user_message="hi", assistant_response="hello",
        history=[], llm=_StubLLM('{"facts": []}'),
        persona_dir=tmp_path, cfg={},
        memory_provider=memory_provider,
        embedder=None,
        persona_store=persona_store,
        memory_v2_service=svc,
    )
    await _write_facts_to_memory(
        ctx, ["grep before reading huge files"],
        kind="lesson", bucket="workflow",
    )
    # v2 received the fact.
    hits = await svc.recall(
        None, kinds=["lesson"], k=10, min_confidence=0.0,
    )
    assert any(
        h.fact.text == "grep before reading huge files" for h in hits
    )
    # memory.db was NOT touched.
    memory_provider.upsert_fact.assert_not_called()
    memory_provider.put.assert_not_called()
    # PersonaStore.render_to_disk was NOT called (v2_renderer did it).
    persona_store.render_to_disk.assert_not_called()


@pytest.mark.asyncio
async def test_phase3a_legacy_path_still_works_when_v2_missing(
    tmp_path: Path,
) -> None:
    """Without v2 wired the legacy memory.db + PersonaStore path
    stays alive — Phase 3a only deprecates the dual-write, not
    the entire legacy stack. Important for installs that haven't
    enabled cognition.memory_v2 yet.
    """
    from unittest.mock import AsyncMock, MagicMock
    from xmclaw.daemon.post_sampling_hooks import _write_facts_to_memory

    memory_provider = MagicMock()
    memory_provider.upsert_fact = AsyncMock()
    persona_store = MagicMock()
    persona_store.render_to_disk = AsyncMock()

    ctx = HookContext(
        session_id="s-legacy",
        agent_id="main",
        user_message="hi", assistant_response="hello",
        history=[], llm=_StubLLM('{"facts": []}'),
        persona_dir=tmp_path, cfg={},
        memory_provider=memory_provider,
        embedder=None,
        persona_store=persona_store,
        memory_v2_service=None,        # v2 NOT wired
    )
    await _write_facts_to_memory(
        ctx, ["a workflow lesson"],
        kind="lesson", bucket="workflow",
    )
    # memory.db DID get the write.
    memory_provider.upsert_fact.assert_called()
    # PersonaStore DID render (legacy path still active).
    persona_store.render_to_disk.assert_called()


@pytest.mark.asyncio
async def test_v2_dual_write_skipped_when_service_none(tmp_path: Path) -> None:
    """No v2 service attached → the dual-write path is a no-op. The
    helper must not raise (best-effort indexing semantics)."""
    from xmclaw.daemon.post_sampling_hooks import _write_facts_to_memory
    ctx = HookContext(
        session_id="s",
        agent_id="main",
        user_message="hi",
        assistant_response="hello",
        history=[],
        llm=_StubLLM('{"facts": []}'),
        persona_dir=tmp_path,
        cfg={},
        memory_provider=None,
        embedder=None,
        persona_store=None,
        memory_v2_service=None,
    )
    # Must not raise even though there's no backend at all.
    await _write_facts_to_memory(
        ctx, ["a lesson"], kind="lesson", bucket="workflow",
    )


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


# ── 2026-06-06 记忆污染修复：内部反思会话不抽取 lesson/preference ──


def _internal_ctx(persona_dir, sid):
    return HookContext(
        session_id=sid,
        agent_id="main",
        user_message="hi",
        assistant_response="hello",
        history=[],
        llm=_StubLLM('{"facts": []}'),
        persona_dir=persona_dir,
        cfg={"evolution": {"memory": {"extract_facts": {"enabled": True},
                                       "extract_memories": {"enabled": True}}}},
    )


def test_internal_sessions_skip_extraction(tmp_path: Path) -> None:
    """goal-from-percept / reflect: 等内部反思会话不该抽取记忆，否则反思
    自言自语反复入库污染记忆库。正常聊天会话照常抽取。"""
    for hook in (ExtractLessonsHook(), ExtractMemoriesHook()):
        # 内部会话 → 失活
        assert hook.is_enabled(_internal_ctx(tmp_path, "goal-from-percept-x")) is False
        assert hook.is_enabled(_internal_ctx(tmp_path, "reflect:abc")) is False
        assert hook.is_enabled(_internal_ctx(tmp_path, "autonomous:y")) is False
    # 正常聊天会话 → ExtractLessons 仍启用（extract_facts.enabled=True）
    assert ExtractLessonsHook().is_enabled(_internal_ctx(tmp_path, "chat-123")) is True
