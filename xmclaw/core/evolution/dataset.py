"""Dataset construction for skill mutation — train/val/holdout split.

Direct port of ``hermes-self-evolution/evolution/datasets/dataset_builder.py:
20-86, 96-201`` (schema + 50/25/25 split). Adapted for XMclaw's
``BehavioralEvent`` substrate: instead of mining external Claude Code
session logs, we source examples from the live event bus's persisted log
(``~/.xmclaw/v2/events.db``), which already records every ``GRADER_VERDICT``
event that the Honest Grader produced.

Key advantage over Hermes's approach: the dataset is **self-grading** —
every (user_msg, response) pair already has a XMclaw HonestGrader score
attached, so the mutator's fitness function uses the same ground-truth
checks the runtime uses. No LLM-on-LLM rubric, no keyword-overlap proxy.
This is the structural difference between our evolution and Hermes's.

Public API:
    * :class:`EvalExample` — one (input, expected, score) tuple
    * :class:`EvalDataset` — train/val/holdout 50/25/25 split
    * :func:`build_dataset_from_history` — fetch + split from events.db
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EvalExample:
    """One labelled example for the mutator's fitness function.

    Mirrors hermes ``dataset_builder.py:20-36`` ``EvalExample`` shape.
    """
    task_input: str
    expected_behavior: str
    baseline_score: float           # score the prior skill version got
    difficulty: str = "medium"      # "easy" | "medium" | "hard"
    category: str = "general"
    source: str = "events.db"


@dataclass
class EvalDataset:
    """Train / val / holdout split per hermes ``evolve_skill.py:208-227``.

    The 50/25/25 ratio is the default; callers can override via
    :meth:`split` if a smaller dataset would leave validation empty.
    Hermes's ratio was chosen so val is large enough to early-stop GEPA
    and holdout is large enough to detect over-fit. Don't change without
    a reason.
    """
    examples: list[EvalExample]
    train: list[EvalExample] = field(default_factory=list)
    val: list[EvalExample] = field(default_factory=list)
    holdout: list[EvalExample] = field(default_factory=list)

    @classmethod
    def split(
        cls,
        examples: list[EvalExample],
        *,
        train_pct: float = 0.5,
        val_pct: float = 0.25,
        seed: int = 0,
    ) -> "EvalDataset":
        """Shuffle ``examples`` then carve into train/val/holdout."""
        if not examples:
            return cls(examples=[], train=[], val=[], holdout=[])
        items = list(examples)
        random.Random(seed).shuffle(items)
        n = len(items)
        n_train = max(1, int(n * train_pct))
        n_val = max(1, int(n * val_pct)) if n - n_train > 0 else 0
        train = items[:n_train]
        val = items[n_train : n_train + n_val]
        holdout = items[n_train + n_val :]
        return cls(examples=items, train=train, val=val, holdout=holdout)

    @property
    def is_empty(self) -> bool:
        return not self.examples

    @property
    def has_holdout(self) -> bool:
        return bool(self.holdout)


def build_dataset_from_history(
    *,
    events_db_path: Path,
    skill_id: str | None = None,
    limit: int = 200,
    min_baseline_score: float = 0.0,
    seed: int = 0,
) -> EvalDataset:
    """Read GRADER_VERDICT events from sqlite event log → dataset.

    Each GRADER_VERDICT event in ``events.db`` carries:
      - The user message that triggered the turn
      - The skill / variant id active for the turn
      - The HonestGrader score (0..1)
      - Evidence list

    We pivot those into ``EvalExample`` rows so DSPy's GEPA can learn
    "what high-score behaviour looks like" without requiring an external
    LLM-judge. Hermes's importer mines arbitrary Claude Code sessions
    (``external_importers.py:157-416``); ours mines our own bus, which
    is structurally cleaner.

    Args:
        events_db_path: usually ``~/.xmclaw/v2/events.db``
        skill_id: filter to this skill's verdicts. ``None`` = all skills.
        limit: max events to fetch (most recent first).
        min_baseline_score: drop low-score examples — they don't represent
            "good behaviour" we want the mutator to preserve.
        seed: shuffle seed for reproducible splits.

    Returns:
        :class:`EvalDataset` with ~50/25/25 train/val/holdout split.
    """
    if not events_db_path.exists():
        return EvalDataset(examples=[])

    conn = sqlite3.connect(str(events_db_path))
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT payload FROM events
             WHERE type = 'grader_verdict'
             ORDER BY ts DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        # Schema may differ on older daemons; bail to empty dataset
        # rather than crash the mutator.
        return EvalDataset(examples=[])
    finally:
        conn.close()

    examples: list[EvalExample] = []
    for (raw_payload,) in rows:
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload)
        except (json.JSONDecodeError, TypeError):
            continue

        if skill_id is not None and payload.get("skill_id") != skill_id:
            continue

        score = float(payload.get("score", 0.0))
        if score < min_baseline_score:
            continue

        user_msg = payload.get("user_message") or payload.get("input") or ""
        expected = payload.get("expected_behavior") or payload.get("output") or ""
        if not user_msg or not expected:
            continue

        examples.append(
            EvalExample(
                task_input=str(user_msg)[:2000],
                expected_behavior=str(expected)[:2000],
                baseline_score=score,
                category=str(payload.get("category", "general")),
                source="events.db",
            )
        )

    return EvalDataset.split(examples, seed=seed)
