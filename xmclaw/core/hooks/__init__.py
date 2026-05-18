"""Public API for the hook engine. See engine.py for the wiring story."""
from xmclaw.core.hooks.context import (
    Decision,
    HookContext,
    HookResult,
    merge_decisions,
)
from xmclaw.core.hooks.engine import (
    DispatchOutcome,
    HookEngine,
    build_hook_engine_from_config,
)
from xmclaw.core.hooks.events import ALL_EVENTS, HookEvent, parse_event
from xmclaw.core.hooks.runners import (
    AgentRunner,
    CommandRunner,
    FunctionRunner,
    HookSpec,
    HttpRunner,
    PromptRunner,
)
from xmclaw.core.hooks.trust import (
    TrustLevel,
    mark_workspace_trusted,
    unmark_workspace_trusted,
    workspace_trust_level,
)

__all__ = [
    "ALL_EVENTS",
    "AgentRunner",
    "CommandRunner",
    "Decision",
    "DispatchOutcome",
    "FunctionRunner",
    "HookContext",
    "HookEngine",
    "HookEvent",
    "HookResult",
    "HookSpec",
    "HttpRunner",
    "PromptRunner",
    "TrustLevel",
    "build_hook_engine_from_config",
    "mark_workspace_trusted",
    "merge_decisions",
    "parse_event",
    "unmark_workspace_trusted",
    "workspace_trust_level",
]
