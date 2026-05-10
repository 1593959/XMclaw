"""LongMemEval real-corpus suite — pulls from HuggingFace.

This is the production sibling of :mod:`xmclaw.eval.longmemeval` (the
hand-coded mini suite). Where the mini suite ships 7 fixtures so the
harness/CLI/grader are exercisable offline, this suite pulls the
official ``OpenMOSS/LongMemEval`` corpus from HuggingFace so XMclaw
can produce A/B numbers for the README and release notes.

Paper: https://arxiv.org/abs/2410.10813.
Dataset: https://huggingface.co/datasets/OpenMOSS/LongMemEval.

Why a separate module instead of folding into ``longmemeval.py``:

* The mini suite must be importable on every install (zero extras).
  Pulling in ``datasets`` would make the eval harness depend on the
  HuggingFace stack, which we don't want for the always-on path.
* This module lazy-imports ``datasets`` *inside* ``load_tasks`` so the
  daemon (which imports ``xmclaw.eval`` for SUITE_REGISTRY) never
  pays the import cost — only ``xmclaw eval run longmemeval`` does.

Grader: case-insensitive substring match of the ground-truth answer in
the agent's text (1.0 / 0.0 binary). The official benchmark uses GPT-4
as a judge; our substring heuristic is the same shortcut the mini suite
takes (good enough to spot regressions; a follow-up may wire a real
LLM judge through ``HonestGrader``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from xmclaw.eval.harness import BenchmarkSuite, TaskCase


# Where we tell HuggingFace to cache the dataset. Using XMclaw's own
# workspace dir means a fresh laptop only fetches it once and keeps it
# isolated from the user's global ``~/.cache/huggingface`` (which they
# may share with other projects / wipe independently).
# Patch A (2026-05-10): resolved lazily via paths.eval_cache_dir() so
# ``XMC_DATA_DIR`` env reroutes also relocate the HF cache. Pre-fix
# the module-level Path.home() captured at import time was immune to
# env overrides, defeating the unified-paths anti-req.
def _hf_cache_dir() -> Path:
    from xmclaw.utils.paths import eval_cache_dir
    return eval_cache_dir("longmemeval")


# Error hint when the optional extra isn't installed. Surfacing this from
# ``load_tasks`` (not at import time) means the daemon and ``xmclaw eval
# list`` keep working without the extra — only ``xmclaw eval run
# longmemeval`` trips the install prompt.
_INSTALL_HINT = (
    "LongMemEval real-corpus suite requires the 'eval-hf' extra. "
    "Install with: pip install 'xmclaw[eval-hf]'"
)


class LongMemEvalSuite(BenchmarkSuite):
    """Real-corpus LongMemEval suite — pulls from HuggingFace on demand.

    The first ``load_tasks`` call downloads the dataset (cached under
    ``~/.xmclaw/v2/eval_cache/longmemeval/``); subsequent calls are
    fast. ``limit`` is honoured before any TaskCase construction so
    a smoke run with ``--limit 5`` doesn't iterate the whole corpus.
    """

    SUITE_ID = "longmemeval"
    UPSTREAM_DATASET = "OpenMOSS/LongMemEval"
    UPSTREAM_SPLIT = "test"

    @property
    def suite_id(self) -> str:
        return self.SUITE_ID

    def load_tasks(self, limit: int | None = None) -> list[TaskCase]:
        """Fetch the LongMemEval test split and convert each row to a
        ``TaskCase``.

        Lazy-imports ``datasets`` so missing-extra environments don't
        crash on ``import xmclaw.eval``.
        """
        if limit is not None and limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")

        # Point HF at our cache dir BEFORE importing/calling. Setting the
        # env var also affects the underlying ``huggingface_hub`` client.
        _cache = _hf_cache_dir()
        _cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_DATASETS_CACHE", str(_cache))

        try:
            from datasets import load_dataset  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via mock
            raise ImportError(_INSTALL_HINT) from exc

        ds = load_dataset(
            self.UPSTREAM_DATASET,
            split=self.UPSTREAM_SPLIT,
            cache_dir=str(_cache),
        )

        cases: list[TaskCase] = []
        for idx, row in enumerate(ds):
            if limit is not None and len(cases) >= limit:
                break
            cases.append(self._row_to_case(idx, row))
        return cases

    @staticmethod
    def _row_to_case(idx: int, row: dict[str, Any]) -> TaskCase:
        """Convert one HuggingFace row to a ``TaskCase``.

        Pulled out so tests can construct cases directly without touching
        ``datasets``. Schema reference (from the dataset card):

        * ``question_id``: stable string id
        * ``question``: the recall question
        * ``answer``: ground truth (string)
        * ``haystack_sessions``: list of multi-turn session transcripts
        * ``evidence_session_ids``: which sessions contain the answer
        * ``question_type``: e.g. ``single-session-user`` /
          ``multi-session`` / ``temporal-reasoning``
        """
        task_id = str(row.get("question_id") or f"longmemeval-{idx}")
        question = str(row.get("question") or "")
        answer = str(row.get("answer") or "")
        evidence_ids = row.get("evidence_session_ids") or []

        sessions_text = _flatten_sessions(row.get("haystack_sessions") or [])

        prompt = (
            "Given the following multi-session conversation:\n\n"
            f"{sessions_text}\n\n"
            f"Question: {question}\n\n"
            "Answer concisely."
        )

        return TaskCase(
            task_id=task_id,
            prompt=prompt,
            expected_signals={
                "answer": answer,
                "evidence_session_ids": list(evidence_ids),
            },
            metadata={
                "question_type": str(
                    row.get("question_type") or "single-session-user"
                ),
            },
        )

    def grade(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Case-insensitive substring match on the ground-truth answer.

        Score is 1.0 on hit, 0.0 otherwise — same coarse heuristic the
        mini suite uses. Empty agent text always fails (``score=0``)
        even if the ground truth is the empty string.
        """
        answer = case.expected_signals.get("answer") or ""
        if not isinstance(answer, str):
            return False, 0.0, {
                "matched": False,
                "error": "expected_signals.answer is not a string",
            }
        if not agent_text:
            return False, 0.0, {"matched": False, "reason": "empty agent text"}
        if not answer.strip():
            return False, 0.0, {
                "matched": False,
                "reason": "empty ground truth — cannot grade",
            }

        matched = answer.lower() in agent_text.lower()
        return matched, (1.0 if matched else 0.0), {"matched": matched}


def _flatten_sessions(sessions: Any) -> str:
    """Concatenate the session transcripts into a single prompt-friendly
    block.

    The HF schema stores each session as a list of turn dicts (``role`` +
    ``content``); we render them as ``Session N:\\nUser: ...\\nAssistant:
    ...`` so the model sees the boundaries. Defensive against shape
    drift — strings are passed through, anything else is ``str()``'d.
    """
    if not sessions:
        return "(no prior conversation)"

    parts: list[str] = []
    for i, session in enumerate(sessions, start=1):
        parts.append(f"Session {i}:")
        if isinstance(session, str):
            parts.append(session)
        elif isinstance(session, list):
            for turn in session:
                if isinstance(turn, dict):
                    role = str(turn.get("role") or "user").capitalize()
                    content = str(turn.get("content") or "")
                    parts.append(f"{role}: {content}")
                else:
                    parts.append(str(turn))
        else:
            parts.append(str(session))
        parts.append("")  # blank line between sessions
    return "\n".join(parts).rstrip()


__all__ = ["LongMemEvalSuite"]
