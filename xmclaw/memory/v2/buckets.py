"""Memory bucket registry — the normative schema for fact routing.

2026-05-28 (memory v3 phase 1).

Background
==========

Pre-v3 the bucket→file mapping lived as an inline dict in
``xmclaw/core/persona/v2_renderer.py`` and bucket assignment lived
as hardcoded ``if/elif`` in ``xmclaw/memory/v2/llm_extractor.py``.
Two structural problems:

  1. **Dark facts** — any fact whose ``kind/scope`` didn't match the 3
     branches in the extractor got ``bucket=""``. The renderer
     skipped empty buckets entirely, so those facts never reached
     any ``.md`` file → never reached the system prompt → the agent
     could never see them without explicitly running ``memory_search``.

  2. **Bucket coverage 3/8** — the renderer dict declared 8 buckets,
     but only 3 were filled by writes (agent_identity / user_identity
     / user_preference). The other 5 sections (AGENTS.md ## Workflows,
     TOOLS.md ## Tool quirks, etc.) stayed empty because no writer
     emitted those bucket names. The legacy ``ExtractLessonsHook``
     used to write those .md sections directly, bypassing v2 entirely
     — that's the "two write paths" split the v3 audit identified.

This module
===========

Single registry. Every fact MUST declare a bucket from the registry.
The registry declares for each bucket:

  * which ``.md`` file + section header it renders into
  * char / item caps applied at render time
  * default ``FactKind`` when the writer doesn't specify
  * a one-line description suitable for the LLM extractor's prompt

If a writer can't classify a fact, it MUST use ``misc`` (which
renders to ``MEMORY.md ## Other facts (recent)``). Never ``""``.

This makes the .md files a strict **deterministic projection** of
the LanceDB store: every fact has exactly one render destination,
every render destination has exactly one bucket source, no fact is
dark.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BucketDef:
    """One bucket's render contract.

    ``tag``: the string stored on a fact's ``bucket`` column.
    ``target_file``: persona MD filename (e.g. ``"USER.md"``).
    ``section``: full markdown header line (e.g. ``"## Auto-identity"``).
    ``cap_chars``: max chars rendered into this section before LRU
        eviction kicks in (cap applied at render time, not at write
        time — so the LanceDB store keeps the full history).
    ``cap_items``: max bullet count for this section. Tighter of
        (cap_chars, cap_items) wins.
    ``default_kind``: ``FactKind`` value used when a writer doesn't
        explicitly specify ``kind`` but does specify this bucket.
    ``description``: short human-language description shown to the
        LLM extractor + to the agent in tool docs.
    """

    tag: str
    target_file: str
    section: str
    cap_chars: int
    cap_items: int
    default_kind: str
    description: str

    @property
    def safe_section_slug(self) -> str:
        """Stable slug suitable for HTML-comment markers — mirrors
        ``v2_renderer._section_markers``'s slugger."""
        slug = self.section.lstrip("# ").strip()
        return "".join(c if c.isalnum() else "-" for c in slug).strip("-")


# ─── The registry ─────────────────────────────────────────────────

# Order matters only for ``render_for_prompt`` output (LLM sees them
# in this order). Within a file, ``v2_renderer`` writes sections in
# their natural .md position; this order doesn't dictate that.

