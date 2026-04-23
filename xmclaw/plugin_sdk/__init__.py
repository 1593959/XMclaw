"""Plugin SDK — the frozen public surface for third-party plugins (Epic #2).

A third-party plugin (distributed as its own pip package, discovered via
``importlib.metadata.entry_points``) must import only from this module.
Reaching into ``xmclaw.core``, ``xmclaw.providers``, or any other internal
subpackage is a layering violation and will be flagged by
:file:`scripts/check_plugin_isolation.py`.

What's here is a deliberately small re-export of:

* The ABCs plugins must subclass (``Skill``, ``ToolProvider``,
  ``LLMProvider``, ``MemoryProvider``, ``ChannelAdapter``, ``SkillRuntime``).
* The IR dataclasses they exchange with the runtime
  (``ToolCall``, ``ToolResult``, ``ToolSpec``, ``MemoryItem``, etc.).
* The event-bus primitives they read (``EventType``, ``BehavioralEvent``).
  Writing to the bus is reserved for the daemon — plugins raise / return
  to signal; the daemon decides what to publish.

Compatibility contract:

* Adding new names to ``__all__`` is a minor-version change.
* Removing a name, renaming a name, or narrowing a signature is a
  **major-version** change and must ship with a migration note in
  ``CHANGELOG.md``.
* Changing a type's field set:
    - Adding an optional field with a default  -> minor.
    - Adding a required field / removing field -> major.

See :file:`xmclaw/plugin_sdk/AGENTS.md` for the full contract (what you
can and can't import, how discovery works, how to run isolation checks
locally).
"""
from __future__ import annotations

# ── Event bus (read-only for plugins) ────────────────────────────────────
from xmclaw.core.bus.events import BehavioralEvent, EventType

# ── Internal Tool-Call IR ────────────────────────────────────────────────
from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec

# ── Provider ABCs ────────────────────────────────────────────────────────
from xmclaw.providers.channel.base import (
    ChannelAdapter,
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.memory.base import MemoryItem, MemoryProvider
from xmclaw.providers.runtime.base import SkillRuntime
from xmclaw.providers.tool.base import ToolProvider

# ── Skill ABC ────────────────────────────────────────────────────────────
from xmclaw.skills.base import Skill, SkillInput, SkillOutput

__all__ = [
    # bus
    "BehavioralEvent",
    "EventType",
    # IR
    "ToolCall",
    "ToolCallShape",
    "ToolResult",
    "ToolSpec",
    # channel
    "ChannelAdapter",
    "ChannelTarget",
    "InboundMessage",
    "OutboundMessage",
    # llm
    "LLMChunk",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "Pricing",
    # memory
    "MemoryItem",
    "MemoryProvider",
    # runtime
    "SkillRuntime",
    # tool
    "ToolProvider",
    # skill
    "Skill",
    "SkillInput",
    "SkillOutput",
]

# Stability guard — changes to this tuple require a changelog entry.
# The test suite reads this and asserts it matches ``__all__``, so an
# accidental addition can't sneak in.
FROZEN_SURFACE: tuple[str, ...] = tuple(sorted(__all__))
