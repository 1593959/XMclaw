"""Unit tests for AutobiographicalMemory (Sprint 1 Track B)."""
from __future__ import annotations

import pytest

from xmclaw.cognition.autobiographical_memory import (
    AutobiographicalMemory,
)


@pytest.fixture
def mem(tmp_path):
    return AutobiographicalMemory(root=tmp_path)


# ── Recording ───────────────────────────────────────────────────────


def test_record_fact_new(mem):
    fid = mem.record_fact(
        kind="preference", subject="user",
        predicate="likes", value="黑客松",
    )
    assert fid
    facts = mem.facts_about("user")
    assert len(facts) == 1
    assert facts[0].predicate == "likes"
    assert facts[0].value == "黑客松"


def test_record_fact_idempotent_upsert(mem):
    # Same fact twice — should not dupe.
    f1 = mem.record_fact(
        kind="preference", subject="user",
        predicate="likes", value="X",
    )
    f2 = mem.record_fact(
        kind="preference", subject="user",
        predicate="likes", value="X",
    )
    assert f1 == f2  # same row updated
    assert len(mem.facts_about("user")) == 1


def test_record_fact_confidence_merge(mem):
    """Two records with confidence 0.7 → merged ≈ 0.85."""
    mem.record_fact(
        kind="fact", subject="user", predicate="is",
        value="developer", confidence=0.7,
    )
    mem.record_fact(
        kind="fact", subject="user", predicate="is",
        value="developer", confidence=0.7,
    )
    facts = mem.facts_about("user")
    assert facts[0].confidence > 0.7
    assert facts[0].confidence <= 0.99


def test_record_person_new(mem):
    pid = mem.record_person(name="何鹏", relationship="朋友", importance=0.8)
    assert pid
    ppl = mem.people()
    assert len(ppl) == 1
    assert ppl[0].name == "何鹏"
    assert ppl[0].relationship == "朋友"


def test_record_person_idempotent(mem):
    mem.record_person(name="何鹏", relationship="朋友")
    mem.record_person(name="何鹏", relationship="朋友")
    assert len(mem.people()) == 1


def test_record_project_new(mem):
    pid = mem.record_project(
        name="XMclaw", status="active",
        current_focus="multimodal UI",
    )
    assert pid
    projs = mem.projects()
    assert len(projs) == 1
    assert projs[0].current_focus == "multimodal UI"


def test_record_project_update(mem):
    mem.record_project(name="XMclaw", current_focus="vision")
    mem.record_project(name="XMclaw", current_focus="proactive agent")
    projs = mem.projects()
    assert len(projs) == 1
    assert projs[0].current_focus == "proactive agent"


# ── Rule-based extractor ────────────────────────────────────────────


def test_extract_chinese_self_facts(mem):
    n = mem.extract_from_message("我是何鹏，我喜欢黑客松，我讨厌早起。")
    assert n >= 3  # 我是 + 我喜欢 + 我讨厌
    facts = mem.facts_about("user")
    preds = {f.predicate for f in facts}
    assert "is" in preds
    assert "likes" in preds
    assert "dislikes" in preds


def test_extract_english_self_facts(mem):
    mem.extract_from_message(
        "I'm working on XMclaw. I love coffee. My name is Alice.",
    )
    facts = mem.facts_about("user")
    preds = {f.predicate for f in facts}
    assert "working_on" in preds
    assert "likes" in preds
    assert "name" in preds


def test_extract_person_mention(mem):
    mem.extract_from_message("我朋友小6子今天来了。")
    ppl = mem.people()
    names = [p.name for p in ppl]
    assert "小6子" in names
    target = next(p for p in ppl if p.name == "小6子")
    assert target.relationship == "朋友"


def test_extract_empty_message_no_op(mem):
    assert mem.extract_from_message("") == 0
    assert mem.extract_from_message(None) == 0  # type: ignore[arg-type]


def test_extract_idempotent(mem):
    msg = "我是何鹏。"
    mem.extract_from_message(msg)
    mem.extract_from_message(msg)
    # Single fact, not duped
    facts = [f for f in mem.facts_about("user") if f.predicate == "is"]
    assert len(facts) == 1


# ── Recall / summary ────────────────────────────────────────────────


def test_summarize_empty(mem):
    assert mem.summarize_for_prompt() == ""


def test_summarize_with_facts(mem):
    mem.record_fact(
        kind="fact", subject="user", predicate="name",
        value="何鹏",
    )
    mem.record_fact(
        kind="preference", subject="user", predicate="likes",
        value="黑客松",
    )
    mem.record_person(name="小6子", relationship="朋友")
    mem.record_project(name="XMclaw", current_focus="multimodal UI")
    summary = mem.summarize_for_prompt()
    assert "What I remember" in summary
    assert "name" in summary
    assert "何鹏" in summary
    assert "likes" in summary
    assert "黑客松" in summary
    assert "小6子" in summary
    assert "XMclaw" in summary


