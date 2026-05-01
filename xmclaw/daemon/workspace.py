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
from xmclaw.daemon.evolution_agent import EvolutionAgent
from xmclaw.daemon.factory import build_agent_from_config

# Recognized values of ``config["kind"]``. The default "llm" preserves
# pre-Phase-7 behavior (build_agent_from_config → AgentLoop). Phase 7
# adds "evolution" — a headless observer workspace, see
# :class:`EvolutionAgent`.
KIND_LLM = "llm"
KIND_EVOLUTION = "evolution"
_KNOWN_KINDS = {KIND_LLM, KIND_EVOLUTION}


@dataclass
class Workspace:
    """A running agent + the config it was built from.

    Fields:
      * ``agent_id`` — the stable ID clients send in ``X-Agent-Id``
        (Phase 3). Must match ``config["agent_id"]`` post-build.
      * ``config`` — the resolved config dict the loop was built from.
        Kept around so Phase 2's persistence layer can round-trip it
        to disk without a second load_config pass.
      * ``kind`` — ``"llm"`` (default, serves WS turns via
        :attr:`agent_loop`) or ``"evolution"`` (headless observer, see
        :attr:`observer`). Phase 7 added the discriminator so the UI
        and the ``list_agents`` tool can tell the two apart without
        poking at the loop attribute.
      * ``agent_loop`` — ``None`` when ``kind != "llm"`` or the config
        had no LLM. Mirrors :func:`build_agent_from_config`'s contract
        — a workspace with no LLM is a valid preset shape (user hasn't
        picked a provider yet), it just can't serve turns.
      * ``observer`` — ``None`` unless ``kind == "evolution"``. Phase 7
        evolution workspaces carry the :class:`EvolutionAgent` here;
        the manager drives its lifecycle via :meth:`start` / :meth:`stop`.

    Not frozen: Phase 2 needs to attach per-workspace resources
    (memory manager, skill registry) via post-hoc setters when those
    epics land. Freezing now would force a redesign then.
    """

    agent_id: str
    config: dict[str, Any] = field(default_factory=dict)
    agent_loop: AgentLoop | None = None
    kind: str = KIND_LLM
    observer: EvolutionAgent | None = None

    def is_ready(self) -> bool:
        """True when the workspace is usable for its kind.

        LLM workspaces need an :class:`AgentLoop`; evolution observers
        only need an :class:`EvolutionAgent` instance (its subscription
        is started by the manager, not the dataclass itself).
        """
        if self.kind == KIND_EVOLUTION:
            return self.observer is not None
        return self.agent_loop is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a preset-compatible dict.

        Drops runtime-only handles (``agent_loop``, ``observer``) and
        returns the config payload plus ``agent_id``. Shape matches
        what Epic #18's Web-UI workspace POST writes — so Phase 2's
        persistence layer can share the same ``~/.xmclaw/workspaces/``
        directory.
        """
        return {"agent_id": self.agent_id, **self.config}

    async def start(self) -> None:
        """Spin up the workspace's background work, if any.

        LLM workspaces are inert until a turn arrives; evolution
        workspaces need their bus subscription attached. Called by the
        manager after a successful build / rehydrate.
        """
        if self.observer is not None:
            await self.observer.start()

    async def stop(self) -> None:
        """Tear down the workspace's background work, if any."""
        if self.observer is not None:
            await self.observer.stop()


def build_workspace(
    agent_id: str,
    config: dict[str, Any],
    bus: InProcessEventBus,
    *,
    max_hops: int = 20,
    primary_config: dict[str, Any] | None = None,
) -> Workspace:
    """Assemble a :class:`Workspace` from a preset config.

    Dispatches on ``config["kind"]``:

    * ``"llm"`` (default) — builds an :class:`AgentLoop` via
      :func:`build_agent_from_config`. ``agent_id`` is injected so
      event stamps line up with the manager's key.
    * ``"evolution"`` — builds an :class:`EvolutionAgent` that will
      subscribe to the bus on :meth:`Workspace.start`. The observer
      never needs an LLM, so the config is free of provider keys.

    Unknown kinds raise ``ValueError`` so a typo in a preset file
    fails loud at rehydrate time rather than silently producing a
    ``Workspace`` that serves neither turns nor observations.

    Returns a workspace even when the LLM config is empty (for kind
    "llm") — the caller decides whether to surface that as "not ready
    yet" to the client.

    B-134: ``primary_config`` (the daemon's config used for the main
    agent) lets sub-agents OMIT their own ``llm`` block and inherit
    the primary's LLM provider/model/api_key. Templates that only
    customise the system prompt no longer need to repeat the LLM
    section. The sub-agent's own ``llm`` block, when present, wins.
    """
    kind = str(config.get("kind", KIND_LLM))
    if kind not in _KNOWN_KINDS:
        raise ValueError(
            f"unknown workspace kind {kind!r}; expected one of {sorted(_KNOWN_KINDS)}"
        )
    merged = {**config, "agent_id": agent_id, "kind": kind}
    # B-134: inherit primary's llm section when sub-agent omits it.
    # We only fall back when ``llm`` is wholly absent — an explicit
    # empty ``{}`` in the sub-agent config still wins (user signalled
    # "no LLM, this agent is meant to be inert").
    if (
        kind == KIND_LLM
        and "llm" not in merged
        and isinstance(primary_config, dict)
        and isinstance(primary_config.get("llm"), dict)
    ):
        merged["llm"] = primary_config["llm"]
    if kind == KIND_EVOLUTION:
        # B-117: thresholds读自 config evolution.promotion_thresholds.*。
        # 之前是 dataclass 默认值硬编码 — 改要改源码 + 重启。现在
        # 也热重载（B-109 watcher 改 config dict 但 controller 自己
        # 不持续读，所以严格说热生效要等下一次 EvolutionAgent 重建。
        # 大多数用户改 thresholds 是配 + 重启 daemon，所以接受。)
        from xmclaw.core.evolution.controller import PromotionThresholds
        ev_section = (config.get("evolution") or {}) if isinstance(config, dict) else {}
        thresh_section = ev_section.get("promotion_thresholds") or {}
        thresholds = PromotionThresholds(
            min_plays=int(thresh_section.get("min_plays", 10)),
            min_mean=float(thresh_section.get("min_mean", 0.65)),
            min_gap_over_head=float(thresh_section.get("min_gap_over_head", 0.05)),
            min_gap_over_second=float(thresh_section.get("min_gap_over_second", 0.03)),
        )
        observer = EvolutionAgent(agent_id, bus, thresholds=thresholds)
        return Workspace(
            agent_id=agent_id, config=merged, kind=kind, observer=observer,
        )
    loop = build_agent_from_config(merged, bus, max_hops=max_hops)
    return Workspace(
        agent_id=agent_id, config=merged, agent_loop=loop, kind=kind,
    )
