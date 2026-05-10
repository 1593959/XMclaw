"""Jarvis Phase 6.4 — GoalGenerator + AutonomyPolicy.

Self-generated goal proposals gated by an autonomy slider (0..100). The
operator sets ``AutonomyPolicy.level``; everything else (per-hour action
caps, action-flag bundles, notification toggles) is computed
deterministically so behavior is auditable end-to-end.

This module is intentionally standalone:

* It defines a frozen ``Goal`` dataclass that mirrors
  ``xmclaw.cognition.state.Goal`` but stays independently addressable to
  avoid an import cycle with the in-flight Phase 6 work on ``state.py``.
* The generator never reaches into the daemon, providers, or bus —
  ``GoalGenerator`` only accepts a ``cognitive_state`` snapshot
  (anything with a ``current_goals`` collection) and the policy.
* No bus events are emitted here; the caller is responsible for
  routing returned goals through the existing planner / dispatcher.

See ``docs/JARVIS_PHASE_6_DESIGN.md`` §3.5 for the spec.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- constants


_UNLIMITED_CAP = -1

# Goal categories the generator emits. Kept as plain strings (not an Enum)
# so the dataclass stays trivially JSON-serializable for audit logs.
_CATEGORY_MAINTENANCE = "maintenance"
_CATEGORY_EXPLORATION = "exploration"
_CATEGORY_SOCIAL = "social"
_CATEGORY_GENERAL = "general"


# ---------------------------------------------------------------- AutonomyPolicy


@dataclass(frozen=True)
class AutonomyPolicy:
    """0..100 autonomy slider distilled into a frozen, auditable policy.

    Operators only ever set ``level``; ``from_level`` deterministically
    derives every other field so two policies with the same level are
    bit-identical.

    Level semantics (matching design doc §3.5 backward-compat matrix):

    * **0** — fully manual. All action flags off, ``cap=0``, no
      proactive notifications, no self experiments, no weekly summary.
      Equivalent to Phase 5 behavior (``cognition.continuous_loop``
      observes only).
    * **1..49** — observe + minimal nudge. Proactive notification stays
      off; only ``weekly_summary_enabled`` ramps in around level 30.
    * **50..74** — proactive prompting. ``proactive_notification_enabled``
      and ``weekly_summary_enabled`` on; action flags still off; cap=0.
      System suggests but never acts.
    * **75..99** — guarded autonomy. ``can_modify_files`` and
      ``can_run_long_processes`` come on with a per-hour cap of 5.
      Self experiments toggle on at >=75.
    * **100** — full Jarvis. All flags on, ``cap=-1`` (unlimited).
    """

    level: int
    autonomous_action_per_hour_cap: int  # 0 / 5 / -1 (unlimited)
    can_modify_files: bool
    can_send_messages: bool
    can_run_long_processes: bool
    proactive_notification_enabled: bool
    self_experiment_enabled: bool
    weekly_summary_enabled: bool

    # ---- factory --------------------------------------------------------

    @classmethod
    def from_level(cls, level: int) -> "AutonomyPolicy":
        """Build a deterministic policy from a 0..100 slider value.

        Out-of-range inputs are clamped to ``[0, 100]`` rather than
        raising, so a UI passing a slightly malformed value can never
        surprise the generator into a crash mid-tick.
        """
        if level < 0:
            level = 0
        elif level > 100:
            level = 100

        # Defaults for level 0 — the conservative bottom of the ramp.
        cap = 0
        can_modify_files = False
        can_send_messages = False
        can_run_long_processes = False
        proactive_notification = False
        self_experiment = False
        weekly_summary = False

        if level >= 30:
            # Light observation perks — non-actionable digest only.
            weekly_summary = True

        if level >= 50:
            # Proactive prompting on; still no actions.
            proactive_notification = True
            weekly_summary = True

        if level >= 75:
            # Guarded autonomy — limited writes + long-process work.
            cap = 5
            can_modify_files = True
            can_run_long_processes = True
            self_experiment = True

        if level >= 90:
            # Add messaging only near the top — sending things to other
            # humans is the highest-blast-radius bucket.
            can_send_messages = True

        if level >= 100:
            cap = _UNLIMITED_CAP
            can_modify_files = True
            can_send_messages = True
            can_run_long_processes = True
            proactive_notification = True
            self_experiment = True
            weekly_summary = True

        return cls(
            level=level,
            autonomous_action_per_hour_cap=cap,
            can_modify_files=can_modify_files,
            can_send_messages=can_send_messages,
            can_run_long_processes=can_run_long_processes,
            proactive_notification_enabled=proactive_notification,
            self_experiment_enabled=self_experiment,
            weekly_summary_enabled=weekly_summary,
        )

    # ---- gate -----------------------------------------------------------

    def can_act(self, goal: "Goal") -> bool:
        """Goal-against-policy gate (without rate-limit accounting).

        Maps ``goal.category`` to the action flag(s) it requires:

        * ``maintenance`` → ``can_modify_files`` (typical: clean temp,
          compact journal).
        * ``exploration`` → ``can_run_long_processes`` (probing tools /
          re-reading large diffs is allowed to take a while).
        * ``social`` → ``can_send_messages`` (the only category that
          contacts humans).
        * ``general`` → falls through to the most permissive check —
          allowed iff the policy can do *anything* autonomous.

        Rate limiting is enforced separately by the generator so the
        same policy object can be queried statelessly from many call
        sites.
        """
        if self.autonomous_action_per_hour_cap == 0:
            return False

        category = goal.category
        if category == _CATEGORY_MAINTENANCE:
            return self.can_modify_files
        if category == _CATEGORY_EXPLORATION:
            return self.can_run_long_processes
        if category == _CATEGORY_SOCIAL:
            return self.can_send_messages
        # general / unknown → most permissive bucket
        return (
            self.can_modify_files
            or self.can_send_messages
            or self.can_run_long_processes
        )


# ---------------------------------------------------------------- Goal


@dataclass(frozen=True)
class Goal:
    """A single self-generated goal.

    Mirrors ``xmclaw.cognition.state.Goal`` but stays independently
    addressable so this module never imports ``state`` (avoiding a
    cycle while ``state.py`` is still in flight for sibling Phase 6
    work).
    """

    id: str
    name: str
    description: str
    priority: int  # 1..10, 10 highest
    parent_goal_id: str | None = None
    sub_goal_ids: tuple[str, ...] = field(default_factory=tuple)
    completion_criteria: dict[str, Any] = field(default_factory=dict)
    deadline: float | None = None
    category: str = _CATEGORY_GENERAL
    generated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------- GoalGenerator


class GoalGenerator:
    """Three classes of self-generated goals, gated by ``AutonomyPolicy``.

    Each generator method returns ``[]`` when the policy disallows the
    relevant action bucket; ``generate_all()`` further dedupes against
    the cognitive state's existing goals and enforces a per-hour cap.

    The generator is deliberately tiny — it produces *candidates*. The
    decision to hand them to the planner / dispatcher is the caller's.
    """

    def __init__(
        self,
        cognitive_state: Any,
        policy: AutonomyPolicy,
        recent_goal_window_s: float = 3600.0,
    ) -> None:
        self._state = cognitive_state
        self._policy = policy
        self._window_s = float(recent_goal_window_s)
        # Per-instance ledger: timestamps of goals minted in the current
        # rolling window. ``generate_all`` trims this on every call so
        # we don't grow without bound.
        self._minted_at: list[float] = []

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _new_id() -> str:
        return f"goal-{uuid.uuid4().hex[:10]}"

    def _now(self) -> float:
        return time.time()

    def _mint(
        self,
        *,
        name: str,
        description: str,
        priority: int,
        category: str,
        completion_criteria: dict[str, Any] | None = None,
    ) -> Goal:
        return Goal(
            id=self._new_id(),
            name=name,
            description=description,
            priority=priority,
            category=category,
            completion_criteria=dict(completion_criteria or {}),
        )

    def _existing_goals(self) -> list[Any]:
        """Snapshot the ``current_goals`` list off the cognitive state.

        Tolerant of shapes: dataclass with attribute, dict-backed fake
        used in unit tests, or anything with a ``current_goals``
        sequence.
        """
        if isinstance(self._state, dict):
            existing = self._state.get("current_goals", [])
        else:
            existing = getattr(self._state, "current_goals", [])
        if existing is None:
            return []
        return list(existing)

    def _is_duplicate(self, goal: Goal, existing: list[Any]) -> bool:
        """Reject if any existing goal carries the same ``name`` *or*
        the same ``description``.

        We don't compare ids (those are random) and we don't try to do
        semantic dedup (that's the planner's job) — name + description
        already catches the only case we care about: re-firing the same
        canned recommendation on every tick.
        """
        for cand in existing:
            cand_name = self._read_field(cand, "name", "")
            cand_desc = self._read_field(cand, "description", "")
            if cand_name and cand_name == goal.name:
                return True
            if cand_desc and cand_desc == goal.description:
                return True
        return False

    @staticmethod
    def _read_field(obj: Any, name: str, default: Any) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    def _trim_minted_window(self) -> None:
        cutoff = self._now() - self._window_s
        self._minted_at = [t for t in self._minted_at if t >= cutoff]

    def _within_rate_limit(self) -> bool:
        cap = self._policy.autonomous_action_per_hour_cap
        if cap == _UNLIMITED_CAP:
            return True
        if cap <= 0:
            return False
        self._trim_minted_window()
        return len(self._minted_at) < cap

    def _record_minted(self, goal: Goal) -> None:
        # We track when the goal was *recorded* (i.e. accepted into the
        # rate window), not ``goal.generated_at``, so test fakes that
        # freeze time stay accurate.
        self._minted_at.append(self._now())

    # ---- generators -----------------------------------------------------

    async def maintenance(self) -> list[Goal]:
        """System-health goals.

        Examples: clean stale temp / compact old journal / audit
        ``daemon.log`` error rate. Requires ``can_modify_files``;
        returns ``[]`` for policies below level 50 or that lack write
        permission.
        """
        if self._policy.level < 50:
            return []
        if not self._policy.can_modify_files:
            return []
        return [
            self._mint(
                name="maintenance.clean_stale_temp",
                description="Sweep ~/.xmclaw/v2/tmp for entries older than 7 days.",
                priority=3,
                category=_CATEGORY_MAINTENANCE,
                completion_criteria={"max_age_days": 7},
            ),
            self._mint(
                name="maintenance.compact_journal",
                description="Compact memory journal segments older than 14 days.",
                priority=4,
                category=_CATEGORY_MAINTENANCE,
                completion_criteria={"max_age_days": 14},
            ),
            self._mint(
                name="maintenance.audit_daemon_log",
                description="Audit daemon.log error rate over the trailing 24h.",
                priority=5,
                category=_CATEGORY_MAINTENANCE,
                completion_criteria={"window_hours": 24},
            ),
        ]

    async def exploration(self) -> list[Goal]:
        """Learning goals.

        Examples: try a freshly-installed tool / probe a skill's
        boundary / re-read recently-modified user code. Requires
        ``can_run_long_processes``; returns ``[]`` otherwise.
        """
        if not self._policy.can_run_long_processes:
            return []
        return [
            self._mint(
                name="exploration.try_new_tool",
                description="Run a smoke test against a recently-registered tool provider.",
                priority=3,
                category=_CATEGORY_EXPLORATION,
            ),
            self._mint(
                name="exploration.probe_skill_boundary",
                description="Pick the lowest-confidence promoted skill and stress its inputs.",
                priority=4,
                category=_CATEGORY_EXPLORATION,
            ),
            self._mint(
                name="exploration.reread_recent_user_code",
                description="Re-read files the user touched in the last 4h to refresh context.",
                priority=2,
                category=_CATEGORY_EXPLORATION,
                completion_criteria={"window_hours": 4},
            ),
        ]

    async def social(self) -> list[Goal]:
        """Relationship goals.

        Examples: user away N days → check in / deadline approaching →
        remind. Requires ``can_send_messages`` (only category that
        contacts humans); returns ``[]`` otherwise.
        """
        if not self._policy.can_send_messages:
            return []
        return [
            self._mint(
                name="social.checkin_after_silence",
                description="If user hasn't chatted in 3+ days, send a low-pressure check-in.",
                priority=4,
                category=_CATEGORY_SOCIAL,
                completion_criteria={"silence_days_min": 3},
            ),
            self._mint(
                name="social.deadline_reminder",
                description="Remind user of upcoming deadlines within the next 48h.",
                priority=6,
                category=_CATEGORY_SOCIAL,
                completion_criteria={"window_hours": 48},
            ),
        ]

    async def generate_all(self) -> list[Goal]:
        """Run all three generators, dedupe vs. ``current_goals``, and
        respect ``autonomous_action_per_hour_cap`` globally.

        Goals are accepted in priority order (higher first), ties
        broken by emission order, so when the cap bites we keep the
        most useful candidates.
        """
        # 1. drain rolling window before deciding anything
        self._trim_minted_window()

        candidates: list[Goal] = []
        candidates.extend(await self.maintenance())
        candidates.extend(await self.exploration())
        candidates.extend(await self.social())

        existing = self._existing_goals()

        # Stable priority-desc sort so the cap keeps the most important.
        candidates.sort(key=lambda g: (-g.priority, g.generated_at))

        accepted: list[Goal] = []
        cap = self._policy.autonomous_action_per_hour_cap
        already_minted = len(self._minted_at)

        for goal in candidates:
            if self._is_duplicate(goal, existing):
                continue
            if cap == 0:
                # Cap == 0 means "no autonomous actions". Even though
                # the per-class methods already guard, generate_all is
                # the contract surface; fail closed here too.
                break
            if cap != _UNLIMITED_CAP and (already_minted + len(accepted)) >= cap:
                break
            accepted.append(goal)
            self._record_minted(goal)

        return accepted
