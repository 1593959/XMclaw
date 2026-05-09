"""Sprint 3 Iron Rule #2 — staging mechanics: candidate → 4 gates → bundle.

This module ships the *mechanics* of staged promotion: a `Candidate`
payload, four explicit gate functions (size / growth / structure /
holdout), and a `run_gates` aggregator producing a `GateBundle`. The
goal is to make promotion **never inline** — the controller (Sprint 3
follow-up) will compose this with `promotion_policy.decide` to gate
the existing `EvolutionController.maybe_promote` path.

Iron Rule #2 (`docs/EVOLUTION_HONEST_STATE.md`):

    "Staging → gate → explicit promote. The orchestrator never mutates
    `SkillRegistry` HEAD inline. Always: candidate dir → 4 gates →
    explicit `promote()` call (auto-policy or human)."

Out of scope on purpose (deferred to controller-integration follow-up):

* Wiring `run_gates` into `EvolutionController.maybe_promote`.
* Emitting bus events for gate verdicts / promotion holds.
* CLI surface for inspecting staged candidates / forcing decisions.
* On-disk staging directories (today this is purely in-memory mechanics).

The four gates correspond to the four anti-bloat checks the
Hermes-style mutator validators perform, restated in Iron Rule #2
terms so the controller can reason about them as a uniform bundle.

Public API:

* :class:`Candidate` — frozen payload describing a proposed skill
  version awaiting promotion.
* :class:`GateResult` / :class:`GateBundle` — result containers with
  pass/fail aggregation logic.
* :func:`gate_size_limit` / :func:`gate_growth_limit` /
  :func:`gate_structure_validation` / :func:`gate_holdout_test` —
  individual gate functions; pure, side-effect-free.
* :func:`run_gates` — executes all four gates with a configurable
  holdout policy and returns a :class:`GateBundle`.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Literal

GateStatus = Literal["passed", "failed", "skipped"]
"""Per-gate outcome. ``skipped`` means "the gate could not run", e.g.
no holdout test was registered. Whether a skip blocks promotion is
the policy layer's job (see `promotion_policy.py`)."""

