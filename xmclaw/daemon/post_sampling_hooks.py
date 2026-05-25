"""Post-sampling hook framework (B-112) — free-code parity.

After every successfully completed turn (final assistant response, no
pending tool calls), fire a chain of registered hooks. Each hook gets
the post-turn context (history, llm provider, persona dir) and may run
its own background work asynchronously without blocking the user's
next prompt.

Free-code uses this to:
  * extractMemories  — each turn end, scan transcript for durable
                       facts, append to MEMORY.md
  * SessionMemory    — maintain a per-session running summary file
  * autoDream        — schedule MEMORY.md compaction when thresholds met
  * PromptSuggestion — speculatively pre-warm the next likely prompt

XMclaw already has Auto-Dream as a cron (B-51); this framework makes
the rest pluggable.

Hook landing order:
  * B-112  ``ExtractMemoriesHook``  — original 1-bucket extractor
                                      (durable MEMORY.md facts only).
                                      Gated default OFF.
  * B-168  ``ExtractLessonsHook``   — 3-bucket lesson extractor
                                      (workflow / tool_quirks /
                                      failure_modes). Default ON.
  * B-303  ExtractLessonsHook gets two more buckets (values / rules)
           so SOUL.md + LEARNING.md stop sitting empty.
  * B-319  ExtractLessonsHook absorbs the ``preferences`` bucket —
           now handles all 6 auto-section kinds across the full
           7-file persona set in a single LLM call. The hook is
           re-aliased as ``ExtractFactsHook`` to reflect the
           broader scope; the legacy ``ExtractLessonsHook`` name is
           kept as a backward-compat re-export.
           ``ExtractMemoriesHook`` is now deprecated — its only
           output (durable failure_modes bullets) is now produced
           by the ``failure_modes`` bucket of the unified extractor,
           so leaving it on costs an extra LLM call for zero new
           coverage.

Cache-sharing optimisation (the main reason free-code uses a "forked
agent" pattern) is left for a follow-up — the LLM provider needs
explicit cache_breakpoint support, which is a separate plumbing job.
The hook can already run today; cache hit-rate is the future
optimisation.
"""
from __future__ import annotations

import abc
import asyncio
import re
from dataclasses import dataclass
from typing import Any

from xmclaw.daemon.extractor_prompts import load_prompt
from xmclaw.utils.log import get_logger


# B-179 (joint audit fix): the LLM extractor sometimes includes a
# leading "YYYY-MM-DD:" inside the lesson text — when we then prepend
# our own date, MEMORY.md ends up with "- 2026-05-02: 2026-05-02: ..."
# duplicates that the joint audit caught. Strip any leading date /
# colon prefix from the LLM string before prepending our canonical
# one.
_LEADING_DATE_RE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}\s*[:：]?\s*"
    r"(\(?[^)]{0,30}\)?\s*[:：]\s*)?",  # optional "(精炼):" parenthetical
)


def _strip_leading_date(text: str) -> str:
    """Strip ``YYYY-MM-DD:`` (and optional parenthetical tag) prefixes
    from LLM-extracted bullet text so the caller's own date prefix
    isn't doubled."""
    if not text:
        return text
    out = text
    # Strip up to 3 leading date prefixes — the LLM has been observed
    # producing "2026-05-02: 2026-05-02 (精炼): ..." in evidence dumps.
    for _ in range(3):
        m = _LEADING_DATE_RE.match(out)
        if m is None or m.end() == 0:
            break
        out = out[m.end():]
    return out.strip()

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HookContext:
    """Snapshot of the just-finished turn passed to every hook.

    Hooks must NOT mutate the history list — it's the live one
    AgentLoop uses for the next turn. Read-only by convention.

    B-197: ``memory_provider`` and ``embedder`` let extractor hooks
    dual-write facts (kind=lesson / kind=preference) to the vector
    store in addition to the persona markdown files. Both default
    None for legacy / test setups without a vec store wired.
    """

    session_id: str
    agent_id: str
    user_message: str
    assistant_response: str
    history: list  # type: ignore[type-arg]   # list[Message]
    llm: Any
    persona_dir: Any   # Path | None
    cfg: dict[str, Any]
    memory_provider: Any = None  # MemoryProvider | None
    embedder: Any = None         # EmbeddingProvider | None
    persona_store: Any = None    # PersonaStore | None — B-198 Phase 3
    # Wave-27 follow-up: when set, lesson-kind facts also dual-write
    # to the v2 facts store so the LanceDB dedup pipeline (write-time
    # near-dup merge + bulk dedup + SUPERSEDES graph) covers them.
    # Legacy memory.db kind=lesson rows continue to land so the
    # persona MD render path stays unchanged.
    memory_v2_service: Any = None  # MemoryService | None


