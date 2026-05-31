"""SkillInductor — turn a successful trajectory into a NEW skill candidate.

The Voyager (arXiv:2305.16291) ``add_new_skill`` move, adapted to
XMclaw's evidence-gated, honest-state evolution loop. Pre-induction
XMclaw could only **improve existing** skills (ReflectiveMutator/GEPA);
it had no path to **invent a brand-new** skill from a concretely-solved
task. This module supplies that path — see
``docs/audit/SKILL_SYSTEM_SOTA_RESEARCH_2026.md`` §②.

Pipeline (deliberately conservative):

  successful multi-step trajectory
    → guards (succeeded + ≥ MIN_STEPS distinct tools + a real outcome)
    → LLM synthesis: {name, description, when_to_use, body}  OR  skip
      (the prompt is shown the EXISTING skills and told to skip when one
       already covers the task — cheap dedup at the source)
    → validation (slug name, non-trivial body, no name collision)
    → InductionProposal  (caller writes it as an UNTRUSTED .proposed
       SKILL.md — never HEAD, never auto-promoted: anti-req #12)

Honest-state guardrails (mirrors ReflectiveMutator / StrategyDistiller):

* **Never auto-promotes.** ``induce`` returns a *proposal*; the writer
  stamps it ``.proposed.json`` (untrusted until human/grader review).
  No path goes induction → HEAD without the grader-driven controller.
* **Best-effort.** Any LLM / JSON / schema error → ``None`` (no
  proposal this pass). Never raises into the caller's loop.
* **LLM is the authority on reusability.** We don't claim a trajectory
  is "worth a skill" — we ask, show the existing library, and accept a
  skip. The synthesised skill then has to earn its keep via real
  invocation evidence like any other.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

#: a trajectory needs at least this many distinct tool steps to be worth
#: crystallising into a skill — fewer is "the agent just answered" or a
#: single tool call that's already a tool, not a reusable procedure.
MIN_STEPS = 2

#: skill-name slug rule — same as ``skill_propose`` so induced names
#: round-trip through the loader identically.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")

#: minimum non-blank body length — defends against empty-stub skills
#: (same floor ``skill_propose`` enforces).
_MIN_BODY_CHARS = 50


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    """One step of a solved task: the tool called + a short summary of
    what it did (args/result gist). Free-form; the LLM reads it."""

    tool: str
    summary: str = ""


@dataclass(frozen=True, slots=True)
class Trajectory:
    """A concretely-solved task, harvested from a session's history."""

    goal: str
    steps: tuple[TrajectoryStep, ...] = ()
    outcome: str = ""
    ok: bool = True
    session_id: str = ""

    @property
    def distinct_tools(self) -> int:
        return len({s.tool for s in self.steps if s.tool})


@dataclass(frozen=True, slots=True)
class InductionProposal:
    """A synthesised NEW skill candidate (not yet written / registered)."""

    name: str
    description: str
    when_to_use: str
    body: str
    reason: str = ""
    source_session_id: str = ""

    def to_skill_md(self) -> str:
        """Render the Anthropic/skills.sh SKILL.md form: YAML
        frontmatter (name + description [+ when_to_use]) then body."""
        fm = [f"name: {self.name}", f"description: {self.description}"]
        if self.when_to_use.strip():
            fm.append(f"when_to_use: {self.when_to_use.strip()}")
        return "---\n" + "\n".join(fm) + "\n---\n\n" + self.body.strip() + "\n"