BUCKETS: dict[str, BucketDef] = {
    # ── Identity layer (who is who) ────────────────────────────────
    "agent_identity": BucketDef(
        tag="agent_identity",
        target_file="IDENTITY.md",
        section="## Auto-extracted",
        cap_chars=800, cap_items=10,
        default_kind="identity",
        description=(
            "AI agent 自己的身份信息（名字、性格底色、自我描述）。"
            "用户说 \"我叫你 X\" 时归这里。"
        ),
    ),
    "user_identity": BucketDef(
        tag="user_identity",
        target_file="USER.md",
        section="## Auto-identity",
        cap_chars=600, cap_items=8,
        default_kind="identity",
        description=(
            "人类用户的身份信息（姓名、公司、角色、地点、联系方式）。"
        ),
    ),
    # ── Preference / values layer ──────────────────────────────────
    "user_preference": BucketDef(
        tag="user_preference",
        target_file="USER.md",
        section="## Auto-extracted preferences",
        cap_chars=2000, cap_items=15,
        default_kind="preference",
        description=(
            "用户的偏好和习惯（语言/语气/工具/工作方式/讨厌什么）。"
            "用户说 \"我喜欢/我不要/我习惯\" 类语句归这里。"
        ),
    ),
    "values": BucketDef(
        tag="values",
        target_file="SOUL.md",
        section="## Auto-extracted",
        cap_chars=1500, cap_items=10,
        default_kind="identity",
        description=(
            "agent 自己应该秉持的价值观和操作底线"
            "（\"我偏好结构化输出\"、\"我不主动猜测\"）。"
        ),
    ),
    # ── Procedure / workflow layer ─────────────────────────────────
    "workflow": BucketDef(
        tag="workflow",
        target_file="AGENTS.md",
        section="## Workflows",
        cap_chars=3000, cap_items=20,
        default_kind="procedure",
        description=(
            "可重用的操作步骤 / 工作流模板"
            "（\"改 v2_renderer 后必须跑 test_v2_persona_renderer\"）。"
        ),
    ),
    "tool_quirks": BucketDef(
        tag="tool_quirks",
        target_file="TOOLS.md",
        section="## Tool quirks",
        cap_chars=3000, cap_items=20,
        default_kind="lesson",
        description=(
            "工具使用的坑和窍门（\"Playwright select_option 接 string "
            "要先 try value 再 label\"）。"
        ),
    ),
    "rules": BucketDef(
        tag="rules",
        target_file="LEARNING.md",
        section="## Auto-extracted",
        cap_chars=2500, cap_items=15,
        default_kind="lesson",
        description=(
            "学到的硬性规则 / 不变量（\"永远不直接 push main\"、"
            "\"修 fact 必须 supersedes 不要 in-place\"）。"
        ),
    ),
    # ── Episodic / failure layer ───────────────────────────────────
    "failure_modes": BucketDef(
        tag="failure_modes",
        target_file="MEMORY.md",
        section="## Failure Modes",
        cap_chars=2000, cap_items=15,
        default_kind="lesson",
        description=(
            "已知会失败的场景及原因（\"DeepSeek /anthropic shim 不支持 "
            "图像，会返回 [Unsupported Image]\"）。"
        ),
    ),
    # ── Project / domain facts ─────────────────────────────────────
    "project_fact": BucketDef(
        tag="project_fact",
        target_file="MEMORY.md",
        section="## Project facts",
        cap_chars=3000, cap_items=20,
        default_kind="fact",
        description=(
            "项目层面的客观信息（\"myapp 用 FastAPI + SQLAlchemy 2.0 "
            "async\"、\"数据库迁移走 alembic\"）。"
        ),
    ),
    # ── Commitments (time-bound followups) ─────────────────────────
    "commitment": BucketDef(
        tag="commitment",
        target_file="MEMORY.md",
        section="## Active commitments",
        cap_chars=1000, cap_items=10,
        default_kind="commitment",
        description=(
            "时间相关的承诺 / 待办（\"明天 10am 提醒用户开会\"）。"
            "必须带 due_ts 字段，到期由 cron 触发 proactive 通知。"
        ),
    ),
    # ── Catch-all (never empty) ────────────────────────────────────
    "misc": BucketDef(
        tag="misc",
        target_file="MEMORY.md",
        section="## Other facts (recent)",
        cap_chars=1500, cap_items=12,
        default_kind="fact",
        description=(
            "兜底类。任何无法归到其他 10 个 bucket 的事实落这里 —— "
            "保证 fact 100% 进 .md，没有暗 fact。"
        ),
    ),
}


# Stable default — every writer that fails to classify ends here.
DEFAULT_BUCKET = "misc"


# ─── Helpers ──────────────────────────────────────────────────────


def resolve(bucket: str | None) -> BucketDef:
    """Return the ``BucketDef`` for ``bucket``, defaulting to
    ``misc`` when the input is empty / unknown.

    Never raises — the registry is closed and bucket misclassification
    is a runtime concern (LLM hallucinated a bad tag), not a coding
    error. Logging the fallback is the caller's choice.
    """
    if not bucket:
        return BUCKETS[DEFAULT_BUCKET]
    hit = BUCKETS.get(bucket)
    if hit is None:
        return BUCKETS[DEFAULT_BUCKET]
    return hit


def is_known(bucket: str | None) -> bool:
    """True iff ``bucket`` is registered. ``None`` / ``""`` → False."""
    return bool(bucket) and bucket in BUCKETS


def for_file(filename: str) -> list[BucketDef]:
    """Return every bucket that renders into ``filename``.

    Used by ``v2_renderer.render_persona_file`` to gather all buckets
    contributing to one .md file (e.g. USER.md collects both
    ``user_identity`` and ``user_preference``).
    """
    return [b for b in BUCKETS.values() if b.target_file == filename]


def known_files() -> list[str]:
    """All distinct ``.md`` files the registry knows about."""
    return sorted({b.target_file for b in BUCKETS.values()})


def render_for_prompt() -> str:
    """Render the registry as a prompt-friendly judgement list.

    Used by ``llm_extractor`` to inject "here are the bucket
    choices" into its extraction prompt — keeps the prompt
    automatically in sync with the registry, no copy-paste.
    """
    lines = ["可选 bucket（必须从下列里选，无合理归类用 misc）："]
    for b in BUCKETS.values():
        lines.append(f"  - {b.tag}: {b.description}")
    return "\n".join(lines)


__all__ = [
    "BucketDef",
    "BUCKETS",
    "DEFAULT_BUCKET",
    "resolve",
    "is_known",
    "for_file",
    "known_files",
    "render_for_prompt",
]