class PostSamplingHook(abc.ABC):
    """One pluggable post-turn task. Subclasses implement ``run``."""

    #: Stable id for telemetry / disable-by-config. e.g. "extract_memories".
    id: str = ""

    @abc.abstractmethod
    async def run(self, ctx: HookContext) -> None:
        """Do whatever the hook does. Failures must NOT raise — log
        and swallow, so one broken hook doesn't break the chain."""

    def is_enabled(self, ctx: HookContext) -> bool:
        """Default: enabled. Override to gate on config."""
        return True


class HookRegistry:
    """Ordered list of hooks. AgentLoop calls ``dispatch`` after every
    successful turn (final response with no pending tool calls)."""

    def __init__(self) -> None:
        self._hooks: list[PostSamplingHook] = []

    def register(self, hook: PostSamplingHook) -> None:
        self._hooks.append(hook)

    def hooks(self) -> list[PostSamplingHook]:
        return list(self._hooks)

    async def dispatch(self, ctx: HookContext) -> None:
        """Fire every enabled hook concurrently. Errors logged, never
        propagated. Returns when all hooks settle."""
        coros: list[Any] = []
        for h in self._hooks:
            try:
                if not h.is_enabled(ctx):
                    continue
            except Exception:  # noqa: BLE001
                continue
            coros.append(_safe_run(h, ctx))
        if not coros:
            return
        await asyncio.gather(*coros, return_exceptions=True)


async def _safe_run(hook: PostSamplingHook, ctx: HookContext) -> None:
    try:
        await hook.run(ctx)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "post_sampling_hook.run_failed id=%s err=%s",
            getattr(hook, "id", type(hook).__name__), exc,
        )


