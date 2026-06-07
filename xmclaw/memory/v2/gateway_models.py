"""Cognitive Memory Gateway — data models (Phase 1).

Pure data classes. No I/O, no LLM calls. Consumed by gateway.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xmclaw.memory.v2.models import FactKindStr, FactScopeStr


@dataclass(slots=True)
class Observation:
    """One raw input fed into the Cognitive Memory Gateway.

    Extractors (regex / LLM / cognition) no longer write directly to
    the store. They produce Observations and submit them to the
    Gateway, which decides what to do.
    """

    source: str
    """Where this observation came from.

    Values:
      * ``user_msg``           — raw user message (regex layer)
      * ``assistant_response`` — agent's own reply
      * ``tool_result``        — tool invocation output
      * ``post_sampling``      — ExtractLessonsHook / ExtractFactsHook
      * ``cognition``          — reflection_cycle consolidate
      * ``manual``             — user explicitly said "记住 X"
    """

    content: str
    """Raw text payload. For ``post_sampling`` this is the extracted
    fact string; for ``user_msg`` it is the user message itself."""

    turn_id: str
    """Session-scoped turn identifier (usually ``session_id``)."""

    timestamp: float
    """Unix epoch seconds when the observation was created."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extra context that helps the Gateway route / prioritise:

      * ``kind_hint``        — suggested FactKind (extractor's opinion)
      * ``scope_hint``       — suggested FactScope
      * ``bucket_hint``      — suggested bucket label
      * ``confidence_hint``  — extractor's confidence [0, 1]
      * ``tool_name``        — for tool_result observations
      * ``tool_success``     — bool, did the tool succeed?
    """


@dataclass(slots=True)
class CognitiveDigest:
    """Output of the Gateway THINK step.

    The THINK LLM consumes an Observation + neighbouring facts + recent
    turn context and produces a digest that tells the DECIDE step what
    to do.
    """

    worth_remembering: bool
    """False → drop immediately (e.g. ephemeral command, already known)."""

    action: Literal["ADD", "UPDATE", "DELETE", "NOOP"]
    """What the Gateway should do with this observation."""

    synthesized_text: str
    """Normalised, compact statement (NOT a verbatim copy of the source).

    Example: user said "我那个网店是 pw310 的" → synthesized_text
    might become ``用户运营的网店域名为 pw310``.
    """

    target_fact_id: str | None = None
    """When action is UPDATE or DELETE, the id of the existing fact that
    should be superseded / contradicted."""

    kind: FactKindStr = "lesson"
    scope: FactScopeStr = "project"
    bucket: str = ""
    confidence: float = 0.8
    reason: str = ""
    """Human-readable rationale for the action (audit trail)."""


@dataclass(slots=True)
class RecallPlan:
    """Output of the Gateway recall-gate step.

    Before we search the store, we decide (a) whether search is needed
    at all and (b) which buckets / kinds are relevant.  This prevents
    the "inject 4 vaguely-related facts into every turn" noise.
    """

    need_recall: bool
    """True → run hybrid search; False → skip entirely."""

    relevant_buckets: list[str] = field(default_factory=list)
    """When non-empty, restrict recall to these buckets (AND any
    unbucketed facts that are vector-close).  Empty list means
    "no bucket restriction — search everything"."""

    relevant_kinds: list[FactKindStr] = field(default_factory=list)
    """Optional kind filter.  Empty = no restriction."""

    query_expansion: str = ""
    """Optional rephrased / expanded query for better vector recall.
    When empty the original user message is used."""


@dataclass(slots=True)
class RecallResult:
    """One fact returned by the Gateway recall pipeline."""

    fid: str
    text: str
    bucket: str
    kind: str
    similarity: float
    ts_first: float