class SkillInductor:
    """Synthesise skill candidates from solved trajectories via one LLM
    round-trip. Stateless apart from the LLM handle."""

    def __init__(self, llm) -> None:  # noqa: ANN001 — duck-typed provider
        self._llm = llm

    async def induce(
        self,
        traj: Trajectory,
        *,
        existing_skills: list[tuple[str, str]] | None = None,
        min_steps: int = MIN_STEPS,
    ) -> InductionProposal | None:
        """Return an :class:`InductionProposal` or ``None``.

        ``existing_skills`` is ``[(name, description), ...]`` — shown to
        the LLM so it can SKIP when a skill already covers this task, and
        used for a hard name-collision guard. Never raises."""
        # ── guards ───────────────────────────────────────────────────
        if self._llm is None:
            return None
        if not traj.ok:
            return None
        if traj.distinct_tools < max(1, int(min_steps)):
            return None
        if not (traj.goal or "").strip():
            return None

        existing = existing_skills or []
        existing_names = {n.strip().lower() for n, _ in existing}

        steps_text = "\n".join(
            f"  {i+1}. {s.tool}: {s.summary}".rstrip(": ").rstrip()
            for i, s in enumerate(traj.steps)
        )
        existing_text = (
            "\n".join(f"  - {n}: {d}" for n, d in existing[:60])
            or "  (none registered)"
        )
        system_prompt = (
            "你是技能归纳器(Voyager 风格)。下面是 agent **成功完成**的一次"
            "多步任务的轨迹。判断它是否值得固化成一个**可复用技能**。\n\n"
            "只返回纯 JSON(不要 markdown):\n"
            '{"skip": true/false, "reason": "简述", '
            '"name": "kebab-case 短名", '
            '"description": "一句话:这个技能做什么(给检索/触发用,要具体)", '
            '"when_to_use": "什么场景该用它", '
            '"body": "Markdown 步骤手册:把这次轨迹抽象成可复用的步骤,'
            '去掉一次性细节,留下通用流程;agent 之后会读 body 当指令、用'
            "自带工具执行\"}\n\n"
            "规则:\n"
            "1. **已有技能已覆盖就 skip**(看下方已注册技能列表)。\n"
            "2. 太琐碎/一次性/不可复用的也 skip(宁缺毋滥)。\n"
            "3. name 用 [a-z0-9][a-z0-9_-]{0,40} 短横线 slug。\n"
            "4. body 是**通用流程**,不是这次的流水账;≥50 字。\n"
            "5. 拿不准就 skip。"
        )
        user_prompt = (
            f"【任务目标】\n{traj.goal}\n\n"
            f"【执行轨迹(成功)】\n{steps_text or '  (no tool steps)'}\n\n"
            f"【结果】\n{traj.outcome or '(completed)'}\n\n"
            f"【已注册技能(若有覆盖则 skip)】\n{existing_text}"
        )
        try:
            from xmclaw.core.ir import Message
            resp = await self._llm.complete(messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt),
            ])
            raw = (resp.content or "").strip()
            if raw.startswith("```"):
                raw = raw.removeprefix("```json").removeprefix("```")
                raw = raw.removesuffix("```").strip()
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — induction is best-effort
            _log.debug("skill_inductor.llm_failed err=%s", exc)
            return None

        if not isinstance(data, dict) or data.get("skip") is True:
            return None
        name = str(data.get("name") or "").strip().lower()
        description = str(data.get("description") or "").strip()
        when_to_use = str(data.get("when_to_use") or "").strip()
        body = str(data.get("body") or "").strip()
        reason = str(data.get("reason") or "")[:200]

        # ── validation ───────────────────────────────────────────────
        if not _NAME_RE.fullmatch(name):
            _log.debug("skill_inductor.bad_name name=%r", name)
            return None
        if name in existing_names:
            # LLM ignored the dedup hint — hard guard.
            _log.debug("skill_inductor.name_collision name=%r", name)
            return None
        if not description:
            return None
        if len(body) < _MIN_BODY_CHARS:
            _log.debug("skill_inductor.body_too_short len=%d", len(body))
            return None

        return InductionProposal(
            name=name,
            description=description,
            when_to_use=when_to_use,
            body=body,
            reason=reason,
            source_session_id=traj.session_id,
        )


#: markers that mean a turn FAILED / was interrupted — a trajectory
#: ending in one of these is not a "success" worth crystallising.
_FAILURE_MARKERS = (
    "⚠️ 这一轮没能完成",
    "⚠️ 这一轮还没完成",
    "Hit the agent's tool-call budget",
)