async def _write_facts_to_memory(
    ctx: HookContext,
    facts: list[str],
    *,
    kind: str,
    bucket: str | None = None,
) -> None:
    """B-197: dual-write extracted facts to the memory provider as DB
    rows so they're vector-searchable + filterable by kind.

    Called by ExtractMemoriesHook / ExtractLessonsHook after their
    markdown append succeeds. Failure here is logged but never
    propagated — markdown stays the user-visible surface, DB is
    best-effort indexing.

    Wave-27 follow-up: when ``ctx.memory_v2_service`` is also wired,
    ``lesson`` + ``preference`` kinds get a *parallel* write to the v2
    facts store so the LanceDB dedup pipeline covers them. The v2 path
    runs even when ``memory_provider`` is None (the legacy memory.db
    write is optional; v2 doesn't depend on it).
    """
    if not facts:
        return

    # 2026-05-25 (chat-b3c614bc fix): drop self-capability-denial facts
    # BEFORE they reach memory. Background:
    #   When the model hallucinates "我看不到聊天里的图片", the
    #   extractor would capture that as a high-conf fact, render it
    #   into USER.md, and every subsequent turn would re-inject it
    #   as ground truth → the model parrots the lie forever. Prior
    #   fixes (06d3ba3 anti-poisoning ground-truth note at the
    #   message boundary) only worked at READ time; the persona MD
    #   file kept growing. This is the WRITE-time gate.
    from xmclaw.core.persona.toxic_facts import (
        is_toxic_self_capability_denial,
    )
    cleaned_facts: list[str] = []
    for _f in facts:
        toxic, pid = is_toxic_self_capability_denial(_f)
        if toxic:
            try:
                from xmclaw.utils.log import get_logger as _gl
                _gl(__name__).warning(
                    "toxic_fact.rejected_at_write pattern=%s "
                    "kind=%s text=%s",
                    pid, kind, (_f or "")[:160],
                )
            except Exception:  # noqa: BLE001
                pass
            continue
        cleaned_facts.append(_f)
    if not cleaned_facts:
        return
    facts = cleaned_facts

    # Wave-27 follow-up: v2 facts write runs FIRST + independently so
    # the legacy memory.db None-path doesn't short-circuit it. Lessons
    # (workflow / tool_quirks / failure_modes / values / rules) and
    # preferences flow here; the v2 fact id is deterministic on
    # (kind, scope, text) so repeats merge into one row with bumped
    # evidence_count via the write-time near-dup pipeline.
    #
    # Phase 2 (2026-05-16): pass ``bucket`` through to the v2 store
    # so v2_renderer can find the right MD file for each lesson
    # bucket (workflow → AGENTS.md, values → SOUL.md, etc.) — see
    # xmclaw/core/persona/v2_renderer.py:BUCKET_TO_FILE for the
    # full mapping. After the writes complete, fire the renderer
    # once for the whole batch — re-renders only the MD files
    # whose buckets are actually touched (idempotent, cheap).
    v2_svc = getattr(ctx, "memory_v2_service", None)
    v2_written: list[Any] = []
    if v2_svc is not None and kind in ("lesson", "preference"):
        # Default scope. preference is user-scoped (matches the
        # LLM extractor's contract); lessons stay project-scoped.
        v2_scope = "user" if kind == "preference" else "project"
        # Map the legacy ``bucket`` argument to the v2 bucket label.
        # For preference writes the legacy code passed bucket=None,
        # so we synthesise "user_preference" — same label the LLM
        # extractor uses in Phase 1.
        if kind == "preference":
            v2_bucket = "user_preference"
        else:
            v2_bucket = bucket or ""
        for fact in facts:
            try:
                f = await v2_svc.remember(
                    fact,
                    kind=kind,
                    scope=v2_scope,
                    confidence=0.7,
                    source_event_id=ctx.session_id,
                    bucket=v2_bucket,
                )
                v2_written.append(f)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "post_sampling.v2_dual_write_failed kind=%s err=%s",
                    kind, exc,
                )

    # Phase 3a (2026-05-16): when v2 actually accepted writes for
    # this batch, SKIP the legacy memory.db dual-write entirely.
    # Pre-fix the two stores both held lessons → drift between
    # them caused the user's "L1 has it, IDENTITY.md doesn't"
    # complaint (resolved with v2_renderer in Phase 1/2), but it
    # also left ``memory_search`` and ``Auto-Dream`` consuming
    # stale memory.db rows when v2 had already deduped /
    # superseded them. Single-source-of-truth wins: v2 holds the
    # data, v2_renderer (fired below) writes the MD file, the
    # legacy memory.db path stays only as a fallback for the
    # "v2 not configured" install. ``kind not in (lesson,
    # preference)`` is OUT OF SCOPE — Wave-27 fix-8 only routed
    # those two kinds through v2; other kinds (preference,
    # decision, file_chunk, code_chunk) still flow through
    # memory.db as before.
    v2_handled_kind = (
        v2_svc is not None
        and v2_written
        and kind in ("lesson", "preference")
    )

    if v2_handled_kind:
        # v2 absorbed the write — render persona files from v2 and
        # return BEFORE the legacy memory.db path so we don't
        # double-write the same MD file under two different
        # formats.
        if ctx.persona_dir is not None:
            try:
                from xmclaw.core.persona.v2_renderer import render_affected_files
                await render_affected_files(
                    v2_svc, ctx.persona_dir, v2_written,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "post_sampling.v2_renderer_failed kind=%s err=%s",
                    kind, exc,
                )
        return

    if ctx.memory_provider is None:
        return

    try:
        import time as _t
        import uuid as _uuid
        from xmclaw.providers.memory.base import MemoryItem
    except Exception:  # noqa: BLE001
        return

    # Embed in one batch when an embedder is wired (cheaper than
    # per-row roundtrips, graceful degradation when down).
    embeddings: list[list[float] | None] = [None] * len(facts)
    if ctx.embedder is not None:
        try:
            vecs = await ctx.embedder.embed(list(facts))
            if vecs:
                embeddings = [
                    list(v) if v is not None else None for v in vecs
                ]
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "post_sampling.embedder_failed kind=%s err=%s",
                kind, exc,
            )

    # B-197 Phase 2: prefer upsert_fact so repeats strengthen one row
    # (evidence_count++) instead of stacking duplicates. Falls back to
    # put() for providers that don't expose upsert_fact yet.
    upsert = getattr(ctx.memory_provider, "upsert_fact", None)
    any_wrote = False

    for fact, emb in zip(facts, embeddings):
        md: dict[str, Any] = {
            "kind": kind,
            "session_id": ctx.session_id,
            "agent_id": ctx.agent_id,
            "evidence_count": 1,
            "ts": _t.time(),
        }
        if bucket is not None:
            md["bucket"] = bucket
        try:
            if upsert is not None:
                await upsert(
                    text=fact,
                    embedding=emb,
                    layer="working",
                    metadata=md,
                )
            else:
                item = MemoryItem(
                    id=_uuid.uuid4().hex,
                    layer="working",
                    text=fact,
                    metadata=md,
                    embedding=tuple(emb) if emb else None,
                    ts=_t.time(),
                )
                await ctx.memory_provider.put("working", item)
            any_wrote = True
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "post_sampling.db_write_failed kind=%s err=%s",
                kind, exc,
            )

    # B-198 Phase 3: re-render the affected persona files from DB so
    # the on-disk cache (still read by the assembler) reflects the
    # newly-upserted rows. We render ALL files for simplicity — disk
    # write is cheap, the alternative (kind→file mapping inversion)
    # adds complexity for marginal speedup.
    #
    # Phase 3a (2026-05-16): only reached when v2 was NOT wired
    # (the v2-handled fast-return above skips this block). The
    # PersonaStore render path stays for backward compat with
    # installs that haven't enabled v2 — once cognition.memory_v2
    # is true everywhere, this branch can be removed entirely.
    if any_wrote and ctx.persona_store is not None:
        try:
            await ctx.persona_store.render_to_disk()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "post_sampling.render_to_disk_failed err=%s", exc,
            )