# Default thresholds — tuned permissively so plain-Python skills with
# a docstring + a function body pass without ceremony. The point of
# the gates is to catch ballooning / structural drift, not to nag.
_DEFAULT_MAX_KB = 100
_DEFAULT_MAX_GROWTH_RATIO = 2.0


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict.

    ``evidence`` carries the raw numbers / parser findings the gate
    consulted — surfacing these is half the point of staging, since
    the policy layer (and the eventual `xmclaw evolve review` UI)
    needs to explain *why* a candidate was held.
    """

    name: str
    status: GateStatus
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class GateBundle:
    """Aggregate verdict over all gates for a single candidate.

    The bundle preserves gate order so the controller can render the
    same sequence the policy reasoned over. ``passed_all`` is strict:
    a single ``failed`` flips it. ``skipped`` does not count as a
    failure here — the policy decides whether a skip is acceptable.
    """

    results: tuple[GateResult, ...]

    @property
    def passed_all(self) -> bool:
        """True iff no gate failed. Skips are tolerated at this layer.

        The policy module (`is_high_risk` / `decide`) is the place
        that escalates skips into human review.
        """
        return all(r.status != "failed" for r in self.results)

    @property
    def failed_any(self) -> bool:
        """True iff at least one gate failed."""
        return any(r.status == "failed" for r in self.results)

    @property
    def skipped_any(self) -> bool:
        """True iff at least one gate was skipped (couldn't run)."""
        return any(r.status == "skipped" for r in self.results)

    def by_name(self, name: str) -> GateResult | None:
        for r in self.results:
            if r.name == name:
                return r
        return None


@dataclass(frozen=True, slots=True)
class Candidate:
    """A skill version awaiting promotion.

    ``id`` is opaque to staging — callers (the controller / mutator)
    compute it however they like (uuid, content hash, …) so long as
    it is stable for the lifetime of the candidate.
    ``source_text`` is the canonical skill body; gates run against
    this. ``metadata`` is a free-form dict for caller-supplied tags
    (proposal source, mutator version, etc).
    """

    id: str
    skill_id: str
    version: int
    source_text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


# ---------------------------------------------------------------------------
# Individual gates. Each is pure: same inputs → same `GateResult`.
# ---------------------------------------------------------------------------


def gate_size_limit(c: Candidate, max_kb: int = _DEFAULT_MAX_KB) -> GateResult:
    """Reject candidates whose body exceeds ``max_kb`` kilobytes.

    Measured on UTF-8 bytes (1 KB = 1024 bytes), matching how the
    bus serialiser counts size. Hermes uses 60 KB; we default higher
    (100 KB) to leave room for skills that include long examples,
    on the assumption that the structure gate catches genuine bloat.
    """
    size_bytes = len(c.source_text.encode("utf-8"))
    size_kb = size_bytes / 1024.0
    if size_kb > max_kb:
        return GateResult(
            name="size_limit",
            status="failed",
            evidence={"size_bytes": size_bytes, "size_kb": size_kb, "max_kb": max_kb},
            reason=f"candidate is {size_kb:.1f} KB; max is {max_kb} KB",
        )
    return GateResult(
        name="size_limit",
        status="passed",
        evidence={"size_bytes": size_bytes, "size_kb": size_kb, "max_kb": max_kb},
    )


def gate_growth_limit(
    c: Candidate,
    head_source: str | None,
    max_ratio: float = _DEFAULT_MAX_GROWTH_RATIO,
) -> GateResult:
    """Reject candidates that balloon vs the current HEAD.

    ``head_source`` is the registry's currently-promoted body (or
    `None` if the candidate is the first version of this skill).
    First versions can never fail this gate — they have nothing to
    grow against. Any ratio ≤ ``max_ratio`` passes; anything strictly
    above fails.
    """
    if head_source is None:
        return GateResult(
            name="growth_limit",
            status="passed",
            evidence={"reason": "first_version", "max_ratio": max_ratio},
        )
    head_bytes = len(head_source.encode("utf-8"))
    cand_bytes = len(c.source_text.encode("utf-8"))
    if head_bytes == 0:
        # Treat empty HEAD as a degenerate first-version — pass.
        return GateResult(
            name="growth_limit",
            status="passed",
            evidence={
                "head_bytes": 0,
                "candidate_bytes": cand_bytes,
                "max_ratio": max_ratio,
                "reason": "empty_head",
            },
        )
    ratio = cand_bytes / head_bytes
    evidence = {
        "head_bytes": head_bytes,
        "candidate_bytes": cand_bytes,
        "ratio": ratio,
        "max_ratio": max_ratio,
    }
    if ratio > max_ratio:
        return GateResult(
            name="growth_limit",
            status="failed",
            evidence=evidence,
            reason=f"candidate is {ratio:.2f}× HEAD; max is {max_ratio}×",
        )
    return GateResult(name="growth_limit", status="passed", evidence=evidence)


# Patterns the structure gate looks for. We accept either a Python
# skill (parses as a module + has at least one function or class) or
# a YAML/markdown skill (has a leading `---` frontmatter or `# Skill`
# header). A candidate that matches *neither* is structurally
# suspect — that's the only failure path here.
_PYTHON_HINT = re.compile(r"^\s*(def |class |from |import )", re.MULTILINE)
_FRONTMATTER_HINT = re.compile(r"^---\s*$", re.MULTILINE)
_MD_HEADER_HINT = re.compile(r"^#\s+\S", re.MULTILINE)


def gate_structure_validation(c: Candidate) -> GateResult:
    """Reject candidates with no recognisable skill structure.

    Two acceptance paths:

    1. **Python skill** — ``ast.parse`` succeeds AND the module body
       contains at least one ``def`` / ``class`` / ``import`` /
       ``from`` statement.
    2. **YAML/markdown skill** — body contains either a ``---``
       frontmatter delimiter or a markdown ``#`` header.

    A candidate that satisfies neither is "shapeless prose" and
    fails the gate. Empty bodies always fail.
    """
    text = c.source_text
    findings: list[str] = []
    has_python = False
    has_doc = False

    if not text.strip():
        return GateResult(
            name="structure_validation",
            status="failed",
            evidence={"empty": True, "findings": ["empty"]},
            reason="candidate body is empty",
        )

    # Path 1: Python.
    if _PYTHON_HINT.search(text):
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            findings.append(f"python_parse_error: {exc.msg} (line {exc.lineno})")
        else:
            has_python = any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom))
                for node in tree.body
            )
            if not has_python:
                findings.append("python_no_top_level_def_or_import")

    # Path 2: YAML/markdown.
    if _FRONTMATTER_HINT.search(text) or _MD_HEADER_HINT.search(text):
        has_doc = True

    evidence = {
        "has_python_structure": has_python,
        "has_doc_structure": has_doc,
        "findings": findings,
        "size_bytes": len(text.encode("utf-8")),
    }

    if not has_python and not has_doc:
        return GateResult(
            name="structure_validation",
            status="failed",
            evidence=evidence,
            reason="no recognisable skill structure (python or markdown/yaml)",
        )

    return GateResult(
        name="structure_validation",
        status="passed",
        evidence=evidence,
    )


