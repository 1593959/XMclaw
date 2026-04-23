"""Workspace: a bundle of state owned by a single agent.

Epic #17 Phase 1. Lays the foundation for multi-agent — one workspace
per agent means memory, skill registry, history, and (future) channel
adapters all live on the workspace rather than in module globals.

Phase 1 scope is intentionally narrow: just the dataclass and a factory
that wraps :func:`xmclaw.daemon.factory.build_agent_from_config`. The
existing single-agent daemon is NOT rewired yet — ``app.state.agent``
still holds one AgentLoop. Phase 2 adds a manager / registry; Phase 3
does the app-level rewire. This split keeps each PR reviewable.

Why a dataclass instead of a class? Phase 2 needs to serialize
workspaces to ``~/.xmclaw/workspaces/<id>.json`` (the same directory
Epic #18's Web UI writes presets to — one concept, two roles: stored
preset vs running instance). Dataclass + explicit ``to_dict`` /
``from_dict`` gives us a stable serialization boundary without dragging
in pydantic.

Why not put this in ``factory.py``? Factory is ~650 lines already and
does DI for a single AgentLoop. A workspace is a bundle of state, not
a construction recipe — orthogonal concerns belong in separate
modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.factory import build_agent_from_config


@dataclass
class Workspace:
    """A running agent + the config it was built from.

    Fields:
      * ``agent_id`` — the stable ID clients send in ``X-Agent-Id``
        (Phase 3). Must match ``config["agent_id"]`` post-build.
      * ``config`` — the resolved config dict the loop was built from.
        Kept around so Phase 2's persistence layer can round-trip it
        to disk without a second load_config pass.
      * ``agent_loop`` — ``None`` when the config had no LLM set.
        Mirrors :func:`build_agent_from_config`'s contract — a
        workspace with no LLM is a valid preset shape (user hasn't
        picked a provider yet), it just can't serve turns.

    Not frozen: Phase 2 needs to attach per-workspace resources
    (memory manager, skill registry) via post-hoc setters when those
    epics land. Freezing now would force a redesign then.
    """

    agent_id: str
    config: dict[str, Any] = field(default_factory=dict)
    agent_loop: AgentLoop | None = None

    def is_ready(self) -> bool:
        """True when the workspace can serve turns (has an AgentLoop)."""
        return self.agent_loop is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a preset-compatible dict.

        Drops ``agent_loop`` (runtime-only) and returns just the config
        payload plus the agent_id. Shape matches what Epic #18's
        Web-UI workspace POST writes — so Phase 2's persistence layer
        can share the same ``~/.xmclaw/workspaces/`` directory.
        """
        return {"agent_id": self.agent_id, **self.config}


def build_workspace(
    agent_id: str,
    config: dict[str, Any],
    bus: InProcessEventBus,
    *,
    max_hops: int = 20,
) -> Workspace:
    """Assemble a :class:`Workspace` from a preset config.

    Injects ``agent_id`` into the config before calling
    :func:`build_agent_from_config`, so the resulting AgentLoop stamps
    the right ID onto every event it emits. An explicit ``agent_id``
    on the caller side (rather than "read it out of the dict") makes
    the contract at the manager boundary unambiguous: the manager
    owns the ID, the config is just payload.

    Returns a workspace even when the config has no LLM — the caller
    decides whether to surface that as "not ready yet" to the client.
    """
    merged = {**config, "agent_id": agent_id}
    loop = build_agent_from_config(merged, bus, max_hops=max_hops)
    return Workspace(agent_id=agent_id, config=merged, agent_loop=loop)
