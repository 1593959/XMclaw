"""HookContext + HookResult — the data shapes that flow through hooks.

``HookContext`` is built by the daemon at every lifecycle point and
passed to every matching hook. It carries:

  * Identity (event, session_id, agent_id, hop) — what fired and where.
  * Payload — event-specific data (the user message for
    UserPromptSubmit, the tool call args for PreToolUse, etc.).
  * Mutable handles (NOT mutated by hooks themselves — hooks return
    a HookResult and the engine reconciles).

``HookResult`` is what a hook returns. JSON-protocol parity with
Claude Code:

  * ``continue: bool`` — allow the chain / lifecycle to proceed
  * ``decision: "allow" | "deny" | "ask"`` — for gate events
    (PreToolUse, UserPromptSubmit). ``deny`` blocks; ``ask`` surfaces
    to the user via approval_service; ``allow`` continues.
  * ``system_message: str`` — extra system instruction injected into
    the next LLM call (PreLLM only)
  * ``updated_input: Any`` — rewrite the event payload (e.g. user
    message text after a redactor hook)
  * ``output: str`` — free-form text logged + emitted on the bus
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xmclaw.core.hooks.events import HookEvent


Decision = Literal["allow", "deny", "ask"]


@dataclass(frozen=True, slots=True)
class HookContext:
    """Per-fire context handed to a hook. Immutable; hooks read from
    it, return a HookResult to influence behaviour.
    """

    event: HookEvent
    session_id: str
    agent_id: str
    # Event-specific payload (see docs/HOOKS.md for the shape per
    # event). Always a JSON-serialisable dict so command runners can
    # pipe it to stdin as JSON.
    payload: dict[str, Any] = field(default_factory=dict)
    # Workspace root for path-aware hooks. None when no workspace
    # wired (echo-mode tests).
    workspace_root: str | None = None
    # Trust level of the workspace. ``"trusted"`` = full access,
    # ``"untrusted"`` = command/function hooks refuse to run.
    workspace_trust: Literal["trusted", "untrusted"] = "trusted"
    # Wall-clock at the firing moment, for hook-side timing.
    ts: float = 0.0
    # Hop number within the current turn (0 for the first LLM call).
    # Only meaningful for PreLLM / PostLLM / PreToolUse / etc.;
    # session / turn events leave it at -1.
    hop: int = -1


@dataclass(frozen=True, slots=True)
class HookResult:
    """Return value from a hook. All fields optional; defaults are
    the no-op "continue cleanly without changing anything" shape.
    """

    # Engine-level: should the lifecycle continue? When False, the
    # turn / tool call / session is aborted with ``reason``.
    continue_: bool = True
    # Gate events only: allow / deny / ask. None = don't vote.
    decision: Decision | None = None
    # Append-to-system-prompt for PreLLM; ignored elsewhere.
    system_message: str = ""
    # Rewrite the event payload. For UserPromptSubmit this is the
    # new user text; for PreToolUse it's the new args dict.
    updated_input: Any = None
    # Free-form output, surfaced on the event bus + (when set) the
    # chat UI as a system note.
    output: str = ""
    # Reason for stop/deny, shown to the operator + the model.
    reason: str = ""
    # Hook id that produced this result. Filled by the engine —
    # callers don't set it.
    hook_id: str = ""

    @staticmethod
    def deny(reason: str) -> "HookResult":
        return HookResult(continue_=False, decision="deny", reason=reason)

    @staticmethod
    def ask(reason: str = "") -> "HookResult":
        return HookResult(decision="ask", reason=reason)

    @staticmethod
    def allow() -> "HookResult":
        return HookResult(decision="allow")

    @staticmethod
    def system_note(text: str) -> "HookResult":
        return HookResult(system_message=text)


def merge_decisions(results: list[HookResult]) -> Decision | None:
    """Reduce a list of hook results to a single permission decision.

    Priority (matches Claude Code): ``deny`` > ``ask`` > ``allow``.
    Hooks that didn't vote (``decision is None``) are ignored. Returns
    None when nothing voted — the caller's default applies.
    """
    has_deny = any(r.decision == "deny" for r in results)
    if has_deny:
        return "deny"
    has_ask = any(r.decision == "ask" for r in results)
    if has_ask:
        return "ask"
    has_allow = any(r.decision == "allow" for r in results)
    if has_allow:
        return "allow"
    return None


__all__ = [
    "HookContext",
    "HookResult",
    "Decision",
    "merge_decisions",
]