def gate_holdout_test(c: Candidate, test_id: str | None) -> GateResult:
    """Holdout-test gate.

    This is the *interface* layer. Today the actual holdout-runner
    lives in the grader; staging only needs to know whether a
    candidate had a holdout test attached, and to surface that as a
    `skipped` (no test) / `passed` (test_id was provided) result.

    The controller-integration follow-up will replace the inline
    "passed" branch with a real call into the holdout runner. For
    now, providing a `test_id` is taken as a promise from the caller
    that the holdout has already been run and passed elsewhere — the
    Iron Rule #1 path (`IronRule1Gate.holdout_signal`) already
    enforces this end-to-end.
    """
    if test_id is None:
        return GateResult(
            name="holdout_test",
            status="skipped",
            evidence={"test_id": None},
            reason="no holdout test attached to candidate",
        )
    return GateResult(
        name="holdout_test",
        status="passed",
        evidence={"test_id": test_id},
    )


# ---------------------------------------------------------------------------
# Aggregator.
# ---------------------------------------------------------------------------


HoldoutPolicy = Literal["require", "skip_if_absent", "skip_always"]
"""How `run_gates` should treat a missing `test_id`:

* ``"require"`` — a missing test_id flips holdout to ``failed``.
* ``"skip_if_absent"`` — default; missing test_id stays ``skipped``
  and the policy layer decides whether that's OK.
* ``"skip_always"`` — never run the holdout gate; always ``skipped``.
"""


def run_gates(
    c: Candidate,
    head_source: str | None = None,
    test_id: str | None = None,
    holdout_policy: HoldoutPolicy = "skip_if_absent",
) -> GateBundle:
    """Run all four gates and return their bundled verdict.

    The order is fixed (size, growth, structure, holdout) so the
    bundle is deterministic for a given candidate + policy. Gates do
    not short-circuit each other — every gate runs, so the policy
    layer sees the complete picture and can decide whether multiple
    soft failures together warrant rejection.
    """
    size = gate_size_limit(c)
    growth = gate_growth_limit(c, head_source)
    structure = gate_structure_validation(c)

    if holdout_policy == "skip_always":
        holdout = GateResult(
            name="holdout_test",
            status="skipped",
            evidence={"policy": "skip_always"},
            reason="holdout disabled by policy",
        )
    elif holdout_policy == "require" and test_id is None:
        holdout = GateResult(
            name="holdout_test",
            status="failed",
            evidence={"policy": "require", "test_id": None},
            reason="holdout policy is 'require' but no test_id was provided",
        )
    else:
        holdout = gate_holdout_test(c, test_id)

    return GateBundle(results=(size, growth, structure, holdout))


__all__ = [
    "Candidate",
    "GateBundle",
    "GateResult",
    "GateStatus",
    "HoldoutPolicy",
    "gate_growth_limit",
    "gate_holdout_test",
    "gate_size_limit",
    "gate_structure_validation",
    "run_gates",
]
