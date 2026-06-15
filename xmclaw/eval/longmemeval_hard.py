"""LongMemEval-Hard — a discriminating recall/reasoning mini suite.

``longmemeval-mini`` saturates at 100% on any competent model (short
transcript + substring recall), so it can't *drive* loop tuning — there's
no headroom to detect a regression or an improvement. This suite targets
the failure modes a mediocre agentic loop actually trips on, so the score
lands BELOW 100% and becomes a usable A/B signal:

* **abstain** — the answer is NOT in the history. The right behaviour is
  to say "not mentioned / I don't know", NOT fabricate a specific value.
  Hallucination is the classic failure; we fail any answer that states
  the trap value.
* **recency** — a fact is stated then later overridden. The agent must
  use the LATEST value, not the stale one.
* **multihop** — requires combining two facts from different turns.
* **distractor** — plausible-but-wrong candidates sit next to the answer.
* **all** — every listed answer must appear (multi-answer questions).

Grading stays deterministic substring matching (no LLM judge) so the
score is stable across runs — the load-bearing property for A/B.
"""
from __future__ import annotations

from typing import Any

from xmclaw.eval.harness import BenchmarkSuite, TaskCase

# (task_id, transcript, question, mode, expected[], trap[])
#   expected — substrings that must appear (semantics depend on mode)
#   trap      — substrings whose presence means the agent fell for the
#               wrong/stale answer (used by abstain + recency)
_HARD_FIXTURES: list[tuple[str, str, str, str, list[str], list[str]]] = [
    (
        "hard-abstain-bloodtype",
        "User: I'm 34, I live in Seattle, and I'm allergic to penicillin.\n"
        "Assistant: Noted.\n",
        "What is my blood type?",
        "abstain", [], ["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-", "type O", "type A", "type B"],
    ),
    (
        "hard-abstain-petname",
        "User: I have a golden retriever and I work as a dentist.\n"
        "Assistant: Got it.\n",
        "What is my dog's name?",
        "abstain", [], ["rex", "max", "buddy", "charlie", "bella", "luna", "his name is", "her name is", "named"],
    ),
    (
        "hard-recency-flight",
        "User: My flight is at 3pm on Friday.\n"
        "Assistant: Okay.\n"
        "User: Actually the airline moved it — it's now at 6pm.\n"
        "Assistant: Updated.\n",
        "What time is my flight?",
        "recency", ["6"], ["3pm", "3 pm", "at 3", "three"],
    ),
    (
        "hard-recency-city",
        "User: I'm planning to move to Denver next year.\n"
        "Assistant: Nice.\n"
        "User: Change of plans — we decided on Austin instead.\n"
        "Assistant: Got it.\n",
        "Which city am I moving to?",
        "recency", ["austin"], ["denver"],
    ),
    (
        "hard-multihop-allergy",
        "User: I'm allergic to peanuts.\n"
        "Assistant: Noted.\n"
        "User: My sister has the exact same allergies as me.\n"
        "Assistant: Understood.\n",
        "What is my sister allergic to?",
        "any", ["peanut"], [],
    ),
    (
        "hard-multihop-count",
        "User: I have 2 dogs.\n"
        "Assistant: Cute.\n"
        "User: I also have 1 cat and 3 fish.\n"
        "Assistant: A full house!\n",
        "How many pets do I have in total?",
        "any", ["6", "six"], [],
    ),
    (
        "hard-distractor-birthplace",
        "User: I've visited Paris, Rome, and Tokyo. I was born in Lisbon and "
        "currently live in Berlin.\n"
        "Assistant: Well travelled!\n",
        "Which city was I born in?",
        "distractor", ["lisbon"], ["paris", "rome", "tokyo", "berlin"],
    ),
    (
        "hard-distractor-job",
        "User: My brother is a lawyer, my wife is a nurse, and I'm a "
        "software engineer.\n"
        "Assistant: Noted.\n",
        "What is my job?",
        "distractor", ["software", "engineer"], ["lawyer", "nurse"],
    ),
    (
        "hard-negation-coffee",
        "User: I never drink coffee. In the morning I always have green tea.\n"
        "Assistant: Healthy!\n",
        "What do I drink in the morning?",
        "any", ["tea"], [],
    ),
    (
        "hard-all-meds",
        "User: I take two daily medications: metformin and lisinopril.\n"
        "Assistant: Noted.\n",
        "List both of my medications.",
        "all", ["metformin", "lisinopril"], [],
    ),
]