# ── ExtractMemoriesHook (B-112 reference impl) ────────────────────────


_EXTRACT_PROMPT = (
    "You are reviewing a chat turn that just ended between a user and "
    "the XMclaw agent. Identify any DURABLE facts worth saving to long-"
    "term memory — user preferences ('prefers Python over Go'), "
    "decisions ('we picked sqlite over postgres'), recurring failure "
    "modes ('build always breaks on missing setuptools'), tool-usage "
    "lessons. Skip ephemeral things (status reports, summaries of "
    "what just happened, restated context).\n\n"
    "Output strict JSON: {\"facts\": [\"fact 1\", \"fact 2\"]}. "
    "Empty list if nothing durable was discussed. No prose."
)


class ExtractMemoriesHook(PostSamplingHook):
    """DEPRECATED (B-319) — kept for backward compat / opt-in deeper
    analysis only.

    Originally B-112: each turn end, ask the main LLM whether the
    just-finished exchange contained durable facts worth recording,
    writing hits to MEMORY.md ``## Auto-extracted``.

    Why deprecated: the unified :class:`ExtractFactsHook` (default ON)
    now produces the same MEMORY.md content via its ``failure_modes``
    bucket — and four other buckets in the same LLM round-trip. Leaving
    this hook enabled costs a *second* LLM call per turn for output
    that's already covered. We keep the class around (and the
    ``evolution.memory.extract_memories.enabled`` gate, default OFF)
    so installs that historically opted in for the more conservative
    "durable only" prompt can still flip it on if they want both
    extractors running. New installs should leave it off.

    Gated by ``evolution.memory.extract_memories.enabled`` (default
    OFF). Skips automatically when ``persona_dir`` is unset.
    """

    id = "extract_memories"

    def is_enabled(self, ctx: HookContext) -> bool:
        if ctx.persona_dir is None:
            return False
        section = (
            ((ctx.cfg.get("evolution") or {}).get("memory") or {})
            .get("extract_memories") or {}
        )
        return bool(section.get("enabled", False))

    async def run(self, ctx: HookContext) -> None:
        import json

        from xmclaw.providers.llm.base import Message

        excerpt = (
            f"User: {ctx.user_message[:1000]}\n\n"
            f"Assistant: {ctx.assistant_response[:1500]}"
        )
        messages = [
            Message(
                role="system",
                content=load_prompt("extract_memories", _EXTRACT_PROMPT),
            ),
            Message(role="user", content=excerpt),
        ]
        try:
            resp = await ctx.llm.complete(messages, tools=None)
        except Exception:  # noqa: BLE001
            return
        raw = (getattr(resp, "content", None) or "").strip()
        if not raw:
            return
        # Strict / lenient JSON extraction. Same pattern as
        # relevant_picker._parse_pick_response.
        facts: list[str] = []
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and isinstance(obj.get("facts"), list):
                facts = [str(f).strip() for f in obj["facts"] if str(f).strip()]
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not facts:
            return

        # B-198 Phase 3: when persona_store is wired, the legacy
        # markdown append is redundant — _write_facts_to_memory will
        # upsert and re-render the disk cache from DB. Skip it.
        if ctx.persona_store is None:
            # Legacy markdown-only path (tests / installs without store).
            try:
                from xmclaw.providers.tool.builtin import (
                    PERSONA_CHAR_CAPS,
                    _append_under_section,
                    enforce_char_cap,
                )
                from xmclaw.utils.fs_locks import atomic_write_text, get_lock
            except Exception:  # noqa: BLE001
                return
            from pathlib import Path

            pdir = Path(str(ctx.persona_dir))
            pdir.mkdir(parents=True, exist_ok=True)
            mfile = pdir / "MEMORY.md"
            async with get_lock(mfile):
                try:
                    existing = (
                        mfile.read_text(encoding="utf-8") if mfile.is_file() else ""
                    )
                    new_text = existing
                    import time as _t
                    date = _t.strftime("%Y-%m-%d")
                    for fact in facts[:5]:  # cap per-turn yield
                        cleaned = _strip_leading_date(
                            fact.replace(chr(10), " ").strip()
                        )
                        bullet = f"- {date}: {cleaned}"
                        new_text = _append_under_section(
                            new_text,
                            section_header="## Auto-extracted",
                            bullet=bullet,
                            placeholder_title="MEMORY.md — what I want to remember next time",
                        )
                    cap = PERSONA_CHAR_CAPS.get("MEMORY.md")
                    if cap is not None and len(new_text) > cap:
                        new_text = enforce_char_cap(new_text, cap)
                    if new_text != existing:
                        atomic_write_text(mfile, new_text)
                except OSError:
                    return

        # B-341 (audit pass-2 #14): when the unified
        # ExtractFactsHook is also enabled (the default since B-319)
        # it already writes to ``bucket="failure_modes"`` from its own
        # LLM round-trip, so this hook would file SECOND-LLM "memories"
        # into the SAME bucket → AUTO_SECTIONS renders both extractions
        # interleaved under the same MEMORY.md heading, defeating the
        # value of running two extractors. Pre-B-341 the user
        # explicitly opting both on got mixed-source bullets with no
        # way to tell them apart.
        #
        # If ExtractFactsHook is on, skip the DB write entirely
        # (legacy markdown above is enough — the failure_modes bucket
        # is already covered). If ExtractFactsHook is OFF, fall
        # through to the original DB write so the legacy hook still
        # works as the sole extractor.
        memory_cfg = ((ctx.cfg.get("evolution") or {}).get("memory") or {})
        facts_section = (
            memory_cfg.get("extract_facts")
            or memory_cfg.get("extract_lessons")
            or {}
        )
        facts_hook_on = bool(facts_section.get("enabled", True))
        if facts_hook_on:
            return
        # bucket=durable_fact tags these as "general lessons" rather
        # than the workflow/tool_quirks/failure_modes split that
        # ExtractLessonsHook produces. They render under MEMORY.md's
        # auto section per AUTO_SECTIONS routing.
        await _write_facts_to_memory(
            ctx, facts[:5], kind="lesson", bucket="failure_modes",
        )