def test_summary_groups_multiple_likes(mem):
    mem.record_fact(
        kind="preference", subject="user",
        predicate="likes", value="黑客松",
    )
    mem.record_fact(
        kind="preference", subject="user",
        predicate="likes", value="代码",
    )
    summary = mem.summarize_for_prompt()
    # Both values appear in the same likes bullet
    assert "黑客松" in summary
    assert "代码" in summary


def test_summary_caps_max_facts(mem):
    for i in range(40):
        mem.record_fact(
            kind="fact", subject="user",
            predicate=f"trait_{i}", value="v",
        )
    summary = mem.summarize_for_prompt(max_facts=5)
    # Roughly 5 facts → 5 lines + headers + ≤ 6-8 lines total
    assert summary.count("**") < 12  # markdown bold pairs


# ── Forget ──────────────────────────────────────────────────────────


def test_forget_fact(mem):
    mem.record_fact(
        kind="preference", subject="user",
        predicate="likes", value="X",
    )
    assert mem.forget_fact(
        kind="preference", subject="user", predicate="likes",
    ) is True
    assert mem.facts_about("user") == []


def test_forget_person(mem):
    mem.record_person(name="老史")
    assert mem.forget_person("老史") is True
    assert mem.people() == []


def test_forget_unknown_returns_false(mem):
    assert mem.forget_fact(
        kind="x", subject="y", predicate="z",
    ) is False
    assert mem.forget_person("nobody") is False


# ── Subject normalisation ───────────────────────────────────────────


def test_subject_case_insensitive(mem):
    mem.record_fact(
        kind="fact", subject="User", predicate="is", value="x",
    )
    # Should find under lowercase "user"
    assert len(mem.facts_about("user")) == 1
    assert len(mem.facts_about("USER")) == 1


# ── Wave 25.6: ProfileExtractor → autobio bridge ────────────────


@pytest.mark.asyncio
async def test_subscribe_to_bus_ingests_user_profile_event(mem):
    """USER_PROFILE_UPDATED events should land as facts about 'user'
    in the autobio tables — bridging the LLM extractor pipeline."""
    from xmclaw.core.bus import EventType, make_event
    from xmclaw.core.bus.memory import InProcessEventBus

    bus = InProcessEventBus()
    sub = mem.subscribe_to_bus(bus)
    assert sub is not None

    ev = make_event(
        session_id="chat-test",
        agent_id="main",
        type=EventType.USER_PROFILE_UPDATED,
        payload={
            "file_path": "/tmp/USER.md",
            "delta_count": 2,
            "session_id": "chat-test",
            "deltas": [
                {
                    "kind": "preference",
                    "text": "Prefers Chinese language responses",
                    "confidence": 0.85,
                },
                {
                    "kind": "style",
                    "text": "Uses casual tone with occasional slang",
                    "confidence": 0.75,
                },
            ],
        },
    )
    await bus.publish(ev)
    await bus.drain()

    facts = mem.facts_about("user")
    assert len(facts) == 2
    kinds = {f.kind for f in facts}
    assert kinds == {"preference", "style"}
    by_kind = {f.kind: f for f in facts}
    assert "Chinese" in by_kind["preference"].value
    assert by_kind["preference"].source == "profile_extractor"
    assert 0.84 <= by_kind["preference"].confidence <= 0.86


@pytest.mark.asyncio
async def test_subscribe_to_bus_ignores_unrelated_events(mem):
    from xmclaw.core.bus import EventType, make_event
    from xmclaw.core.bus.memory import InProcessEventBus

    bus = InProcessEventBus()
    mem.subscribe_to_bus(bus)

    other = make_event(
        session_id="chat-x",
        agent_id="main",
        type=EventType.USER_MESSAGE,
        payload={"content": "I like cookies"},
    )
    await bus.publish(other)
    await bus.drain()

    assert mem.facts_about("user") == []


@pytest.mark.asyncio
async def test_subscribe_to_bus_skips_empty_deltas(mem):
    from xmclaw.core.bus import EventType, make_event
    from xmclaw.core.bus.memory import InProcessEventBus

    bus = InProcessEventBus()
    mem.subscribe_to_bus(bus)

    ev = make_event(
        session_id="chat-x",
        agent_id="main",
        type=EventType.USER_PROFILE_UPDATED,
        payload={
            "deltas": [
                {"kind": "preference", "text": "", "confidence": 0.9},
                {"kind": "style", "text": "   ", "confidence": 0.9},
                {"kind": "habit", "text": "real fact", "confidence": 0.9},
            ],
        },
    )
    await bus.publish(ev)
    await bus.drain()

    facts = mem.facts_about("user")
    assert len(facts) == 1
    assert facts[0].value == "real fact"


def test_subscribe_to_bus_returns_none_when_no_subscribe_method(mem):
    """Buses without ``subscribe`` (e.g. unwired test stubs) shouldn't
    crash; the subscribe call is a no-op."""

    class _NoSubBus:
        pass

    assert mem.subscribe_to_bus(_NoSubBus()) is None