def _msg_attr(msg, name: str, default=None):  # noqa: ANN001
    return (
        msg.get(name, default) if isinstance(msg, dict)
        else getattr(msg, name, default)
    )


def trajectory_from_messages(
    session_id: str,
    messages,  # noqa: ANN001 — list[Message] | list[dict]
    *,
    max_steps: int = 40,
) -> Trajectory | None:
    """Extract a :class:`Trajectory` from one session's message history.
    Pure + dict/dataclass tolerant so it's unit-testable without a live
    daemon. Returns ``None`` when the session doesn't look like a clean
    multi-step success.

      * goal    = first non-empty user message
      * steps   = every assistant tool_call, in order (tool + arg gist)
      * outcome = last assistant message that carries final text
      * ok      = has a final answer AND no failure/interrupt marker
    """
    if not messages:
        return None
    goal = ""
    steps: list[TrajectoryStep] = []
    outcome = ""
    for msg in messages:
        role = _msg_attr(msg, "role")
        content = _msg_attr(msg, "content") or ""
        if role == "user" and not goal and isinstance(content, str):
            g = content.strip()
            # skip scaffolding-only user messages
            if g and not g.startswith(("[GOAL-ANCHOR]", "[turn hint]")):
                goal = g
        if role == "assistant":
            tcs = _msg_attr(msg, "tool_calls") or ()
            for tc in tcs:
                tname = _msg_attr(tc, "name") or ""
                targs = _msg_attr(tc, "args") or {}
                if not tname:
                    continue
                gist = ""
                if isinstance(targs, dict) and targs:
                    gist = ", ".join(
                        f"{k}={str(v)[:40]}" for k, v in list(targs.items())[:3]
                    )
                steps.append(TrajectoryStep(tool=tname, summary=gist))
                if len(steps) >= max_steps:
                    break
            if isinstance(content, str) and content.strip():
                outcome = content.strip()  # last non-empty assistant text wins
    if not goal or not steps or not outcome:
        return None
    ok = not any(m in outcome for m in _FAILURE_MARKERS)
    return Trajectory(
        goal=goal,
        steps=tuple(steps),
        outcome=outcome[:600],
        ok=ok,
        session_id=session_id,
    )


def write_induced_proposal(
    proposal: InductionProposal, *, root: Path,
) -> Path | None:
    """Write an induced skill as an UNTRUSTED proposed SKILL.md under
    ``root/<name>/``, mirroring ``skill_propose`` exactly:

      * ``SKILL.md``        — YAML frontmatter + body
      * ``.proposed.json``  — trust marker (untrusted until review)

    Returns the skill dir on success, ``None`` when it already exists or
    the write fails (never raises). The marker is what keeps an
    auto-induced skill OUT of the trusted/HEAD path — it appears in
    ``skill_browse`` (so the agent can try it) but is flagged untrusted
    and never auto-promotes (anti-req #12)."""
    try:
        skill_dir = root / proposal.name
        if skill_dir.exists():
            return None  # don't clobber an existing skill / proposal
        skill_dir.mkdir(parents=True, exist_ok=False)
        (skill_dir / "SKILL.md").write_text(
            proposal.to_skill_md(), encoding="utf-8",
        )
        marker = {
            "proposed_by": "induction",
            "proposed_at": time.time(),
            "evidence_count": 0,
            "promote_after_evidence": 3,
            "source_session_id": proposal.source_session_id,
            "reason": proposal.reason,
            "note": (
                "Auto-induced from a successful trajectory (Voyager-style). "
                "Trust=untrusted until manual review removes this marker."
            ),
        }
        (skill_dir / ".proposed.json").write_text(
            json.dumps(marker, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return skill_dir
    except Exception as exc:  # noqa: BLE001 — never raise into the loop
        _log.warning("skill_inductor.write_failed err=%s", exc)
        return None


__all__ = [
    "SkillInductor",
    "Trajectory",
    "TrajectoryStep",
    "InductionProposal",
    "write_induced_proposal",
    "trajectory_from_messages",
    "MIN_STEPS",
]