# ── ExtractLessonsHook (B-168) ────────────────────────────────────────


_LESSONS_PROMPT = (
    "You are reviewing the chat turn that just ended between a user "
    "and the XMclaw agent. Extract anything *future-you* would benefit "
    "from remembering. Be GENEROUS — pre-B-303 the bar was 'DURABLE "
    "lesson only' which produced empty buckets nearly every turn, so "
    "AGENTS.md / TOOLS.md / LEARNING.md / SOUL.md sat empty for weeks. "
    "Now lower the bar: ANY observation, technique, or principle that "
    "could plausibly help a future turn counts. Six buckets:\n\n"
    "  - \"workflow\": procedure / sequencing observations. Anything "
    "from 'grep before reading huge files' to 'when user asks 怎么 X, "
    "first list_dir to confirm context'. Smaller hints are fine — "
    "future-you can dedupe via Auto-Dream. Goes to AGENTS.md.\n"
    "  - \"tool_quirks\": tool gotchas, surprises, hidden flags, "
    "unexpected output formats. Even 'memory_search returns dicts not "
    "strings' or 'bash on Windows runs Git Bash, not PowerShell' "
    "qualifies. Goes to TOOLS.md.\n"
    "  - \"failure_modes\": breakage patterns, error shapes, retry "
    "strategies, things that didn't work. Goes to MEMORY.md (Failure "
    "Modes).\n"
    "  - \"values\" (B-303 new): character / values / aesthetic the "
    "agent expressed or chose. Examples: 'prefer surgical edits over "
    "rewrites', 'be honest when uncertain', '能动手就别让用户动手', "
    "'reject ASCII boxart in commit messages'. Goes to SOUL.md.\n"
    "  - \"rules\" (B-303 new): explicit if-then heuristics for future "
    "behaviour. Examples: 'if user asks for time, do not just compute "
    "from training-cutoff — read ## 当前时刻 block', 'if 0 skill_* "
    "match a query, call skill_browse before bash'. Goes to "
    "LEARNING.md.\n"
    "  - \"preferences\" (B-319 new): user-specific preferences about "
    "communication style, language, tooling, output format, or "
    "personality. Examples: 'user prefers Chinese for casual chat, "
    "English for code comments', 'user wants concise answers, no "
    "preamble', 'user prefers ruff over black', 'user wants me to "
    "fix lint inline rather than ask first'. Goes to USER.md. This "
    "subsumes the legacy ProfileExtractor write-path — emit any "
    "stable preference signal you see, not only ones the user "
    "explicitly stated.\n\n"
    "Skip:\n"
    "  - Pure status restatements ('I just did X', 'opening file Y').\n"
    "  - Information already in the system prompt.\n"
    "  - One-off transient context that won't matter next turn.\n\n"
    "It's BETTER to extract a small/imperfect bullet than to skip — "
    "Auto-Dream consolidates duplicates, and the cap of 3 per bucket "
    "stops verbose LLMs from spamming. Output strict JSON: "
    "{\"workflow\": [\"...\"], \"tool_quirks\": [\"...\"], "
    "\"failure_modes\": [\"...\"], \"values\": [\"...\"], "
    "\"rules\": [\"...\"], \"preferences\": [\"...\"]}. "
    "Empty arrays only when truly nothing fits. No prose."
)


