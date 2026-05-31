"""§② trajectory→skill induction (Voyager add_new_skill).

Pins: a successful multi-step trajectory becomes a NEW skill candidate
(untrusted .proposed SKILL.md), with conservative guards + dedup, and
NEVER auto-promotes.
"""
from __future__ import annotations

import json

import pytest

from xmclaw.skills.inductor import (
    InductionProposal,
    SkillInductor,
    Trajectory,
    TrajectoryStep,
    trajectory_from_messages,
    write_induced_proposal,
)


class _FakeLLM:
    def __init__(self, response: dict | str):
        self._payload = (
            response if isinstance(response, str)
            else json.dumps(response, ensure_ascii=False)
        )
        self.calls = 0

    async def complete(self, *, messages, **kwargs):
        self.calls += 1
        payload = self._payload

        class _R:
            content = payload
        return _R()


def _good_traj() -> Trajectory:
    return Trajectory(
        goal="把项目里所有 PNG 压缩并生成报告",
        steps=(
            TrajectoryStep("file_glob", "find **/*.png → 42 files"),
            TrajectoryStep("bash", "pngquant each file"),
            TrajectoryStep("file_write", "wrote report.md"),
        ),
        outcome="42 images compressed, report.md written",
        ok=True,
        session_id="chat-abc",
    )


_GOOD_RESPONSE = {
    "skip": False,
    "reason": "可复用的批量图片压缩流程",
    "name": "batch-png-compress",
    "description": "批量压缩项目内 PNG 并生成压缩报告",
    "when_to_use": "用户要批量优化/压缩图片资源时",
    "body": (
        "1. 用 file_glob 找到所有目标 PNG\n"
        "2. 对每个文件用 pngquant 压缩\n"
        "3. 汇总前后体积,用 file_write 写一份 report.md\n"
        "4. 把节省的总体积报告给用户"
    ),
}


# ── guards ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_induce_skips_failed_trajectory():
    ind = SkillInductor(_FakeLLM(_GOOD_RESPONSE))
    traj = Trajectory(goal="x", steps=(TrajectoryStep("bash"), TrajectoryStep("file_write")), ok=False)
    assert await ind.induce(traj) is None


@pytest.mark.asyncio
async def test_induce_skips_trivial_trajectory():
    ind = SkillInductor(_FakeLLM(_GOOD_RESPONSE))
    # only one distinct tool → trivial
    traj = Trajectory(goal="x", steps=(TrajectoryStep("bash"), TrajectoryStep("bash")), ok=True)
    assert await ind.induce(traj) is None


@pytest.mark.asyncio
async def test_induce_skips_without_llm():
    ind = SkillInductor(None)
    assert await ind.induce(_good_traj()) is None


# ── synthesis ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_induce_synthesises_proposal():
    ind = SkillInductor(_FakeLLM(_GOOD_RESPONSE))
    p = await ind.induce(_good_traj())
    assert isinstance(p, InductionProposal)
    assert p.name == "batch-png-compress"
    assert "压缩" in p.description
    assert p.source_session_id == "chat-abc"
    assert len(p.body) >= 50


@pytest.mark.asyncio
async def test_induce_respects_llm_skip():
    ind = SkillInductor(_FakeLLM({"skip": True, "reason": "已有技能覆盖"}))
    assert await ind.induce(_good_traj()) is None


@pytest.mark.asyncio
async def test_induce_dedup_name_collision():
    ind = SkillInductor(_FakeLLM(_GOOD_RESPONSE))
    p = await ind.induce(
        _good_traj(),
        existing_skills=[("batch-png-compress", "already exists")],
    )
    assert p is None  # hard collision guard even if LLM ignored the hint


@pytest.mark.asyncio
async def test_induce_rejects_bad_name():
    bad = dict(_GOOD_RESPONSE, name="Bad Name With Spaces!")
    ind = SkillInductor(_FakeLLM(bad))
    assert await ind.induce(_good_traj()) is None


@pytest.mark.asyncio
async def test_induce_rejects_short_body():
    bad = dict(_GOOD_RESPONSE, body="too short")
    ind = SkillInductor(_FakeLLM(bad))
    assert await ind.induce(_good_traj()) is None


@pytest.mark.asyncio
async def test_induce_survives_bad_json():
    ind = SkillInductor(_FakeLLM("not json at all"))
    assert await ind.induce(_good_traj()) is None


# ── SKILL.md rendering ────────────────────────────────────────────


def test_to_skill_md_has_frontmatter():
    p = InductionProposal(
        name="x", description="does x", when_to_use="when x",
        body="step 1\nstep 2",
    )
    md = p.to_skill_md()
    assert md.startswith("---\n")
    assert "name: x" in md
    assert "description: does x" in md
    assert "when_to_use: when x" in md
    assert "step 1" in md


# ── writer (untrusted .proposed) ──────────────────────────────────


def test_write_induced_proposal_creates_untrusted_marker(tmp_path):
    p = InductionProposal(
        name="batch-png-compress", description="批量压缩 PNG",
        when_to_use="批量优化图片", body="1. glob\n2. compress\n3. report and summarise to user",
        source_session_id="chat-abc", reason="reusable",
    )
    out = write_induced_proposal(p, root=tmp_path)
    assert out is not None
    assert (out / "SKILL.md").is_file()
    marker = json.loads((out / ".proposed.json").read_text(encoding="utf-8"))
    assert marker["proposed_by"] == "induction"
    assert marker["evidence_count"] == 0  # never starts promoted
    assert marker["source_session_id"] == "chat-abc"
    body = (out / "SKILL.md").read_text(encoding="utf-8")
    assert "description: 批量压缩 PNG" in body


def test_write_induced_proposal_skips_existing(tmp_path):
    p = InductionProposal(name="dup", description="d", when_to_use="", body="x" * 60)
    assert write_induced_proposal(p, root=tmp_path) is not None
    # second write must NOT clobber.
    assert write_induced_proposal(p, root=tmp_path) is None


# ── trajectory extraction from session messages ───────────────────


def _m(role, content="", tool_calls=()):
    return {"role": role, "content": content, "tool_calls": tool_calls}


def test_trajectory_from_messages_extracts_success():
    msgs = [
        _m("user", "把所有图片压缩"),
        _m("assistant", "", [
            {"name": "file_glob", "args": {"pattern": "**/*.png"}},
            {"name": "bash", "args": {"command": "pngquant"}},
        ]),
        _m("tool", "42 files"),
        _m("assistant", "压缩完成,共 42 张,节省 12MB"),
    ]
    traj = trajectory_from_messages("chat-1", msgs)
    assert traj is not None
    assert traj.ok is True
    assert traj.goal == "把所有图片压缩"
    assert traj.distinct_tools == 2
    assert "节省" in traj.outcome


def test_trajectory_from_messages_flags_interrupted_as_failed():
    msgs = [
        _m("user", "做个大任务"),
        _m("assistant", "", [{"name": "bash", "args": {}}, {"name": "file_write", "args": {}}]),
        _m("assistant", "⚠️ 这一轮没能完成(出错或超时)。你的消息已经保留"),
    ]
    traj = trajectory_from_messages("chat-2", msgs)
    assert traj is not None
    assert traj.ok is False  # interrupt marker → not a success


def test_trajectory_from_messages_none_without_tools():
    msgs = [_m("user", "hi"), _m("assistant", "hello!")]
    assert trajectory_from_messages("chat-3", msgs) is None