_ABSTAIN_MARKERS = (
    "not mentioned", "not stated", "no mention", "don't know", "do not know",
    "doesn't say", "does not say", "no information", "not provided",
    "not specified", "can't tell", "cannot tell", "isn't mentioned",
    "wasn't mentioned", "unknown", "没有提到", "未提及", "不知道", "没有说",
    "未说明", "无法确定", "没有提供",
)


class LongMemEvalHardSuite(BenchmarkSuite):
    """Discriminating recall/reasoning suite (abstain / recency / multihop)."""

    SUITE_ID = "longmemeval-hard"

    @property
    def suite_id(self) -> str:
        return self.SUITE_ID

    def load_tasks(self, limit: int | None = None) -> list[TaskCase]:
        rows = _HARD_FIXTURES
        if limit is not None:
            if limit < 0:
                raise ValueError(f"limit must be non-negative, got {limit}")
            rows = rows[:limit]
        out: list[TaskCase] = []
        for task_id, transcript, question, mode, expected, trap in rows:
            instruction = (
                "Answer ONLY from the conversation history above. If the "
                "history does not contain the answer, say you don't know — "
                "do NOT guess."
                if mode == "abstain" else
                "Answer ONLY from the conversation history above."
            )
            prompt = (
                "Given the following conversation history:\n\n"
                f"{transcript}\n{instruction}\n\nQuestion: {question}\n"
            )
            out.append(TaskCase(
                task_id=task_id,
                prompt=prompt,
                expected_signals={
                    "mode": mode, "expected": expected,
                    "trap": trap, "question": question,
                },
                metadata={"source": "hand-coded-hard", "mode": mode},
            ))
        return out

    def grade(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        sig = case.expected_signals
        mode = str(sig.get("mode") or "any")
        expected = [str(x).lower() for x in (sig.get("expected") or [])]
        trap = [str(x).lower() for x in (sig.get("trap") or [])]
        hay = (agent_text or "").lower()
        if not hay:
            return False, 0.0, {"reason": "empty agent text", "mode": mode}

        trap_hit = next((t for t in trap if t in hay), None)

        if mode == "abstain":
            # Pass iff the agent did NOT fabricate the trap value. Bonus
            # signal: an explicit abstention marker.
            if trap_hit is not None:
                return False, 0.0, {"mode": mode, "fabricated": trap_hit}
            abstained = any(m in hay for m in _ABSTAIN_MARKERS)
            return True, 1.0, {"mode": mode, "abstained": abstained}

        if mode == "recency":
            # Must state the latest value AND not the stale one.
            has_new = all(e in hay for e in expected) if expected else False
            if has_new and trap_hit is None:
                return True, 1.0, {"mode": mode}
            return False, (0.5 if has_new else 0.0), {
                "mode": mode, "used_stale": trap_hit, "stated_current": has_new,
            }

        if mode == "all":
            matched = [e for e in expected if e in hay]
            if len(matched) == len(expected) and expected:
                return True, 1.0, {"mode": mode, "matched": matched}
            denom = len(expected) or 1
            return False, len(matched) / denom, {"mode": mode, "matched": matched}

        # "any" / "distractor": any expected substring present, and (for
        # distractor) the answer shouldn't be ONLY a distractor.
        if any(e in hay for e in expected):
            return True, 1.0, {"mode": mode}
        return False, 0.0, {"mode": mode, "expected": expected}


__all__ = ["LongMemEvalHardSuite"]
