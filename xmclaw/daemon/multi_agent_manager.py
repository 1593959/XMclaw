"""MultiAgentManager: the registry of running :class:`Workspace` s.

Epic #17 Phase 2. Wraps ``dict[str, Workspace]`` with the three
properties Phase 3 needs before it can rewire ``app.state.agent`` to
``app.state.agents``:

* **Concurrency-safe mutation** — an :class:`asyncio.Lock` serializes
  create / remove, and a ``pending_starts`` map deduplicates two
  simultaneous ``create(same_id, …)`` calls into one build. That
  matters because the Web UI's "launch agent" button and the CLI's
  ``xmclaw agent start`` might both fire at once; building the same
  AgentLoop twice would leak a second provider client and double the
  ``memory.db`` handle count.
* **Crash-safe persistence** — each create writes the resolved config
  to ``~/.xmclaw/v2/agents/<id>.json``; ``load_from_disk`` rehydrates
  on daemon start. Lives in ``v2/`` (daemon runtime state) rather
  than ``~/.xmclaw/workspaces/`` (Epic #18 Web-UI presets) — see
  :func:`xmclaw.utils.paths.agents_registry_dir` for the rationale.
* **Inert until used** — Phase 2 is additive: the FastAPI app still
  hands one AgentLoop out of ``app.state.agent``. Phase 3 will
  instantiate this manager, wire it into ``app.state.agents``, and
  add the ``X-Agent-Id`` routing middleware.

No direct routes are mounted here. Phase 3 adds the router.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.workspace import Workspace, build_workspace
from xmclaw.utils.paths import agents_registry_dir

log = logging.getLogger(__name__)

_ALLOWED_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _sanitize_id(raw: str) -> str:
    """Reduce an agent_id to filename-safe ASCII.

    Mirrors Epic #18's ``routers/workspaces._sanitize_id`` so the two
    namespaces stay collision-symmetric: any ID that names a valid
    preset in the UI also names a valid agent here.
    """
    cleaned = "".join(c if c in _ALLOWED_ID_CHARS else "_" for c in raw)
    return cleaned or "default"


class AgentIdError(ValueError):
    """Raised when a create/remove call supplies a malformed ID.

    Separate from the file system layer: the caller gave us an empty
    string or whitespace, which would collapse to ``default`` and
    silently clobber another agent. Fail loud instead.
    """


class MultiAgentManager:
    """Concurrency-safe registry of running :class:`Workspace` objects.

    Ownership: this class owns the ``dict[str, Workspace]`` and the
    ``<id>.json`` files on disk. Callers get read-only views via
    :meth:`get` / :meth:`list_ids`; mutations go through the async
    create/remove entry points so the lock + pending-starts dedup
    path is always honored.
    """

    def __init__(
        self,
        bus: InProcessEventBus,
        *,
        registry_dir: Path | None = None,
        max_hops: int = 20,
        primary_config: dict[str, Any] | None = None,
    ) -> None:
        self._bus = bus
        self._dir = registry_dir if registry_dir is not None else agents_registry_dir()
        self._max_hops = max_hops
        # B-134: stored so sub-agents whose config omits ``llm`` can
        # inherit the primary's provider/model/api_key. Lets the persona
        # template UI ship a one-line system_prompt without forcing the
        # user to re-type the LLM section.
        self._primary_config = primary_config
        self._agents: dict[str, Workspace] = {}
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Task[Workspace]] = {}

    def set_primary_config(self, primary_config: dict[str, Any] | None) -> None:
        """Update the inherited primary config after construction.

        The daemon builds the manager BEFORE it knows the resolved
        primary config (the order is: bus → manager → factory). This
        setter lets ``app.py`` hand over the config once it's parsed
        without forcing the manager into a two-phase init.
        """
        self._primary_config = primary_config

    # ── read-only views ────────────────────────────────────────────────

    def get(self, agent_id: str) -> Workspace | None:
        """Return the live workspace, or ``None`` if not registered."""
        return self._agents.get(agent_id)

    def list_ids(self) -> list[str]:
        """Sorted list of every registered agent_id.

        Sorted for deterministic output — Phase 3's ``list_agents``
        tool shouldn't flicker the order between turns.
        """
        return sorted(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        return isinstance(agent_id, str) and agent_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    # ── mutation ──────────────────────────────────────────────────────

    async def create(self, agent_id: str, config: dict[str, Any]) -> Workspace:
        """Build (or return the already-being-built) workspace for ``agent_id``.

        Dedup semantics: two concurrent ``create("a", cfg)`` calls
        yield the *same* Workspace — the second caller awaits the
        first call's task. That's what prevents "launch" button +
        CLI race from building two AgentLoops.

        An existing registered ID short-circuits to the live instance;
        callers who need to replace must ``remove()`` first. This
        mirrors ``systemd-run --unit=foo``: re-running with a live
        unit is an error, not a silent restart.
        """
        stripped = agent_id.strip()
        if not stripped:
            raise AgentIdError("agent_id must be a non-empty string")
        if stripped != _sanitize_id(stripped):
            # Rejecting rather than silently rewriting: the file on
            # disk uses the sanitized form, but the in-memory key
            # would use the raw form, and the two would drift.
            raise AgentIdError(
                f"agent_id {stripped!r} contains unsafe characters; "
                "allowed: [A-Za-z0-9_-]"
            )

        # Fast path — already running.
        existing = self._agents.get(stripped)
        if existing is not None:
            return existing

        # Dedup concurrent builds without holding the lock across the
        # potentially-slow build_workspace call.
        async with self._lock:
            existing = self._agents.get(stripped)
            if existing is not None:
                return existing
            pending = self._pending.get(stripped)
            if pending is None:
                pending = asyncio.create_task(
                    self._do_create(stripped, config),
                    name=f"multi-agent-create-{stripped}",
                )
                self._pending[stripped] = pending

        try:
            return await pending
        finally:
            # The first create to finish pops the entry. Subsequent
            # awaiters never see it because the task is cached under
            # the same key until this block unwinds.
            async with self._lock:
                self._pending.pop(stripped, None)

    async def _do_create(self, agent_id: str, config: dict[str, Any]) -> Workspace:
        """The actual build. Runs inside a ``pending_starts`` task."""
        ws = build_workspace(
            agent_id, config, self._bus, max_hops=self._max_hops,
            primary_config=self._primary_config,  # B-134
        )
        # Persist before registering: if the disk write fails we don't
        # want a running-but-unpersisted agent that would vanish on
        # daemon restart.
        self._write_config(agent_id, ws.config)
        # Background work (Phase 7 evolution observers) starts before
        # the workspace enters the public dict — a caller that races a
        # create+list should never see a workspace that's visible but
        # not yet subscribed to the bus.
        await ws.start()
        async with self._lock:
            self._agents[agent_id] = ws
        log.info("multi_agent.registered", extra={"agent_id": agent_id})
        return ws

    async def remove(self, agent_id: str) -> bool:
        """Remove the agent from the registry and delete its manifest.

        Returns True when something was removed, False when the ID
        was already absent. Idempotent on disk (missing file = ok).
        Evolution observers (Phase 7) have their bus subscription
        cancelled via :meth:`Workspace.stop`; LLM workspaces are inert
        between turns and need no teardown beyond the dict pop.
        """
        stripped = agent_id.strip()
        if not stripped:
            return False
        async with self._lock:
            ws = self._agents.pop(stripped, None)
        if ws is None and not self._config_path(stripped).exists():
            return False
        if ws is not None:
            try:
                await ws.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                log.warning(
                    "multi_agent.stop_failed",
                    extra={"agent_id": stripped, "error": str(exc)},
                )
        self._delete_config(stripped)
        log.info("multi_agent.removed", extra={"agent_id": stripped})
        return True

    async def load_from_disk(self) -> list[str]:
        """Rehydrate every ``*.json`` under the registry dir.

        Files with no ``llm`` section produce a workspace where
        :attr:`Workspace.agent_loop` is None — that's fine for
        Phase 2: the manager still tracks them so the UI can show
        "not ready" instead of vanishing the preset. Phase 3's
        router translates that into a 409 on turn requests.

        Malformed JSON files are skipped with a warning, not fatal:
        a hand-edited bad file shouldn't brick the daemon.
        """
        loaded: list[str] = []
        if not self._dir.exists():
            return loaded
        for cfg_path in sorted(self._dir.glob("*.json")):
            agent_id = cfg_path.stem
            try:
                raw = cfg_path.read_text(encoding="utf-8")
                config = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning(
                    "multi_agent.load_skip",
                    extra={"agent_id": agent_id, "error": str(exc)},
                )
                continue
            if not isinstance(config, dict):
                log.warning(
                    "multi_agent.load_skip",
                    extra={"agent_id": agent_id, "error": "top-level not object"},
                )
                continue
            try:
                ws = build_workspace(
                    agent_id, config, self._bus, max_hops=self._max_hops
                )
            except Exception as exc:  # noqa: BLE001 — one bad config shouldn't kill boot
                log.warning(
                    "multi_agent.load_skip",
                    extra={"agent_id": agent_id, "error": str(exc)},
                )
                continue
            try:
                await ws.start()
            except Exception as exc:  # noqa: BLE001 — one bad start shouldn't kill boot
                log.warning(
                    "multi_agent.load_skip",
                    extra={"agent_id": agent_id, "error": str(exc)},
                )
                continue
            async with self._lock:
                self._agents[agent_id] = ws
            loaded.append(agent_id)
        log.info("multi_agent.loaded", extra={"count": len(loaded)})
        return loaded

    # ── filesystem helpers ────────────────────────────────────────────

    def _config_path(self, agent_id: str) -> Path:
        return self._dir / f"{agent_id}.json"

    def _write_config(self, agent_id: str, config: dict[str, Any]) -> None:
        """Atomic write — tmp file + rename.

        Rename-into-place keeps an ``xmclaw stop`` in the middle of a
        create from leaving a half-written JSON behind that ``load_from_disk``
        would then skip on reboot. The extra ``dir=`` hint keeps the
        tmp file on the same volume so ``Path.replace`` is atomic on
        Windows (cross-volume moves would fall back to copy+unlink).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._config_path(agent_id)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{agent_id}.", suffix=".json.tmp", dir=str(self._dir)
        )
        tmp_path = Path(tmp_name)
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            tmp_path.replace(target)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _delete_config(self, agent_id: str) -> None:
        self._config_path(agent_id).unlink(missing_ok=True)
