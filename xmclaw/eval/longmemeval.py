"""LongMemEval mini-suite — hand-coded recall tasks.

LongMemEval (the real one — see ``OpenMOSS/LongMemEval`` on
HuggingFace, paper at https://arxiv.org/abs/2410.10813) probes a
chat assistant's ability to retrieve specific facts buried in a long
multi-turn dialogue history. The agent receives the full transcript +
a question; passes if it answers correctly.

This module ships a **mini** version — 7 hand-coded fixtures covering
the same task shape (multi-turn history → recall question) — so the
harness, CLI, and grader are exercisable WITHOUT pulling the 12k-task
HuggingFace dataset over the network. A follow-up ticket should land
the real adapter; the dataset name is recorded in
``LongMemEvalMiniSuite.UPSTREAM_DATASET`` for that work.

Grader: case-insensitive substring match of any ground-truth answer in
the agent's text. Both ``passed`` and ``score`` are derived from the
same hit; a partial-credit grader (e.g. token F1) is out-of-scope for
the mini suite.
"""
from __future__ import annotations

from typing import Any

from xmclaw.eval.harness import BenchmarkSuite, TaskCase


# Each fixture: (task_id, conversation_transcript, question, ground_truth_answers)
# The transcript is plain "User: ... / Assistant: ..." lines so the
# agent doesn't need any tool calls to recall — just attention. The
# answer set is a list because some questions are slightly fuzzy and
# more than one literal phrasing should count as correct.
_MINI_FIXTURES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "lme-mini-001",
        "User: My favourite colour is teal.\n"
        "Assistant: Got it, teal it is.\n"
        "User: What's the capital of France?\n"
        "Assistant: Paris.\n"
        "User: Nice. Anyway, my dog Bowser turned 5 last Tuesday.\n"
        "Assistant: Happy belated birthday to Bowser.\n",
        "What is my dog's name?",
        ("bowser",),
    ),
    (
        "lme-mini-002",
        "User: I'm flying to Lisbon on July 14.\n"
        "Assistant: Sounds great. Anything I should remember?\n"
        "User: Just remind me my hotel is the Pousada do Castelo.\n"
        "Assistant: Noted — Pousada do Castelo, Lisbon, July 14.\n"
        "User: Also, I prefer aisle seats.\n"
        "Assistant: Aisle preference logged.\n",
        "Which hotel am I staying at in Lisbon?",
        ("pousada do castelo", "pousada"),
    ),
    (
        "lme-mini-003",
        "User: Reading list this month: Borges, Le Guin, and Calvino.\n"
        "Assistant: A solid trio.\n"
        "User: Started with Calvino's Invisible Cities yesterday.\n"
        "Assistant: One of his best.\n"
        "User: Should finish by next Sunday.\n"
        "Assistant: Got it.\n",
        "Which book did I start reading?",
        ("invisible cities",),
    ),
    (
        "lme-mini-004",
        "User: My API key for the staging cluster is xmc-staging-abcd1234.\n"
        "Assistant: Stored as a reminder only — won't share it.\n"
        "User: Cool. The prod one is different — xmc-prod-zzzz9999.\n"
        "Assistant: Understood.\n"
        "User: Just so we're clear, prod is the long one.\n"
        "Assistant: Noted.\n",
        "What is the staging API key I mentioned?",
        ("xmc-staging-abcd1234",),
    ),
    (
        "lme-mini-005",
        "User: Today I ate oatmeal for breakfast and a falafel wrap for lunch.\n"
        "Assistant: Sounds balanced.\n"
        "User: Skipped dinner, snacked on almonds.\n"
        "Assistant: Got it.\n"
        "User: I want to track this — am I getting enough protein?\n"
        "Assistant: Probably borderline; the falafel and almonds help.\n",
        "What did I eat for lunch today?",
        ("falafel wrap", "falafel"),
    ),
    (
        "lme-mini-006",
        "User: My partner's birthday is March 22.\n"
        "Assistant: Got it.\n"
        "User: My mum's is November 4.\n"
        "Assistant: Noted.\n"
        "User: My own is the day after Christmas.\n"
        "Assistant: December 26 — fun timing.\n"
        "User: Anyway, I should plan something for my partner.\n"
        "Assistant: Sure, what do you have in mind?\n",
        "When is my partner's birthday?",
        ("march 22", "22 march", "march 22nd", "3/22"),
    ),
    (
        "lme-mini-007",
        "User: I'm allergic to penicillin and shellfish.\n"
        "Assistant: Important — noted.\n"
        "User: Also lactose intolerant but that's milder.\n"
        "Assistant: Got it.\n"
        "User: Doctor said to mention these on any new prescription.\n"
        "Assistant: Will keep it visible.\n",
        "Which two things am I allergic to?",
        ("penicillin", "shellfish"),
    ),
)