_LESSON_BUCKETS: dict[str, tuple[str, str, str, str | None]] = {
    # bucket name → (target file, section header, fact kind, db bucket)
    #
    # ``fact kind`` is what we tag the DB row with — must match the
    # ``fact_kind`` field in :data:`xmclaw.core.persona.store.AUTO_SECTIONS`
    # for the matching file, otherwise the renderer won't pick the row
    # up. ``db bucket`` is the ``metadata.bucket`` we attach to the row
    # — ``None`` keeps the row unscoped (for files whose AUTO_SECTIONS
    # entry has ``bucket_filter=None``, like USER.md).
    "workflow":      ("AGENTS.md",   "## Auto-extracted",             "lesson",     "workflow"),
    "tool_quirks":   ("TOOLS.md",    "## Auto-extracted",             "lesson",     "tool_quirks"),
    "failure_modes": ("MEMORY.md",   "## Failure Modes",              "lesson",     "failure_modes"),
    # B-303: extend to SOUL.md + LEARNING.md so all 7 persona files
    # get auto-coverage instead of just AGENTS / TOOLS / MEMORY +
    # USER (via ProfileExtractor). Pre-B-303 SOUL.md / LEARNING.md
    # were strictly manual-write — agent rarely did, so they sat
    # empty even after 100+ turns.
    "values":        ("SOUL.md",     "## Auto-extracted",             "lesson",     "values"),
    "rules":         ("LEARNING.md", "## Auto-extracted",             "lesson",     "rules"),
    # B-319: absorb the USER.md preference write-path that was
    # previously the exclusive job of ProfileExtractor. Different
    # ``kind`` ("preference", not "lesson") so the renderer matches
    # USER.md's AUTO_SECTIONS entry; ``bucket=None`` because USER.md
    # has ``bucket_filter=None`` (any preference fact qualifies).
    # Net effect: one LLM call per turn now covers all 6 auto-section
    # routes — the legacy ProfileExtractor + ExtractMemoriesHook are
    # kept for backward compat / opt-in deeper analysis but no
    # longer required for baseline coverage.
    "preferences":   ("USER.md",     "## Auto-extracted preferences", "preference", None),
}


