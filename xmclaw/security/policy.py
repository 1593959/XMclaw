"""Policy application for prompt-injection scanning (Epic #14 phase 2).

``scan_text`` and ``redact`` in ``prompt_scanner`` are pure building blocks.
This module adds the thin layer that every callsite would otherwise
re-implement: run the scan, decide whether the policy demands action, build
the structured event payload the bus consumes, and return a result object
that tells the caller the (possibly modified) content + whether to block.

Having this in one place matters because we want to scan more than just
tool output. Future integrations (SOUL / PROFILE / AGENTS.md content that
gets loaded as system context, memory-store recall summaries, web-fetch
bodies delivered outside the tool path) need the *same* policy, the *same*
event shape, and the *same* block semantics — otherwise a gap opens every
time a new injection vector lands. One helper, everyone calls it.

The module is deliberately bus-agnostic: it returns the event payload as a
dict for the caller to publish. We don't import ``InProcessEventBus`` here
— that would drag the security package into the core layering and require
an async context where callers like a future profile loader might not
have one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xmclaw.security.prompt_scanner import (
    PolicyMode,
    ScanResult,
    redact,
    scan_text,
)


# Stable source-tag strings. Callsites pass one of these into ``source`` so
# downstream consumers (UI, dashboards, alerting) can group by origin
# without re-parsing free-form strings. Add new tags here, not at callsites.
SOURCE_TOOL_RESULT = "tool_result"
SOURCE_PROFILE = "agent_profile"       # SOUL.md / PROFILE.md / AGENTS.md
SOURCE_MEMORY_RECALL = "memory_recall"
SOURCE_WEB_FETCH = "web_fetch"


# B-187: per-source false-positive suppressions. memory_recall
# pulls past conversation transcripts that legitimately contain
# ``\nAssistant:`` / ``\nHuman:`` role markers (the format of past
# conversations, not a forgery attempt). Pre-B-187 every recall
# tripped ``anthropic_human_tag`` and emitted noise — joint audit
# (2026-05-02) found 20/24 prompt_injection events were exactly
# this false positive. We suppress the role-forgery pattern only
# for memory_recall; tool_result / web_fetch / agent_profile keep
# the strict rules because external content really might forge a
# system role to inject.
_SOURCE_SUPPRESSIONS: dict[str, frozenset[str]] = {
    SOURCE_MEMORY_RECALL: frozenset({"anthropic_human_tag", "inst_block"}),
}


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """What a callsite needs after applying the policy.

    * ``content`` — the string to actually inject into the prompt. For
      ``detect_only`` this is the original; for ``redact`` it's the
      redacted form; for ``block`` it's the original (callers check
      ``blocked`` first and short-circuit).
    * ``blocked`` — True iff the policy is BLOCK and findings were
      observed. Callers MUST abort the turn / skip the injection when
      this is set; they should not use ``content``.
    * ``scan`` — the raw :class:`ScanResult` for callers that want to
      log extra detail or surface findings to the UI.
    * ``event`` — the structured payload for
      ``EventType.PROMPT_INJECTION_DETECTED``. ``None`` when the scan
      had no findings (no event should be emitted). When non-None, the
      caller publishes it on whatever bus it has access to.
    """

    content: str
    blocked: bool
    scan: ScanResult
    event: dict[str, Any] | None = field(default=None)


def apply_policy(
    text: str,
    *,
    policy: PolicyMode,
    source: str,
    extra: dict[str, Any] | None = None,
) -> PolicyDecision:
    """Scan ``text``, apply ``policy``, and return what the caller needs.

    ``source`` is one of the ``SOURCE_*`` constants in this module — the
    stable tag that goes into the event's ``source`` field. Callers that
    want to add per-call context (tool_call_id, tool_name, profile_path,
    memory_id) pass it as ``extra``; it's merged into the event payload
    without validation, so keep it JSON-serialisable.

    Called hundreds of times per session if a tool loop is chatty, so the
    hot path stays cheap: a single ``scan_text`` call, a few dict
    constructions only when findings exist, no copies otherwise.

    B-187 source-targeted suppressions: certain patterns generate
    false positives on certain trusted sources. We suppress them
    inline so the scanner stays strict on truly untrusted inputs
    (tool_result, web_fetch) without polluting events.db with
    role-prefix matches on memory recall content.
    """
    suppress = _SOURCE_SUPPRESSIONS.get(source)
    scan = scan_text(text, suppress_patterns=suppress)

    if not scan.any_findings:
        # Fast path: nothing to emit, nothing to redact. Return the
        # original text so the caller can splice without a branch.
        return PolicyDecision(content=text, blocked=False, scan=scan)

    acted = policy in (PolicyMode.REDACT, PolicyMode.BLOCK)
    # Whether the policy literally blocks is decided per-branch below; we
    # don't pre-compute it because each return path encodes its own
    # `blocked` flag. Keeping the boolean here would be dead code.

    event: dict[str, Any] = {
        "source": source,
        "policy": policy.value,
        "findings": [
            {
                "pattern_id": f.pattern_id,
                "severity": f.severity.value,
                "category": f.category,
                # Truncate so a 100KB attack string doesn't bloat
                # the bus. 200 chars is enough to eyeball the
                # payload; full text stays in the tool_result
                # event (or on the filesystem if it came from a
                # fetched file).
                "match": f.match[:200],
            }
            for f in scan.findings
        ],
        "invisible_chars": scan.invisible_chars,
        "scanned_length": scan.scanned_length,
        "categories": scan.categories(),
        "acted": acted,
    }
    if extra:
        # Caller-supplied context (tool_call_id, profile_path, etc.) wins
        # over nothing, but not over the core fields above — we reserve
        # those for the scanner's contract.
        for k, v in extra.items():
            event.setdefault(k, v)

    if policy == PolicyMode.REDACT:
        return PolicyDecision(
            content=redact(text, scan),
            blocked=False,
            scan=scan,
            event=event,
        )

    if policy == PolicyMode.BLOCK:
        # Caller gets the original content back but MUST check ``blocked``
        # and refuse to use it. We don't pre-redact here because callers
        # in block mode typically want to surface the categories to the
        # user / log, not a sanitised version.
        return PolicyDecision(
            content=text,
            blocked=True,
            scan=scan,
            event=event,
        )

    # DETECT_ONLY: emit the event, pass content through untouched.
    return PolicyDecision(
        content=text,
        blocked=False,
        scan=scan,
        event=event,
    )
