"""Honesty guard for proactive intent predictions (2026-05-29).

A proactive ``intent_prediction`` message is just a string surfaced
to the user — no code runs behind it. The LLM layer occasionally
emitted a first-person work-claim ("我已经在处理…处理完后我会给你
合并摘要") that lies to the user (nothing is being processed). The
guard drops such predictions. Honest offers ("要不要我帮你…？") pass.
"""
from __future__ import annotations

import pytest

from xmclaw.cognition.intent_engine.engine import _claims_false_action


# ─── false claims must be flagged ─────────────────────────────────


@pytest.mark.parametrize("msg", [
    "收到，我已经在处理这批1760+条记录的语义去重了，处理完后我会给你合并摘要",
    "我正在分析你的失败记录",
    "我现在帮你整理记忆",
    "我会把重复的条目合并掉",
    "处理完后我会给你一份摘要",
    "我这就帮你去重",
    "稍后给你结果",
    "我已经帮你清理完了",
    "我马上运行一下",
])
def test_first_person_work_claims_are_flagged(msg):
    assert _claims_false_action(msg) is True, (
        f"should flag false-action claim: {msg!r}"
    )


# ─── honest offers must pass ──────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "要不要我帮你把这批记录去重？",
    "我注意到有很多重复条目，需要的话我可以帮你合并",
    "看起来你在整理记忆，是否需要我协助？",
    "你可能想复盘最近的失败，要我列一下吗？",
    "需不需要我帮你看一下？",
    "如果你愿意，我可以帮你整理这些记录",
    "想不想让我分析一下失败模式？",
])
def test_honest_offers_pass(msg):
    assert _claims_false_action(msg) is False, (
        f"should NOT flag honest offer: {msg!r}"
    )


# ─── edge cases ───────────────────────────────────────────────────


def test_empty_message_is_not_a_claim():
    assert _claims_false_action("") is False
    assert _claims_false_action(None) is False  # type: ignore[arg-type]


def test_neutral_observation_without_self_action_passes():
    """A pure observation with no 'I will/am doing' verb is fine."""
    assert _claims_false_action("最近有几次执行失败，可能值得复盘。") is False


# ─── integration: LLM layer drops false-claim predictions ─────────


@pytest.mark.asyncio
async def test_llm_layer_drops_false_action_predictions(tmp_path):
    """End-to-end: a prediction whose proposed_message claims work
    in progress must NOT reach the prediction cache."""
    import json as _json

    from xmclaw.cognition.intent_engine.engine import IntentEngine
    from xmclaw.core.ir import Message  # noqa: F401 — import smoke

    # Fake LLM returns one honest offer + one false claim.
    class _FakeLLM:
        async def complete(self, *, messages, **kwargs):
            class _R:
                content = _json.dumps({
                    "predictions": [
                        {
                            "intent_type": "honest_offer",
                            "confidence": 0.9,
                            "rationale": "user idle",
                            "proposed_message": "要不要我帮你整理一下？",
                            "urgency": "low",
                        },
                        {
                            "intent_type": "false_claim",
                            "confidence": 0.9,
                            "rationale": "user dedup",
                            "proposed_message": "我已经在处理去重了，处理完后我会给你摘要",
                            "urgency": "normal",
                        },
                    ],
                }, ensure_ascii=False)
            return _R()

    from xmclaw.cognition.intent_engine.store import IntentStore
    eng = IntentEngine(
        IntentStore(tmp_path / "intent.db"), llm=_FakeLLM(),
    )
    # Seed the context window so _run_llm_layer doesn't early-return.
    eng._context_window.append({"type": "user_message", "ts": 1.0})

    await eng._run_llm_layer()

    cached_types = {p.intent_type for p in eng._prediction_cache}
    assert "honest_offer" in cached_types, "honest offer should survive"
    assert "false_claim" not in cached_types, (
        "false work-claim prediction must be dropped"
    )