class LongMemEvalMiniSuite(BenchmarkSuite):
    """Hand-coded LongMemEval-style recall mini suite (7 cases)."""

    SUITE_ID = "longmemeval-mini"
    UPSTREAM_DATASET = "OpenMOSS/LongMemEval"
    """HuggingFace dataset id for the real suite — TODO: wire an adapter
    that pulls a streaming subset and reuses this class's grader."""

    @property
    def suite_id(self) -> str:
        return self.SUITE_ID

    def load_tasks(self, limit: int | None = None) -> list[TaskCase]:
        cases: list[TaskCase] = []
        fixtures = _MINI_FIXTURES
        if limit is not None:
            if limit < 0:
                raise ValueError(f"limit must be non-negative, got {limit}")
            fixtures = fixtures[:limit]
        for task_id, transcript, question, answers in fixtures:
            prompt = (
                "Given the following conversation history:\n\n"
                f"{transcript}\n"
                "Now answer this question based ONLY on the history above:\n"
                f"{question}\n"
            )
            cases.append(
                TaskCase(
                    task_id=task_id,
                    prompt=prompt,
                    expected_signals={
                        "answers": list(answers),
                        "question": question,
                    },
                    metadata={"source": "hand-coded"},
                )
            )
        return cases

    def grade(
        self, case: TaskCase, agent_text: str, **extra: Any,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Pass if any ground-truth answer appears (case-insensitively)
        as a substring of the agent's response.

        The grader exposes which answer matched in the metadata so the
        SuiteResult can be inspected post-hoc — useful when comparing
        two profiles to spot which questions one beats the other on.
        """
        answers = case.expected_signals.get("answers") or []
        if not isinstance(answers, list):
            return False, 0.0, {"error": "expected_signals.answers is not a list"}
        if not agent_text:
            return False, 0.0, {"matched": None, "reason": "empty agent text"}

        haystack = agent_text.lower()
        # Multi-answer cases (e.g. "two allergies") expect every listed
        # answer to appear; single-answer cases expect any to match.
        # We treat the answers list as ALL-must-match when len > 1 AND
        # the question contains "two" / "both" / "and" — otherwise ANY-
        # match. This keeps the mini grader honest without a separate
        # multi-answer schema.
        question = (case.expected_signals.get("question") or "").lower()
        all_must_match = len(answers) > 1 and any(
            cue in question for cue in (" two ", "both ", " and ")
        )

        if all_must_match:
            matched = [a for a in answers if str(a).lower() in haystack]
            if len(matched) == len(answers):
                return True, 1.0, {"matched": matched, "mode": "all"}
            return (
                False,
                len(matched) / len(answers),
                {"matched": matched, "mode": "all", "expected": answers},
            )

        # ANY-match. Score = 1.0 if hit, 0.0 otherwise. (Partial-credit
        # token F1 is a follow-up.)
        for ans in answers:
            if str(ans).lower() in haystack:
                return True, 1.0, {"matched": ans, "mode": "any"}
        return False, 0.0, {"matched": None, "mode": "any", "expected": answers}


__all__ = ["LongMemEvalMiniSuite"]