class ExtractLessonsHook(PostSamplingHook):
    """B-168 / B-303 / B-319: turn-end LLM extractor that writes the
    auto-extracted section of every persona file in a single round-trip.

    Originally B-168 covered three buckets (workflow / tool_quirks /
    failure_modes) for AGENTS.md / TOOLS.md / MEMORY.md only. B-303
    added ``values`` (SOUL.md) and ``rules`` (LEARNING.md). B-319
    absorbs the ``preferences`` bucket — written with
    ``kind="preference"`` so the persona store renders them under
    USER.md's ``## Auto-extracted preferences`` heading. That makes
    this hook the unified write-path for all 6 auto-section kinds
    across the 7-file persona set.

    Six buckets, six target files, one LLM call per turn:
      * ``workflow``      → ``AGENTS.md``  ``## Auto-extracted``
      * ``tool_quirks``   → ``TOOLS.md``   ``## Auto-extracted``
      * ``failure_modes`` → ``MEMORY.md``  ``## Failure Modes``
      * ``values``        → ``SOUL.md``    ``## Auto-extracted``
      * ``rules``         → ``LEARNING.md`` ``## Auto-extracted``
      * ``preferences``   → ``USER.md``    ``## Auto-extracted preferences``

    Forward-compat alias: this class is also exported as
    :class:`ExtractFactsHook` — use that name in new code; the
    "Lessons" name predates the broader scope.

    Default ON — closes the original gap "经验教训也会自己总结的对吧？"
    (right, lessons get auto-summarized too?). Pre-B-168 answer was
    "no, only USER.md (via ProfileExtractor), and only manually for
    everything else". Post-B-319 answer is "yes, every persona file
    auto-fills from a single LLM call per turn".

    Cap per turn: 3 facts per bucket so a chatty LLM can't spam.
    Char caps in :data:`PERSONA_CHAR_CAPS` evict the oldest bullets
    when files outgrow their budget.

    Gate: ``evolution.memory.extract_lessons.enabled`` — flip to false
    if the extra LLM call per turn isn't worth the latency for this
    particular agent. (Renamed to ``extract_facts.enabled`` is
    accepted as an alias for forward compatibility.)
    """

    id = "extract_lessons"

    #: Per-turn cap so a verbose LLM can't dump 20 vague "facts".
    MAX_PER_BUCKET: int = 3

    def is_enabled(self, ctx: HookContext) -> bool:
        if ctx.persona_dir is None:
            return False
        memory_cfg = ((ctx.cfg.get("evolution") or {}).get("memory") or {})
        # Forward name first; legacy name as backward-compat alias.
        section = (
            memory_cfg.get("extract_facts")
            or memory_cfg.get("extract_lessons")
            or {}
        )
        return bool(section.get("enabled", True))

    async def run(self, ctx: HookContext) -> None:
        import json
        from pathlib import Path

        from xmclaw.providers.llm.base import Message

        excerpt = (
            f"User: {ctx.user_message[:1000]}\n\n"
            f"Assistant: {ctx.assistant_response[:1500]}"
        )
        messages = [
            Message(
                role="system",
                content=load_prompt("extract_lessons", _LESSONS_PROMPT),
            ),
            Message(role="user", content=excerpt),
        ]
        try:
            resp = await ctx.llm.complete(messages, tools=None)
        except Exception:  # noqa: BLE001 — never let a hook kill the chain
            return
        raw = (getattr(resp, "content", None) or "").strip()
        if not raw:
            return

        # Strip a leading ```json fence if the LLM wrapped its output.
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()

        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(obj, dict):
            return

        buckets: dict[str, list[str]] = {}
        for key in _LESSON_BUCKETS:
            raw_list = obj.get(key)
            if not isinstance(raw_list, list):
                continue
            cleaned: list[str] = []
            for item in raw_list[: self.MAX_PER_BUCKET]:
                s = str(item).strip()
                if s:
                    cleaned.append(s)
            if cleaned:
                buckets[key] = cleaned

        if not buckets:
            return

        try:
            from xmclaw.providers.tool.builtin import (
                PERSONA_CHAR_CAPS,
                _append_under_section,
                enforce_char_cap,
            )
            from xmclaw.utils.fs_locks import atomic_write_text, get_lock
        except Exception:  # noqa: BLE001 — startup-order safety, log only
            return

        pdir = Path(str(ctx.persona_dir))
        pdir.mkdir(parents=True, exist_ok=True)

        # Group facts by destination file so we take one lock per
        # file, not one per bullet — fewer atomic writes, fewer races.
        per_file: dict[str, list[tuple[str, str]]] = {}
        for bucket, facts in buckets.items():
            target_file, section, _kind, _db_bucket = _LESSON_BUCKETS[bucket]
            per_file.setdefault(target_file, []).extend(
                (section, fact) for fact in facts
            )

        import time as _t
        date = _t.strftime("%Y-%m-%d")

        # B-198 Phase 3: skip legacy markdown writes when the
        # persona_store is wired — _write_facts_to_memory below
        # upserts to DB + re-renders disk from there.
        if ctx.persona_store is None:
            for target_file, entries in per_file.items():
                mfile = pdir / target_file
                async with get_lock(mfile):
                    try:
                        existing = (
                            mfile.read_text(encoding="utf-8")
                            if mfile.is_file() else ""
                        )
                        new_text = existing
                        for section, fact in entries:
                            # Renamed from ``cleaned`` to avoid colliding with
                            # the ``cleaned: list[str]`` accumulator earlier in
                            # this function (mypy correctly flagged the
                            # type-shadowing — same name in the same method
                            # body).
                            cleaned_line = _strip_leading_date(
                                fact.replace(chr(10), " ").strip()
                            )
                            bullet = f"- {date}: {cleaned_line}"
                            new_text = _append_under_section(
                                new_text,
                                section_header=section,
                                bullet=bullet,
                                placeholder_title=(
                                    f"{target_file} — auto-extracted"
                                ),
                            )
                        cap = PERSONA_CHAR_CAPS.get(target_file)
                        if cap is not None and len(new_text) > cap:
                            new_text = enforce_char_cap(new_text, cap)
                        if new_text != existing:
                            atomic_write_text(mfile, new_text)
                    except OSError:
                        continue

        # B-197/B-319: dual-write each bucket to DB rows with the
        # bucket's configured (kind, db_bucket). preferences land as
        # kind=preference / bucket=None so they match USER.md's
        # AUTO_SECTIONS render filter; lessons keep kind=lesson with
        # their per-bucket scope. upsert_fact (B-197 Phase 2) merges
        # repeats into evidence_count++ per row.
        for bucket_name, facts in buckets.items():
            _file, _section, fact_kind, db_bucket = _LESSON_BUCKETS[bucket_name]
            await _write_facts_to_memory(
                ctx, facts, kind=fact_kind, bucket=db_bucket,
            )


# B-319 forward-compat alias. New code should reference ExtractFactsHook;
# the legacy ExtractLessonsHook name predates the broader scope (now
# covers preferences + values + rules in addition to the original
# lessons buckets) and is kept only so external imports don't break.
ExtractFactsHook = ExtractLessonsHook


def build_default_registry() -> HookRegistry:
    """Default hook chain shipped with the daemon.

    B-319: ``ExtractMemoriesHook`` is no longer registered by default —
    its output (durable failure_modes facts) is now covered by the
    ``failure_modes`` bucket of :class:`ExtractFactsHook` in the same
    LLM round-trip. Installs that explicitly want the legacy second
    extractor can flip ``evolution.memory.extract_memories.enabled``
    on AND register the hook manually; we don't auto-register it here
    so the per-turn LLM count stays at 1 for new installs.
    """
    reg = HookRegistry()
    reg.register(ExtractFactsHook())
    return reg


__all__ = [
    "HookContext",
    "PostSamplingHook",
    "HookRegistry",
    "ExtractMemoriesHook",
    "ExtractLessonsHook",
    "ExtractFactsHook",
    "build_default_registry",
]
