"""FastAPI lifespan context manager for XMclaw daemon.

Extracted from app.py to keep the factory under control.
"""
from __future__ import annotations

import asyncio
import time as time_module
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI

from xmclaw.core.bus import (
    BehavioralEvent,
    EventType,
    InProcessEventBus,
)
from xmclaw.utils.log import get_logger

log = get_logger(__name__)

def _find_skill_provider(root: Any) -> tuple[Any, Any]:
    """Walk a tool-provider tree, return ``(skill_tool_provider, registry)``.

    B-298: the factory wraps tools as
    ``CompositeToolProvider(CompositeToolProvider(SkillTool, ...),
    MemoryBridge)``, so the SkillToolProvider can sit two levels
    deep. Pre-B-298 single-level lookup against the wrong attribute
    name (``_providers``) silently returned ``(None, None)``,
    leaving:

    * the EvolutionAgent without a registry → B-296's per-skill
      HEAD inference (``registry.active_version(skill_id)``)
      degraded to ``head_version=None`` for every skill;
    * the VariantSelector's ``enabled`` branch never taken →
      candidates never got explore-traffic → controller's
      ``min_plays`` threshold never cleared → B-294's evaluate()
      trigger fired but always proposed nothing.

    Returns the first node in a depth-first traversal whose
    ``_registry`` attribute looks like a SkillRegistry (has the
    ``list_skill_ids`` method). ``(None, None)`` when no such
    provider exists in the tree (e.g. echo-mode daemon, or an
    agent assembled with only BuiltinTools).

    The walker tries public attribute names first
    (``children``) then private ones (``_children``) so a
    custom ToolProvider that exposes a public children API
    can override the walk without subclassing
    CompositeToolProvider.
    """
    stack = [root] if root is not None else []
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        reg = getattr(node, "_registry", None)
        if reg is not None and hasattr(reg, "list_skill_ids"):
            return node, reg
        kids = (
            getattr(node, "children", None)
            or getattr(node, "_children", None)
            or getattr(node, "_providers", None)
            or []
        )
        for k in kids:
            stack.append(k)
    return None, None


def make_lifespan(
    *,
    bus: InProcessEventBus | None,
    memory: Any | None,
    sweep_task: Any | None,
    backup_scheduler: Any | None,
    events_retention_task: Any | None,
    config: dict[str, Any] | None,
    agent: Any | None,
    orchestrator: Any | None,
    agents_manager: Any | None,
    shared_cognitive_state: Any | None,
    cognition_cfg: dict[str, Any] | None,
    memory_build_error: str | None,
    config_path: Path | None,
) -> Callable[[FastAPI], AsyncIterator[None]]:
    """Build the lifespan context manager with all dependencies wired."""
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 2026-05-11: track lifespan startup duration so future
        # regressions like the lark_oapi 3.75s import are visible
        # at boot. Logged right before yield + stamped on app.state
        # for /api/v2/status to surface.
        _lifespan_t0 = time_module.perf_counter()
        # Sprint 2 Wave 6: stamp wall-clock boot ts for the dashboard's
        # uptime widget. perf_counter is monotonic + unhelpful for "when
        # did the daemon start" — time.time() is what we want here.
        _app.state.boot_ts = time_module.time()
        # cron_tick is now a local variable in the closure
        if sweep_task is not None:
            await sweep_task.start()
        if backup_scheduler is not None:
            await backup_scheduler.start()
        if events_retention_task is not None:
            try:
                await events_retention_task.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("events_retention.start_failed err=%s", exc)

        # Cron tick: only start once the primary agent is live; without
        # it run_turn would have nowhere to land. Wraps a per-tick
        # session_id ('cron:<job_id>:<ts>') so cron output is searchable
        # via the Sessions page later.
        try:
            # Use the module-level singleton so the REST router and the
            # tick task see the same jobs. Constructing a fresh
            # CronStore() here would mean the tick loop never observes
            # POST-created jobs (each instance owns its own _jobs cache).
            from xmclaw.core.scheduler.cron import (
                CronTickTask,
                default_cron_store,
            )
            store = default_cron_store()

            async def _runner(job):
                target_agent = _app.state.agent
                if target_agent is None or not job.wake_agent:
                    return f"# {job.name} fired @ {time_module.strftime('%Y-%m-%d %H:%M:%S')}\n\n(no agent wired)\n"
                sid = f"cron:{job.id}:{int(time_module.time())}"
                # B-332: enforce ``CronJob.enabled_toolsets``. Pre-B-332
                # this field was parsed + persisted + shown in the UI
                # but no path filtered the agent's tools — a job
                # claiming "only web_fetch" still got the full stack.
                # ``[]`` (the default) means "no restriction"; non-
                # empty list becomes the per-call allowlist passed
                # to AgentLoop.run_turn, which wraps the agent's
                # tool provider in FilteredToolProvider for the
                # duration of THIS turn (no shared mutable state, so
                # concurrent user chat is unaffected).
                tools_allowlist = (
                    set(job.enabled_toolsets) if job.enabled_toolsets else None
                )

                # Jarvisification: when TaskScheduler is wired, submit
                # cron jobs through it so they participate in the
                # priority queue, dependency graph, and retry logic.
                _task_sched = getattr(_app.state, "task_scheduler", None)
                if _task_sched is not None:
                    from xmclaw.cognition.task_scheduler import Task
                    _task = Task(
                        id=f"cron:{job.id}:{int(time_module.time())}",
                        prompt=job.prompt,
                        priority=3,
                    )
                    await _task_sched.submit(_task)
                    return f"# {job.name} queued @ {time_module.strftime('%Y-%m-%d %H:%M:%S')}\n\n(task_scheduler)\n"

                try:
                    res = await target_agent.run_turn(
                        sid, job.prompt,
                        tools_allowlist=tools_allowlist,
                    )
                    return (
                        f"# {job.name} @ {time_module.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                        f"## Result\n\n{res.text or '(no text)'}\n\n"
                        f"## Tool calls\n\n{len(res.tool_calls)} call(s); ok={res.ok}\n"
                    )
                except Exception as exc:  # noqa: BLE001
                    return (
                        f"# {job.name} @ {time_module.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                        f"## Error\n\n{type(exc).__name__}: {exc}\n"
                    )

            cron_tick = CronTickTask(store=store, runner=_runner, tick_interval_s=60.0)
            await cron_tick.start()
        except Exception as exc:  # noqa: BLE001 — cron failures must
            # not block boot; the API still answers, jobs just won't fire
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning("cron.tick_start_failed", exc_info=exc)
            cron_tick = None

        # B-41: built-in vector index. Watches MEMORY.md / USER.md /
        # memory/*.md and pumps chunks into SqliteVecMemory so the
        # ``memory_search`` tool gets real semantic results, not just
        # keyword fallback. Quietly disabled when no embedding provider
        # is configured — fresh installs run without forcing the user
        # to set an embedding key just to boot.
        memory_indexer = None
        # B-87: record the precise reason indexer didn't start so the UI
        # can stop guessing. Three failure modes the user actually hits:
        #   * embedder None — config evolution.memory.embedding missing
        #     or build_embedding_provider returned None (api_key empty
        #     for a remote endpoint, etc).
        #   * vec_provider None — memory.enabled=false in config, or
        #     factory failed to construct SqliteVecMemory.
        #   * start() raised — Ollama unreachable, dim mismatch with
        #     existing memory_vec table, sqlite-vec extension load fail.
        _app.state.indexer_start_error = None
        try:
            from xmclaw.providers.memory.embedding import build_embedding_provider
            from xmclaw.daemon.memory_indexer import MemoryFileIndexer
            from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory
            embedder = build_embedding_provider(config or {})
            mgr = getattr(_app.state, "memory", None)
            vec_provider = None
            # B-88: ``_app.state.memory`` is the raw return value of
            # ``build_memory_from_config`` — i.e. a bare SqliteVecMemory
            # (or None), NOT a MemoryManager. The MemoryManager lives
            # inside ``agent._memory_manager`` and is constructed later
            # in the lifespan, after this indexer block runs. Earlier
            # code did ``for p in mgr.providers`` here, but
            # SqliteVecMemory has no ``.providers`` attribute, so the
            # loop iterated zero times and vec_provider stayed None
            # → indexer refused to start with the bogus message
            # "sqlite_vec 未挂载". Handle both shapes (direct +
            # manager-wrapped) so a future refactor that swaps in a
            # MemoryManager keeps working.
            if isinstance(mgr, SqliteVecMemory):
                vec_provider = mgr
            elif mgr is not None:
                for p in getattr(mgr, "providers", []):
                    if isinstance(p, SqliteVecMemory):
                        vec_provider = p
                        break
            if embedder is None:
                _app.state.indexer_start_error = (
                    "embedder 未构造（evolution.memory.embedding 节缺失或不可用 — "
                    "检查 api_key / base_url / model）"
                )
            elif vec_provider is None:
                # B-395: surface the captured memory_build_error with
                # cause-specific guidance. Pre-B-395 the bare-except at
                # build time threw the error string away and the banner
                # fell back to a generic "memory.enabled=false 或构造失败"
                # message — wrong when memory.enabled IS true and
                # actively misleading users to delete memory.db (which
                # makes a "database is locked" failure WORSE because the
                # next daemon start has to recreate the schema while
                # another process still holds the WAL).
                if memory_build_error:
                    err_lower = memory_build_error.lower()
                    if "database is locked" in err_lower:
                        hint = (
                            "根因：memory.db 在 daemon 启动那一刻被另一进程锁住了。"
                            "常见来源：(a) 上次 daemon 没干净退出留了 zombie；"
                            "(b) Windows 杀软 / Defender 在扫文件；(c) 旧 daemon "
                            "还活着（``xmclaw stop`` 没生效）。\n"
                            "修法：``xmclaw stop`` → 任务管理器确认 python.exe 真没了 → "
                            "等 5s → ``xmclaw start``。**不要**删 memory.db — "
                            "锁是进程 hold 住的，不是文件本身的问题。"
                        )
                    elif "sqlite_vec" in err_lower or "no module named" in err_lower:
                        hint = (
                            "根因：sqlite-vec Python 包没装。"
                            "修法：``pip install sqlite-vec`` 后重启 daemon。"
                        )
                    elif "enable_load_extension" in err_lower:
                        hint = (
                            "根因：当前 Python 的 sqlite3 编译时未启用 "
                            "load_extension（macOS / 部分 Linux 发行版常见）。"
                            "修法：换一个支持 extension load 的 Python build "
                            "（pyenv install 时加 PYTHON_CONFIGURE_OPTS=\"--enable-loadable-sqlite-extensions\"）。"
                        )
                    else:
                        hint = (
                            "根因不在已识别清单里。看下面 ``原始 error`` "
                            "的具体类型，或翻 ~/.xmclaw/v2/logs/xmclaw.log 找 "
                            "更早的 SqliteVecMemory traceback。"
                        )
                    _app.state.indexer_start_error = (
                        f"SqliteVecMemory 构造失败: {memory_build_error}\n{hint}"
                    )
                else:
                    cfg_enabled = (
                        ((config or {}).get("memory") or {}).get("enabled")
                    )
                    _app.state.indexer_start_error = (
                        f"sqlite_vec 未挂载（memory.enabled={cfg_enabled!r}）。"
                        "config.json 中 memory.enabled 设为 true 才会构造 vec store。"
                    )
            # B-197: stash on app.state so post-sampling extractors
            # (ProfileExtractor / ExtractLessonsHook / ProposalMaterializer)
            # can dual-write facts to the vec store. Both may be None
            # when the indexer didn't start — extractor side handles
            # that gracefully.
            _app.state.embedder = embedder
            _app.state.vec_provider = vec_provider

            # B-198 Phase 3 step 2: construct PersonaStore + migrate
            # existing markdown into DB on first boot. The store
            # becomes the single source of truth for persona content
            # in subsequent steps; for now it's wired but not yet
            # consulted by the assembler (legacy markdown reads still
            # serve the system prompt). Migration is idempotent — re-
            # running after rows exist is a no-op.
            _app.state.persona_store = None
            if vec_provider is not None:
                try:
                    from xmclaw.core.persona.store import PersonaStore
                    from xmclaw.daemon.factory import (
                        _resolve_persona_profile_dir,
                    )
                    from xmclaw.providers.memory.base import MemoryItem

                    _persona_pdir = _resolve_persona_profile_dir(config or {})
                    _persona_pdir.mkdir(parents=True, exist_ok=True)
                    _persona_store = PersonaStore(
                        vec_provider, _persona_pdir,
                        item_factory=MemoryItem,
                        # B-211: pass the embedder so set_manual writes
                        # are vectorised — pre-B-211 audit showed 0%
                        # embedding coverage on persona_manual rows.
                        embedder=embedder,
                    )
                    report = await _persona_store.migrate_from_disk()
                    _app.state.persona_store = _persona_store
                    log.info(
                        "persona_store.migrated profile=%s files=%s",
                        _persona_pdir.name, dict(report),
                    )
                    # Hand the store to the primary AgentLoop so
                    # post-sampling hooks can render-to-disk after
                    # fact upserts. AgentLoop was constructed earlier
                    # in the lifespan (factory build); we attach via
                    # attribute since the constructor signature was
                    # locked before this Phase 3 work landed.
                    _agent_obj = getattr(_app.state, "agent", None)
                    if _agent_obj is not None:
                        _agent_obj._persona_store = _persona_store
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "persona_store.bootstrap_failed err=%s — "
                        "falling back to legacy markdown reads", exc,
                    )

            if embedder is not None and vec_provider is not None:
                # Resolve persona dir lazily — same path the agent's
                # remember tool writes to.
                from xmclaw.daemon.factory import _resolve_persona_profile_dir
                _cfg = config or {}

                def _pdir():
                    return _resolve_persona_profile_dir(_cfg)

                _idx_section = (
                    ((_cfg.get("evolution") or {}).get("memory") or {})
                    .get("indexer") or {}
                )
                # B-210: optional workspace code paths to also index.
                # Lives under ``evolution.memory.workspace_paths`` (list
                # of dir strings). Empty / missing → indexer behaves as
                # before (persona/journal only). Each path is filtered
                # by the indexer's denylist + extension allowlist so
                # ``node_modules`` / ``.git`` / build outputs don't end
                # up in the vector store.
                _ws_paths_raw = (
                    ((_cfg.get("evolution") or {}).get("memory") or {})
                    .get("workspace_paths") or []
                )
                _workspace_paths = [
                    str(p) for p in _ws_paths_raw if isinstance(p, str)
                ]
                memory_indexer = MemoryFileIndexer(
                    persona_dir_provider=_pdir,
                    sqlite_vec=vec_provider,
                    embedder=embedder,
                    poll_interval_s=float(_idx_section.get("poll_interval_s", 10.0)),
                    bus=bus,
                    workspace_paths=_workspace_paths,
                )
                await memory_indexer.start()
                _app.state.memory_indexer = memory_indexer
            else:
                _app.state.memory_indexer = None
        except Exception as exc:  # noqa: BLE001 — indexer failures
            # must not block daemon boot. Without it, memory_search
            # falls back to keyword scan over MEMORY.md — degraded
            # but not broken.
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "memory_indexer.start_failed err=%s", exc,
            )
            _app.state.memory_indexer = None
            _app.state.indexer_start_error = (
                f"indexer 启动抛异常：{type(exc).__name__}: {exc}"
            )

        # B-109: hot-reload config.json on external edits. Polls
        # mtime every 5s; mutates the in-memory cfg dict in place
        # and publishes CONFIG_RELOADED so subscribers can react.
        # Some sections (llm/memory/gateway/runtime/mcp_servers/
        # integrations) need a daemon restart to fully take effect —
        # the event payload flags that.
        _app.state.config_watcher = None
        try:
            if config is not None and config_path is not None:
                from xmclaw.daemon.config_watcher import ConfigFileWatcher
                cw = ConfigFileWatcher(
                    config_path=Path(config_path), cfg=config, bus=bus,
                )
                await cw.start()
                _app.state.config_watcher = cw

                # B-314: live-apply runtime-only config slices on
                # CONFIG_RELOADED. Pre-B-314 the watcher published the
                # event but no subscriber existed for the runtime
                # sections (tools.allowed_dirs, security.guardians.*,
                # logging.level), so users had to restart the daemon
                # for ANY config change. Now: tools/security/logging
                # take effect within ~5s of the file save.
                async def _on_config_reloaded(ev: Any) -> None:
                    payload = getattr(ev, "payload", {}) or {}
                    # B-350 (Sprint 1): stash the latest reload summary
                    # on app.state so /api/v2/setup can surface it. The
                    # UI shows a "config changed — restart" banner when
                    # ``restart_required: true``. Without this the user
                    # writes a new key, sees no UI feedback, and is left
                    # guessing whether the daemon picked it up.
                    _app.state.last_config_reload = dict(payload)
                    top_changed = set(payload.get("top_changed") or [])
                    # logging level — immediate
                    if "logging" in top_changed:
                        try:
                            from xmclaw.utils.log import set_log_level
                            level_str = (
                                (config.get("logging") or {}).get("level")
                            )
                            set_log_level(level_str)
                            log.info("config_reloaded.logging applied")
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "config_reloaded.logging_failed err=%s", exc,
                            )
                    # tools.allowed_dirs — push to BuiltinTools if
                    # present in the agent's tool stack
                    if "tools" in top_changed and agent is not None:
                        try:
                            new_dirs = (
                                (config.get("tools") or {}).get("allowed_dirs")
                            )
                            if new_dirs is not None:
                                from xmclaw.providers.tool.builtin import (
                                    BuiltinTools,
                                )
                                # Walk composite tree to find BuiltinTools.
                                def _walk(p):
                                    yield p
                                    kids = (
                                        getattr(p, "children", None)
                                        or getattr(p, "_children", None)
                                        or []
                                    )
                                    for k in kids:
                                        yield from _walk(k)
                                from pathlib import Path as _Path
                                for node in _walk(getattr(agent, "_tools", None)):
                                    if isinstance(node, BuiltinTools):
                                        try:
                                            # B-340 (audit pass-2 #1):
                                            # BuiltinTools stores the
                                            # sandbox in ``self._allowed``
                                            # (resolved Path list, NOT
                                            # raw strings). Pre-B-340
                                            # this wrote
                                            # ``_allowed_dirs`` — a brand-
                                            # new attribute nobody read,
                                            # so the "applied" log line
                                            # was fiction and the sandbox
                                            # was unchanged. Match the
                                            # ctor's resolve-and-list
                                            # contract.
                                            node._allowed = (
                                                [_Path(d).resolve() for d in new_dirs]
                                                if new_dirs else None
                                            )
                                            log.info(
                                                "config_reloaded.tools.allowed_dirs "
                                                "applied count=%d",
                                                len(new_dirs),
                                            )
                                        except Exception:  # noqa: BLE001
                                            pass
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "config_reloaded.tools_failed err=%s", exc,
                            )
                    # security.guardians.policy — push to GuardedToolProvider
                    if "security" in top_changed:
                        try:
                            from xmclaw.providers.tool.guarded import (
                                GuardedToolProvider,
                            )
                            new_policy = (
                                ((config.get("security") or {})
                                 .get("guardians") or {}).get("policy")
                            )
                            if new_policy is not None and agent is not None:
                                def _walk(p):
                                    yield p
                                    kids = (
                                        getattr(p, "children", None)
                                        or getattr(p, "_children", None)
                                        or []
                                    )
                                    for k in kids:
                                        yield from _walk(k)
                                from xmclaw.security.tool_guard.models import (
                                    GuardianPolicy as _GuardianPolicy,
                                )
                                for node in _walk(getattr(agent, "_tools", None)):
                                    if isinstance(node, GuardedToolProvider):
                                        try:
                                            # B-340 (audit pass-2 #2):
                                            # GuardedToolProvider stores
                                            # the policy as
                                            # ``self._policy`` (a
                                            # GuardianPolicy instance,
                                            # consulted on every invoke
                                            # via .action_for(...)).
                                            # Pre-B-340 this wrote
                                            # ``_policy_dict`` — a
                                            # brand-new attribute nobody
                                            # read; the "applied" log
                                            # line was fiction.
                                            node._policy = (
                                                _GuardianPolicy.from_config(new_policy)
                                            )
                                            log.info(
                                                "config_reloaded.security."
                                                "guardians applied",
                                            )
                                        except Exception as exc:  # noqa: BLE001
                                            log.warning(
                                                "config_reloaded.security."
                                                "policy_apply_failed err=%s",
                                                exc,
                                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "config_reloaded.security_failed err=%s", exc,
                            )
                    # Phase E: cognition.continuous_loop — live-apply
                    # CognitiveDaemonConfig without restart.
                    if "cognition" in top_changed:
                        try:
                            daemon = getattr(
                                _app.state, "cognitive_daemon", None,
                            )
                            if daemon is not None:
                                from xmclaw.cognition.cognitive_daemon import (
                                    CognitiveDaemonConfig,
                                )

                                _cog = (config or {}).get("cognition") or {}
                                _cl = _cog.get("continuous_loop") or {}
                                new_cfg = CognitiveDaemonConfig(
                                    enabled=bool(_cl.get("enabled", True)),
                                    autonomy_level=int(
                                        _cl.get("autonomy_level", 50)
                                    ),
                                    heartbeat_hz=float(
                                        _cl.get("heartbeat_hz", 1.0)
                                    ),
                                    action_threshold=float(
                                        _cl.get("action_threshold", 0.6)
                                    ),
                                    top_k_focus=int(
                                        _cl.get("top_k_focus", 7)
                                    ),
                                    goal_gen_every_n_ticks=int(
                                        _cl.get(
                                            "goal_gen_every_n_ticks", 60
                                        )
                                    ),
                                    self_experiment_every_n_ticks=int(
                                        _cl.get(
                                            "self_experiment_every_n_ticks",
                                            600,
                                        )
                                    ),
                                    skill_propose_every_n_ticks=int(
                                        _cl.get(
                                            "skill_propose_every_n_ticks",
                                            300,
                                        )
                                    ),
                                    max_pending_goals=int(
                                        _cl.get("max_pending_goals", 16)
                                    ),
                                    slow_subsystem_threshold_ms=float(
                                        _cl.get(
                                            "slow_subsystem_threshold_ms",
                                            500.0,
                                        )
                                    ),
                                )
                                daemon.update_config(new_cfg)
                                log.info(
                                    "config_reloaded.cognition applied "
                                    "autonomy=%d hz=%.2f",
                                    new_cfg.autonomy_level,
                                    new_cfg.heartbeat_hz,
                                )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "config_reloaded.cognition_failed err=%s",
                                exc,
                            )

                bus.subscribe(
                    lambda e: e.type == EventType.CONFIG_RELOADED,
                    _on_config_reloaded,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "config_watcher.start_failed err=%s", exc,
            )
            _app.state.config_watcher = None

        # B-51: Auto-Dream cron — daily LLM-driven MEMORY.md
        # compaction. Off when no LLM is configured (we can't rewrite
        # without one). Gated by ``evolution.dream.enabled`` (default
        # true) so users can opt out.
        _app.state.dream_cron = None
        try:
            agent = _app.state.agent if hasattr(_app.state, "agent") else None
            llm = getattr(agent, "_llm", None) if agent is not None else None
            dream_section = (
                ((config or {}).get("evolution") or {}).get("dream") or {}
            )
            dream_enabled = dream_section.get("enabled", True)
            if llm is not None and dream_enabled:
                from xmclaw.daemon.dream_compactor import (
                    DreamCompactor, DreamCron,
                )
                from xmclaw.daemon.factory import _resolve_persona_profile_dir
                _cfg = config or {}

                def _pdir():
                    return _resolve_persona_profile_dir(_cfg)

                compactor = DreamCompactor(
                    llm=llm,
                    persona_dir_provider=_pdir,
                    bus=bus,
                    daily_log_window_days=int(
                        dream_section.get("daily_log_window_days", 7)
                    ),
                    min_keep_ratio=float(
                        dream_section.get("min_keep_ratio", 0.3)
                    ),
                )
                cron = DreamCron(
                    compactor=compactor,
                    hour=int(dream_section.get("hour", 3)),
                    minute=int(dream_section.get("minute", 0)),
                )
                await cron.start()
                _app.state.dream_compactor = compactor
                _app.state.dream_cron = cron
            else:
                _app.state.dream_compactor = None
        except Exception as exc:  # noqa: BLE001
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "dream_cron.start_failed err=%s", exc,
            )
            _app.state.dream_compactor = None
            _app.state.dream_cron = None

        try:
            await agents_manager.load_from_disk()
        except Exception:  # noqa: BLE001 — bad preset file must not block boot
            pass
        # Epic #4 Phase C: start the EvolutionOrchestrator so auto_apply
        # subscriptions go live. No-op when orchestrator is None or
        # auto_apply is False (it still publishes events on explicit
        # promote/rollback, just doesn't consume proposals). Failures
        # here must not prevent the daemon from serving WS traffic —
        # evolution is a best-effort observability layer, not a
        # critical path.
        if orchestrator is not None:
            try:
                await orchestrator.start()
            except Exception:  # noqa: BLE001
                pass

        # Epic #24 Phase 1: default-start a single EvolutionAgent
        # observer subscribed to GRADER_VERDICT (no longer requires
        # an explicit ``POST /workspaces`` with ``kind="evolution"``).
        # The observer aggregates per (skill_id, version) and writes
        # PROMOTE / ROLLBACK proposals through SKILL_CANDIDATE_PROPOSED
        # events; the orchestrator above (auto_apply=False by default)
        # publishes them but does NOT mutate HEAD until a human / CLI
        # approves. This wires the closed loop without giving the
        # agent unsupervised authority over its own version pointer.
        # Failures must not block boot.
        _app.state.evolution_observer = None
        _app.state.evolution_evaluation_trigger = None

        # B-298: walk the agent's tool stack once and reuse the result
        # for both the EvolutionAgent registry injection (B-296) and
        # the VariantSelector wiring (B-295) below. See
        # ``_find_skill_provider`` for the fix history.
        _stp_ref, _evo_registry = _find_skill_provider(
            getattr(agent, "_tools", None),
        )

        try:
            from xmclaw.daemon.evolution_agent import EvolutionAgent
            evo_agent = EvolutionAgent("evo-main", bus, registry=_evo_registry)
            await evo_agent.start()
            _app.state.evolution_observer = evo_agent
        except Exception as exc:  # noqa: BLE001
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "evolution_observer.start_failed err=%s", exc,
            )
            _app.state.evolution_observer = None
            evo_agent = None  # type: ignore[assignment]

        # B-294: wire the evaluation trigger. Phase 3.1 left ``evaluate()``
        # implemented but UNCALLED in production — verdicts accumulated
        # in EWMA forever, never turning into proposals. Without this
        # block the "self-evolving agent" loop is dead from observer
        # onwards. Fires evaluate() ~30s after a verdict-burst settles,
        # capped at 1 fire / 5min, with min 10 new verdicts to skip
        # tiny bursts.
        if evo_agent is not None:
            try:
                from xmclaw.daemon.evolution_evaluation_trigger import (
                    EvolutionEvaluationTrigger,
                )
                _eval_cfg = (
                    (config or {}).get("evolution", {}).get("evaluation", {})
                )
                eval_trigger = EvolutionEvaluationTrigger(
                    evo_agent, bus,
                    debounce_s=float(_eval_cfg.get("debounce_s", 30.0)),
                    cooldown_s=float(_eval_cfg.get("cooldown_s", 300.0)),
                    min_new_verdicts=int(
                        _eval_cfg.get("min_new_verdicts", 10),
                    ),
                    enabled=bool(_eval_cfg.get("enabled", True)),
                )
                await eval_trigger.start()
                _app.state.evolution_evaluation_trigger = eval_trigger
            except Exception as exc:  # noqa: BLE001
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "evolution_evaluation_trigger.start_failed err=%s", exc,
                )
                _app.state.evolution_evaluation_trigger = None

        # B-295: VariantSelector wires UCB1 over (skill_id, version)
        # arms into the SkillToolProvider's invoke path. Without this,
        # candidate skill versions never get traffic → never get plays
        # → controller's ``min_plays`` threshold never clears →
        # B-294's evaluate() trigger fires but always finds the same
        # winner (HEAD) and proposes nothing. With this wired, new
        # variants get explore-traffic via UCB1, accumulate plays,
        # and the loop closes.
        # OPT-IN: ``config.evolution.variant_selector.enabled`` (default
        # True). Operators who want pure HEAD-only behaviour can flip
        # to False; the SkillToolProvider falls back transparently.
        _app.state.variant_selector = None
        try:
            _vs_cfg = (config or {}).get("evolution", {}).get(
                "variant_selector", {},
            )
            # B-298: reuse the (_stp_ref, _evo_registry) the recursive
            # walk found above. If the agent has no SkillToolProvider
            # in its tool stack, both are None and we silently skip —
            # there's nothing to multiplex.
            if (
                _vs_cfg.get("enabled", True)
                and _evo_registry is not None
                and _stp_ref is not None
            ):
                from xmclaw.skills.variant_selector import VariantSelector
                selector = VariantSelector(
                    registry=_evo_registry,
                    exploration_c=float(_vs_cfg.get("exploration_c", 2.0)),
                    head_warmup_plays=int(_vs_cfg.get("head_warmup_plays", 5)),
                )
                await selector.start(bus)
                # Inject into the SkillToolProvider so invoke() consults it.
                _stp_ref._variant_selector = selector
                _app.state.variant_selector = selector
        except Exception as exc:  # noqa: BLE001
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "variant_selector.start_failed err=%s", exc,
            )
            _app.state.variant_selector = None

        # Epic #24 Phase 2.3: default-start JournalWriter + Profile
        # Extractor. JournalWriter buffers session events and writes
        # one mechanical-metadata row per ``SESSION_LIFECYCLE
        # phase=destroy`` to ``~/.xmclaw/v2/journal/<YYYY-MM>/``.
        # ProfileExtractor buffers user/assistant turns and (on every
        # Nth turn or session destroy) calls a pluggable extractor
        # callable to derive ProfileDelta lines, which it appends to
        # the active persona's ``USER.md``. Phase 2.3 wires the
        # *harness* — both default to no-op extractors. Phase 2.4
        # plugs in a real LLM-driven extractor.
        _app.state.journal_writer = None
        try:
            from xmclaw.core.journal import JournalWriter
            jw = JournalWriter(bus)
            await jw.start()
            _app.state.journal_writer = jw
        except Exception as exc:  # noqa: BLE001
            log.warning("journal.writer_start_failed err=%s", exc)
            _app.state.journal_writer = None

        _app.state.profile_extractor = None
        try:
            from xmclaw.core.profile import ProfileExtractor, noop_extractor
            from xmclaw.daemon.factory import _resolve_persona_profile_dir
            _cfg = config or {}

            def _user_md_path():
                return _resolve_persona_profile_dir(_cfg) / "USER.md"

            # Phase 3.5: pull a real LLM-backed extractor if an agent
            # (and therefore an LLM provider) is wired. Falls back to
            # the bench-friendly no-op when there's no LLM (echo-only
            # mode, fresh install before LLM key is set).
            extractor = noop_extractor
            try:
                if agent is not None and getattr(agent, "_llm", None) is not None:
                    from xmclaw.daemon.llm_extractors import (
                        build_profile_extractor,
                    )
                    extractor = build_profile_extractor(agent._llm)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "profile.llm_extractor_build_failed err=%s — "
                    "falling back to noop", exc,
                )

            # B-197: build a fact_writer callback that translates
            # (text, metadata) → MemoryItem + embed + put. Kept as a
            # callback so xmclaw/core/ stays free of providers/
            # imports per the layering rule.
            #
            # B-198 Phase 3: after writing the row, route through
            # PersonaStore.render_to_disk(USER.md) so the on-disk
            # file becomes a fresh render of the DB. Disk is now a
            # cache; DB is truth. (We default to USER.md because
            # ProfileExtractor exclusively produces kind=preference
            # which lives in USER.md per AUTO_SECTIONS.)
            _vec = getattr(_app.state, "vec_provider", None)
            _embed = getattr(_app.state, "embedder", None)
            _store = getattr(_app.state, "persona_store", None)
            _fact_writer = None
            if _vec is not None:
                async def _fact_writer_impl(  # type: ignore[no-redef]
                    text: str, metadata: dict,  # noqa: ANN001
                ) -> None:
                    # B-197 Phase 2: route through upsert_fact so
                    # repeated facts strengthen an existing row
                    # (evidence_count++) instead of stacking new ones.
                    # The auto-promote rule (working → long after
                    # evidence_count >= 3 + confidence >= 0.7) lives
                    # inside upsert_fact / _strengthen.
                    emb = None
                    if _embed is not None:
                        try:
                            vecs = await _embed.embed([text])
                            if vecs and vecs[0]:
                                emb = list(vecs[0])
                        except Exception:  # noqa: BLE001
                            emb = None
                    layer_name = str(metadata.get("layer") or "working")
                    upsert = getattr(_vec, "upsert_fact", None)
                    wrote_ok = False
                    if upsert is not None:
                        try:
                            await upsert(
                                text=text,
                                embedding=emb,
                                layer=layer_name,
                                metadata=metadata,
                            )
                            wrote_ok = True
                        except Exception:  # noqa: BLE001
                            wrote_ok = False
                    if not wrote_ok:
                        # Fallback: legacy put for providers without
                        # upsert (mostly tests / non-default backends).
                        import uuid as _uuid
                        import time as _t
                        from xmclaw.providers.memory.base import MemoryItem
                        item = MemoryItem(
                            id=_uuid.uuid4().hex,
                            layer=layer_name,
                            text=text,
                            metadata=metadata,
                            embedding=tuple(emb) if emb else None,
                            ts=_t.time(),
                        )
                        await _vec.put(layer_name, item)
                    # B-198 Phase 3: re-render USER.md from DB so the
                    # on-disk file (still read by the persona
                    # assembler each turn) reflects the new row. Best
                    # effort — render failure logs but doesn't poison
                    # the write.
                    if _store is not None:
                        try:
                            await _store.render_to_disk("USER.md")
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "persona_store.render_after_write_failed "
                                "file=USER.md err=%s", exc,
                            )
                _fact_writer = _fact_writer_impl

            pe = ProfileExtractor(
                bus, _user_md_path,
                extractor_callable=extractor,
                fact_writer=_fact_writer,
            )
            await pe.start()
            _app.state.profile_extractor = pe
        except Exception as exc:  # noqa: BLE001
            log.warning("profile.extractor_start_failed err=%s", exc)
            _app.state.profile_extractor = None

        # Epic #24 Phase 2.4: invalidate the system-prompt cache when
        # USER.md gets new auto-extracted preferences. Without this,
        # the persona assembler keeps serving the cached snapshot
        # taken at session start and the new lines never reach the
        # agent until the next persona write or daemon restart.
        try:
            from xmclaw.daemon.prompt_builder import (
                bump_prompt_freeze_generation,
            )

            async def _on_profile_updated(event: BehavioralEvent) -> None:
                try:
                    bump_prompt_freeze_generation()
                except Exception:  # noqa: BLE001
                    pass

            bus.subscribe(
                lambda e: e.type == EventType.USER_PROFILE_UPDATED,
                _on_profile_updated,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "profile.prompt_cache_invalidation_wire_failed err=%s",
                exc,
            )

        # Epic #24 Phase 3.2: SkillDreamCycle — periodic SkillProposer
        # task that walks recent journal history and emits
        # SKILL_CANDIDATE_PROPOSED events for tool-use patterns the
        # agent keeps repeating. Default extractor is no-op until
        # Phase 3.3 layers an LLM-backed one inside the factory; the
        # cycle runs the (cheap) pattern detection regardless so the
        # wiring is exercised in production from day one.
        # Configurable via ``evolution.skill_dream.{enabled,interval_s}``.
        # B-164 layers ``RealtimeEvolutionTrigger`` on top so each
        # turn pokes the same proposer ~15s after settling. Configurable
        # via ``evolution.realtime.{enabled,debounce_s,cooldown_s}``.
        _app.state.skill_dream = None
        _app.state.realtime_evolution = None
        try:
            from xmclaw.core.evolution import SkillProposer, noop_extractor
            from xmclaw.core.journal import JournalReader
            from xmclaw.daemon.skill_dream import (
                RealtimeEvolutionTrigger,
                SkillDreamCycle,
            )

            sd_cfg = (
                ((config or {}).get("evolution") or {}).get("skill_dream")
                or {}
            )
            sd_enabled = bool(sd_cfg.get("enabled", True))
            sd_interval = float(sd_cfg.get("interval_s", 1800.0))
            if sd_enabled:
                # Phase 3.5: real LLM-backed extractor when LLM is wired.
                sk_extractor = noop_extractor
                try:
                    if agent is not None and getattr(agent, "_llm", None) is not None:
                        from xmclaw.daemon.llm_extractors import (
                            build_skill_extractor,
                        )
                        sk_extractor = build_skill_extractor(agent._llm)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "skill_dream.llm_extractor_build_failed err=%s — "
                        "falling back to noop", exc,
                    )
                proposer = SkillProposer(
                    JournalReader(), extractor_callable=sk_extractor,
                )
                dream = SkillDreamCycle(
                    proposer, bus,
                    interval_s=sd_interval, enabled=True,
                )
                await dream.start()
                _app.state.skill_dream = dream

                # B-164: realtime trigger. Default ON so the user
                # feels evolution after every conversation; opt-out
                # via ``evolution.realtime.enabled = false``.
                rt_cfg = (
                    ((config or {}).get("evolution") or {}).get("realtime")
                    or {}
                )
                rt_enabled = bool(rt_cfg.get("enabled", True))
                rt_debounce = float(rt_cfg.get("debounce_s", 15.0))
                rt_cooldown = float(rt_cfg.get("cooldown_s", 60.0))
                if rt_enabled:
                    realtime = RealtimeEvolutionTrigger(
                        dream, bus,
                        debounce_s=rt_debounce,
                        cooldown_s=rt_cooldown,
                        enabled=True,
                    )
                    await realtime.start()
                    _app.state.realtime_evolution = realtime
        except Exception as exc:  # noqa: BLE001
            log.warning("skill_dream.start_failed err=%s", exc)
            _app.state.skill_dream = None
            _app.state.realtime_evolution = None

        # Sprint 3 #3: Letta-pattern sleep-time agent + OS idle scheduler.
        # See docs/SLEEP_AGENT.md and docs/EVOLUTION_HONEST_STATE.md
        # ("Iron Rules"). The SleepWorker polls the OS idle interface
        # every 30s; when ``idle_short_s`` (default 5min) crosses, it
        # fires registered "light" tasks (memory sweep / dedup); when
        # ``idle_long_s`` (default 30min) crosses, it fires "heavy"
        # tasks (skill dream / mutation evaluation). Cron-based triggers
        # still work — idle-aware firing layers on top so heavy work
        # never has to wait for the cron when the user has stopped
        # working. Configurable via ``evolution.scheduler.idle_aware``
        # (default True). When false, behaviour is identical to today
        # (cron only).
        _app.state.sleep_worker = None
        try:
            from xmclaw.daemon.sleep_worker import (
                SleepWorker,
                build_idle_detector,
                make_dream_cycle_task,
                make_memory_sweep_task,
                parse_sleep_config,
            )
            sched_cfg = (
                ((config or {}).get("evolution") or {}).get("scheduler")
                or {}
            )
            sleep_cfg = parse_sleep_config(sched_cfg)
            if sleep_cfg.idle_aware:
                detector = build_idle_detector()
                sleep_worker = SleepWorker(
                    detector, bus,
                    idle_short_s=sleep_cfg.idle_short_s,
                    idle_long_s=sleep_cfg.idle_long_s,
                    poll_interval_s=sleep_cfg.poll_interval_s,
                )
                # Register the existing periodic tasks as idle-aware
                # triggers — they keep their cron loop AND get an extra
                # idle-edge firing. Migration pattern from
                # ``docs/SLEEP_AGENT.md`` §Migration.
                _sd = getattr(_app.state, "skill_dream", None)
                if _sd is not None:
                    sleep_worker.register_task(
                        "skill_dream_cycle", "long",
                        make_dream_cycle_task(_sd),
                    )
                if sweep_task is not None:
                    sleep_worker.register_task(
                        "memory_sweep", "short",
                        make_memory_sweep_task(sweep_task),
                    )
                await sleep_worker.start()
                _app.state.sleep_worker = sleep_worker
        except Exception as exc:  # noqa: BLE001 — sleep worker failure
            # must not block daemon boot; cron triggers still fire.
            log.warning("sleep_worker.start_failed err=%s", exc)
            _app.state.sleep_worker = None

        # B-173: SkillsWatcher — periodic user_loader.load_all() so
        # `npx skills add` / `git clone <url> ~/.xmclaw/skills_user/<name>` /
        # manual SKILL.md drops appear without a daemon restart. ~10s
        # tick is cheap (UserSkillsLoader is idempotent on already-
        # registered (id, version) pairs).
        # Configurable via ``evolution.skills_watcher.{enabled,interval_s}``.
        _app.state.skills_watcher = None
        if orchestrator is not None:
            try:
                from xmclaw.daemon.skills_watcher import SkillsWatcher
                from xmclaw.skills.user_loader import resolve_skill_roots
                sw_cfg = (
                    ((config or {}).get("evolution") or {})
                    .get("skills_watcher") or {}
                )
                sw_enabled = bool(sw_cfg.get("enabled", True))
                sw_interval = float(sw_cfg.get("interval_s", 10.0))
                if sw_enabled:
                    canonical, extras = resolve_skill_roots(config)
                    watcher = SkillsWatcher(
                        orchestrator.registry, canonical,
                        extra_roots=extras,
                        interval_s=sw_interval,
                        enabled=True,
                        # B-333: bus needed so the watcher can emit
                        # SKILL_UPDATE_REQUIRES_RESTART when it spots
                        # a skill.py mtime change (importlib-cached,
                        # restart needed to pick up).
                        bus=bus,
                    )
                    await watcher.start()
                    _app.state.skills_watcher = watcher
            except Exception as exc:  # noqa: BLE001
                log.warning("skills_watcher.start_failed err=%s", exc)
                _app.state.skills_watcher = None

        # B-172: MutationOrchestrator — runs the SkillMutator
        # (DSPy/GEPA via xmclaw.core.evolution.mutator) when
        # GRADER_VERDICT EWMA shows a skill is underperforming. Writes
        # the candidate to ``<id>/versions/v<N>.md`` (so daemon
        # restart preserves it via user_loader B-172 extension), then
        # emits SKILL_CANDIDATE_PROPOSED with decision="promote" for
        # the existing EvolutionOrchestrator to handle. ``set_head=False``
        # on register means a worse-than-baseline mutation is audited
        # but never live without explicit promote.
        # Configurable via ``evolution.mutation.{enabled, threshold,
        # min_samples, cooldown_s, score_delta, ewma_alpha}``.
        _app.state.mutation_orchestrator = None
        if orchestrator is not None:
            try:
                from xmclaw.daemon.mutation_orchestrator import (
                    MutationOrchestrator,
                )
                m_cfg = (
                    ((config or {}).get("evolution") or {}).get("mutation")
                    or {}
                )
                if bool(m_cfg.get("enabled", True)):
                    # Sprint 3 #5: wire ReflectiveMutator as fallback
                    # when DSPy is not installed. Pure-Python, no heavy
                    # deps — closes the "mutation engine is wired but
                    # inert" gap identified in the evolution audit.
                    fallback = None
                    if agent is not None and config is not None:
                        llm = getattr(agent, "_llm", None)
                        if llm is not None:
                            from xmclaw.daemon.factory import (
                                build_reflective_mutator_from_config,
                            )
                            fallback = build_reflective_mutator_from_config(
                                config, llm=llm,
                            )
                    mut_orch = MutationOrchestrator(
                        orchestrator.registry, bus,
                        fallback_mutator=fallback,
                        ewma_alpha=float(m_cfg.get("ewma_alpha", 0.2)),
                        threshold=float(m_cfg.get("threshold", 0.5)),
                        min_samples=int(m_cfg.get("min_samples", 5)),
                        cooldown_s=float(m_cfg.get("cooldown_s", 3600.0)),
                        score_delta=float(m_cfg.get("score_delta", 0.05)),
                        enabled=True,
                    )
                    await mut_orch.start()
                    _app.state.mutation_orchestrator = mut_orch
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "mutation_orchestrator.start_failed err=%s", exc,
                )
                _app.state.mutation_orchestrator = None

        # B-167: ProposalMaterializer — closes the missing link between
        # SkillProposer drafts (decision="propose") and SkillRegistry.
        # Pre-B-167 a draft sat in proposals.jsonl + events.db and the
        # "approve" button forwarded to /promote, which 404'd because
        # winner_version=0 was never registered. Now: every proposal
        # writes ~/.xmclaw/skills_user/<id>/SKILL.md + register +
        # set_head — the agent uses it on the next turn, no manual
        # gate. Disable via ``evolution.materialize.enabled = false``
        # if the user prefers the old (broken) approve-first flow.
        _app.state.proposal_materializer = None
        if orchestrator is not None:
            try:
                from xmclaw.daemon.proposal_materializer import (
                    ProposalMaterializer,
                )
                pm_cfg = (
                    ((config or {}).get("evolution") or {}).get("materialize")
                    or {}
                )
                pm_enabled = bool(pm_cfg.get("enabled", True))
                if pm_enabled:
                    # B-197: hand vec store + embedder so each
                    # newly-materialized skill lands as a DB row
                    # (kind=procedure) for memory_search retrieval.
                    materializer = ProposalMaterializer(
                        orchestrator.registry, bus, enabled=True,
                        memory_provider=getattr(
                            _app.state, "vec_provider", None,
                        ),
                        embedder=getattr(_app.state, "embedder", None),
                    )
                    await materializer.start()
                    _app.state.proposal_materializer = materializer
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "proposal_materializer.start_failed err=%s", exc,
                )
                _app.state.proposal_materializer = None

        # 2026-05-12: ReflectionMaterializer — closes the reflection
        # loop. Pre-this, INNER_MONOLOGUE + METACOGNITION_PROPOSAL
        # events landed in events.db + Mind UI but had ZERO downstream
        # Python consumer — agent's next turn rebuilt its system prompt
        # from persona files that had no memory of its own reflections.
        # Now: ``plan`` thoughts → AGENTS.md, ``concern`` thoughts →
        # MEMORY.md, preference_update / curriculum_edit proposals →
        # USER.md / AGENTS.md. ``skill_propose`` proposals still flow
        # through ProposalMaterializer (above) — one materializer per
        # artifact type, no dual writes.
        _app.state.reflection_materializer = None
        try:
            from xmclaw.cognition.reflection_materializer import (
                ReflectionMaterializer,
            )
            from xmclaw.daemon.factory import (
                _resolve_persona_profile_dir,
            )
            _rm_cfg_dict = config or {}

            def _rm_persona_dir() -> Any:
                # Resolved per-event so persona-switch mid-session is
                # honoured (mirrors ProfileExtractor's contract).
                return _resolve_persona_profile_dir(_rm_cfg_dict)

            _rm = ReflectionMaterializer(
                bus=bus,
                persona_dir_provider=_rm_persona_dir,
                cfg=_rm_cfg_dict,
            )
            await _rm.start()
            _app.state.reflection_materializer = _rm
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "reflection_materializer.start_failed err=%s", exc,
            )
            _app.state.reflection_materializer = None

        # B-145: channel adapters (飞书 / 钉钉 / 企微 / Telegram).
        # Each enabled channel gets a long-running adapter that listens
        # for inbound messages + dispatches them through the same
        # AgentLoop as web-UI sessions. Reads
        # ``config.channels.<channel_id>.{enabled, ...creds}``.
        channel_dispatcher = None
        try:
            from xmclaw.daemon.channel_dispatcher import ChannelDispatcher
            from xmclaw.providers.channel.registry import discover as _ch_discover
            channels_cfg = (config or {}).get("channels") or {}
            if isinstance(channels_cfg, dict) and channels_cfg and agent is not None:
                manifests = _ch_discover(include_scaffolds=False)
                # Wave 18: per-user session partitioning. Each channel
                # config can set ``session_per_user: true`` so group
                # chats get one session per sender rather than one
                # shared session.
                _spu_channels: list[str] = []
                for _ch_id, _ch_cfg in channels_cfg.items():
                    if (
                        isinstance(_ch_cfg, dict)
                        and _ch_cfg.get("session_per_user") is True
                    ):
                        _spu_channels.append(_ch_id)
                channel_dispatcher = ChannelDispatcher(
                    agent,
                    app_state=_app.state,
                    session_per_user_channels=frozenset(_spu_channels),
                )
                for ch_id, ch_cfg in channels_cfg.items():
                    if not isinstance(ch_cfg, dict) or not ch_cfg.get("enabled"):
                        continue
                    manifest = manifests.get(ch_id)
                    if manifest is None:
                        log.warning("channel.unknown id=%s", ch_id)
                        continue
                    try:
                        # adapter_factory_path = "module:Class"
                        modpath, clsname = manifest.adapter_factory_path.split(":")
                        mod = __import__(modpath, fromlist=[clsname])
                        AdapterCls = getattr(mod, clsname)
                        adapter_inst = AdapterCls(ch_cfg)
                        channel_dispatcher.add(adapter_inst)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "channel.build_failed id=%s err=%s",
                            ch_id, exc,
                        )
                if channel_dispatcher._adapters:
                    # 2026-05-11 perf fix: don't await start_all() in
                    # lifespan critical path. The feishu adapter's
                    # ``start()`` imports ``lark_oapi``, whose top-level
                    # ``pkg_resources.declare_namespace`` cascade costs
                    # ~3.75s (the entire lifespan budget on cold cache).
                    # Awaiting it serially pushed daemon /health past
                    # the CLI's 10s ``xmclaw start`` timeout, even though
                    # the daemon WAS coming up — just slowly. Spawning
                    # as a background task lets lifespan return in
                    # ~0.05s; channels are "starting up" in parallel
                    # and become reachable as their WS / poll loops
                    # finish handshaking. Each adapter's failures
                    # already log + skip inside start_all itself, so
                    # losing the await doesn't change error semantics.
                    _app.state.channel_dispatcher = channel_dispatcher
                    _app.state.channel_dispatcher_warmup_task = (
                        asyncio.create_task(
                            channel_dispatcher.start_all(),
                            name="channel-dispatcher-warmup",
                        )
                    )
                else:
                    _app.state.channel_dispatcher = None
                    _app.state.channel_dispatcher_warmup_task = None
                    channel_dispatcher = None
            else:
                _app.state.channel_dispatcher = None
        except Exception as exc:  # noqa: BLE001
            log.warning("channel.dispatcher_init_failed err=%s", exc)
            _app.state.channel_dispatcher = None

        # B-142: MCP Hub. Reads ``config.mcp_servers`` (Claude-Desktop
        # shape) + spawns each stdio server as a subprocess. Tools land
        # under name ``<server>__<tool>`` and compose into agent._tools
        # below. MCPHub is itself a ToolProvider so wiring is one-line.
        mcp_hub = None
        try:
            from xmclaw.providers.tool.mcp_hub import MCPHub
            from xmclaw.providers.tool.composite import CompositeToolProvider
            mcp_hub = MCPHub()
            statuses = await mcp_hub.reload_from_config(
                (config or {}).get("mcp_servers"),
            )
            connected = sum(1 for s in statuses.values() if s == "connected")
            if connected > 0 and agent is not None and hasattr(agent, "_tools"):
                if agent._tools is None:
                    agent._tools = mcp_hub
                else:
                    agent._tools = CompositeToolProvider(agent._tools, mcp_hub)
            _app.state.mcp_hub = mcp_hub
        except Exception as exc:  # noqa: BLE001 — MCP failure must not block daemon
            log.warning("mcp.hub_init_failed err=%s", exc)
            _app.state.mcp_hub = None

        # Jarvisification: start cognitive modules when enabled.
        # Default OFF — existing installs are unaffected.
        # Phase 5: the shared cognitive state was loaded before the
        # manager was built so sub-agents inherit the same substrate.
        _cognitive_state = shared_cognitive_state
        _app.state.cognitive_state = _cognitive_state
        _app.state.file_watcher = None
        _app.state.process_watcher = None
        _app.state.evolution_loop = None
        _app.state.task_scheduler = None
        if cognition_cfg.get("enabled", True) and _cognitive_state is not None:

            try:
                from xmclaw.cognition.file_watcher import FileWatcher
                _watch_paths = cognition_cfg.get("watch_paths", [str(Path.home() / "Desktop" / "XMclaw")])
                _fw = FileWatcher(
                    watch_paths=_watch_paths,
                    bus=bus,
                    cognitive_state=_cognitive_state,
                )
                await _fw.start()
                _app.state.file_watcher = _fw
            except Exception as exc:  # noqa: BLE001
                log.warning("cognition.file_watcher_start_failed err=%s", exc)
                _app.state.file_watcher = None

            try:
                from xmclaw.cognition.process_watcher import ProcessWatcher
                _pw = ProcessWatcher(
                    bus=bus,
                    poll_interval_s=float(
                        cognition_cfg.get("process_poll_interval_s", 30.0)
                    ),
                )
                await _pw.start()
                _app.state.process_watcher = _pw
            except Exception as exc:  # noqa: BLE001
                log.warning("cognition.process_watcher_start_failed err=%s", exc)
                _app.state.process_watcher = None

            try:
                from xmclaw.cognition.evolution_loop import EvolutionLoop
                _evo_loop = EvolutionLoop(
                    bus=bus,
                    agent_loop=agent,
                    interval_seconds=float(cognition_cfg.get("evolution_interval_s", 3600.0)),
                )
                await _evo_loop.start()
                _app.state.evolution_loop = _evo_loop
                # Wire into AgentLoop so run_turn can feed tool calls +
                # latencies into the evolution pipeline.
                if agent is not None:
                    agent._evolution_loop = _evo_loop
            except Exception as exc:  # noqa: BLE001
                log.warning("cognition.evolution_loop_start_failed err=%s", exc)
                _app.state.evolution_loop = None

            try:
                from xmclaw.cognition.task_scheduler import TaskScheduler

                async def _task_executor(task):
                    # Route by agent_id so submit_to_agent tasks land on
                    # the correct sub-agent, not always the primary.
                    _mgr = getattr(_app.state, "agents", None)
                    if task.agent_id == "main" or task.agent_id == getattr(
                        _app.state, "agent_id", "main"
                    ):
                        target_agent = _app.state.agent
                    elif _mgr is not None:
                        _ws = _mgr.get(task.agent_id)
                        target_agent = _ws.agent_loop if _ws is not None else None
                    else:
                        target_agent = None
                    if target_agent is None:
                        return f"(no agent wired for {task.agent_id!r})"

                    # Extract caller from the agent-inter caller marker
                    # so a2a tasks get consistent session ids.
                    caller = getattr(
                        _app.state, "agent_id", "main"
                    ) or "main"
                    prompt = task.prompt
                    if prompt.startswith("[Agent "):
                        end = prompt.find(" requesting]")
                        if end > 8:
                            caller = prompt[8:end]

                    # Use a2a session-id format for agent-inter tasks so
                    # they are visually distinct in logs / history.
                    if task.agent_id != "main":
                        stamp = int(time_module.time() * 1000)
                        suffix = uuid.uuid4().hex[:8]
                        sid = f"{caller}:to:{task.agent_id}:{stamp}:{suffix}"
                    else:
                        sid = f"task:{task.id}:{int(time_module.time())}"

                    res = await target_agent.run_turn(sid, prompt)

                    # Extract the last assistant message — same logic as
                    # AgentInterTools._run_background so check_agent_task
                    # returns a clean reply rather than a formatted blob.
                    history = target_agent._histories.get(sid, [])
                    raw_reply = ""
                    for msg in reversed(history):
                        if getattr(msg, "role", None) == "assistant":
                            raw_reply = getattr(msg, "content", "") or ""
                            break
                    if not raw_reply and res is not None:
                        raw_reply = getattr(res, "text", "") or ""

                    # B-307 parity: scan the sub-agent reply for prompt
                    # injection before it lands in the task result.
                    try:
                        from xmclaw.security import (
                            PolicyMode,
                            SOURCE_SUB_AGENT,
                            apply_policy,
                        )
                        policy = getattr(
                            target_agent, "_injection_policy",
                            PolicyMode.DETECT_ONLY,
                        )
                        decision = apply_policy(
                            raw_reply,
                            policy=policy,
                            source=SOURCE_SUB_AGENT,
                            extra={
                                "caller": caller,
                                "callee": task.agent_id,
                                "task_id": task.id,
                                "async": True,
                            },
                        )
                        if decision.blocked:
                            return (
                                "[B-307 sub-agent reply blocked by "
                                "prompt-injection policy — see "
                                "PROMPT_INJECTION_DETECTED event]"
                            )
                        return decision.content
                    except Exception:  # noqa: BLE001
                        return raw_reply

                # Phase 5: derive db_path from the event bus so tasks
                # live in events.db (same WAL, same backup, same prune).
                _task_db_path = None
                if hasattr(bus, "db_path"):
                    _task_db_path = bus.db_path
                _task_sched = TaskScheduler(
                    db_path=_task_db_path,
                    bus=bus,
                    max_concurrent=int(cognition_cfg.get("max_concurrent_tasks", 3)),
                    executor=_task_executor,
                )
                await _task_sched.start()
                _app.state.task_scheduler = _task_sched
            except Exception as exc:  # noqa: BLE001
                log.warning("cognition.task_scheduler_start_failed err=%s", exc)
                _app.state.task_scheduler = None

        # Phase C-4/4: swarm orchestrator.  Wires HTNPlanner + TaskScheduler
        # + MultiAgentManager so the primary agent can dispatch complex goals
        # to the swarm via the ``swarm_dispatch`` tool.
        _swarm = None
        if _app.state.task_scheduler is not None:
            try:
                from xmclaw.daemon.swarm_orchestrator import SwarmOrchestrator
                from xmclaw.cognition.htn_planner import HTNPlanner

                _primary_agent = getattr(_app.state, "agent", None)
                _llm_for_planner = getattr(_primary_agent, "_llm", None)
                _htn = HTNPlanner(
                    llm=_llm_for_planner,
                    max_depth=int(cognition_cfg.get("swarm_max_depth", 3)),
                    max_total_cost_usd=float(
                        cognition_cfg.get("swarm_max_cost_usd", 1.0)
                    ),
                )
                _swarm = SwarmOrchestrator(
                    planner=_htn,
                    scheduler=_app.state.task_scheduler,
                    manager=getattr(_app.state, "agents", None),
                    llm=_llm_for_planner,
                )
                _app.state.swarm_orchestrator = _swarm
            except Exception as exc:  # noqa: BLE001
                log.warning("swarm.orchestrator_init_failed err=%s", exc)
                _app.state.swarm_orchestrator = None

        # Phase 6.7: continuous cognitive daemon. Disabled by default —
        # opted-in via ``cognition.continuous_loop.enabled = true``.
        # This commit ships only the consumer side (heartbeat tick
        # consuming PerceptionBus); percept-source wiring (WS / file /
        # cron pushing INTO the bus) is a separate follow-up.
        _cognitive_daemon = None
        _cont_loop_cfg = ((config or {}).get("cognition") or {}).get(
            "continuous_loop"
        ) or {}
        if _cont_loop_cfg.get("enabled", True):
            try:
                from xmclaw.cognition.perception_bus import PerceptionBus
                from xmclaw.cognition.attention_filter import AttentionFilter
                from xmclaw.cognition.action_dispatcher import ActionDispatcher
                from xmclaw.cognition.cognitive_daemon import (
                    CognitiveDaemon,
                    CognitiveDaemonConfig,
                )

                _cd_cfg = CognitiveDaemonConfig(
                    enabled=True,
                    # 2026-05-10 default flip: 50 = "suggest" tier
                    # (proposes things for review, never auto-applies).
                    # Operator dials down to 0 (observe) or up to 100
                    # (execute) per their trust level.
                    autonomy_level=int(_cont_loop_cfg.get("autonomy_level", 50)),
                    heartbeat_hz=float(_cont_loop_cfg.get("heartbeat_hz", 1.0)),
                    action_threshold=float(
                        _cont_loop_cfg.get("action_threshold", 0.6)
                    ),
                    top_k_focus=int(_cont_loop_cfg.get("top_k_focus", 7)),
                )
                _percept_bus = PerceptionBus()
                _attention = AttentionFilter(
                    cognitive_state=_cognitive_state,
                    bus=_percept_bus,
                    action_threshold=_cd_cfg.action_threshold,
                    top_k_focus=_cd_cfg.top_k_focus,
                )
                # R1: ReflectionCycle — the 3-bucket periodic
                # introspection (5 min reflect / 1 h consolidate /
                # 1 day groom). Wired against the same bus + agent's
                # LLM + UnifiedMemorySystem + CognitiveState so
                # nothing extra needs to be built. Recent-events
                # callback reads from the SqliteEventBus when
                # available — without it reflect_recent silently
                # skips (no LLM cost on a flat journal).
                #
                # R3: also wires a 4th bucket "metacognize" — runs
                # MetaCognitionPass over recent decision traces, lets
                # the Reformer turn patterns into proposals (curriculum
                # _edit / skill_propose / preference_update) emitted as
                # METACOGNITION_PROPOSAL events. Same heartbeat cadence
                # as groom (1 day default).
                _reflection_cycle: Any = None
                _trace_recorder: Any = None
                try:
                    from xmclaw.cognition.reflection_cycle import (
                        ReflectionCycle,
                    )
                    from xmclaw.core.metacognition import (
                        DecisionTraceRecorder,
                        MetaCognitionPass,
                        Reformer,
                    )

                    async def _recent_events(n: int) -> list[Any]:
                        try:
                            from xmclaw.core.bus.sqlite import (
                                SqliteEventBus,
                            )
                            if isinstance(bus, SqliteEventBus):
                                # Latest N events across the whole
                                # daemon — reflection wants a wide
                                # mirror, not just one session.
                                return list(bus.query(limit=n))
                        except Exception:  # noqa: BLE001
                            pass
                        return []

                    _agent_llm = getattr(agent, "_llm", None) if agent else None
                    _agent_unified = (
                        getattr(agent, "_unified_memory", None)
                        if agent else None
                    )

                    # R3: build the metacognition pipeline. Recorder
                    # owns its own decisions.db (sibling of events.db)
                    # so journal back-pressure doesn't bleed in.
                    _meta_pass: Any = None
                    _reformer: Any = None
                    if _agent_llm is not None:
                        try:
                            _trace_recorder = DecisionTraceRecorder()
                            _meta_pass = MetaCognitionPass(
                                llm=_agent_llm,
                                recorder=_trace_recorder,
                            )
                            _reformer = Reformer()
                            _app.state.trace_recorder = _trace_recorder
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "metacognition.build_failed err=%s", exc,
                            )
                            _trace_recorder = None
                            _meta_pass = None
                            _reformer = None

                    # R5: AutonomyPolicy + SuggestionInbox. Even when
                    # metacognition isn't wired (no LLM), the policy
                    # + inbox surface for percept-driven suggestions.
                    try:
                        from xmclaw.cognition.autonomy import (
                            AutonomyPolicy,
                        )
                        from xmclaw.cognition.suggestion_inbox import (
                            SuggestionInbox,
                        )
                        _autonomy_cfg = (
                            _cont_loop_cfg.get("autonomy", {}) or {}
                        )
                        _autonomy = AutonomyPolicy(
                            autonomy_level=int(
                                _cont_loop_cfg.get("autonomy_level", 50),
                            ),
                            risk_overrides=dict(
                                _autonomy_cfg.get(
                                    "risk_overrides", {},
                                ) or {},
                            ),
                            max_auto_applies_per_hour=int(
                                _autonomy_cfg.get(
                                    "max_auto_applies_per_hour", 10,
                                ),
                            ),
                        )
                        _inbox = SuggestionInbox()
                        _app.state.autonomy_policy = _autonomy
                        _app.state.suggestion_inbox = _inbox
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "autonomy.build_failed err=%s", exc,
                        )
                        _app.state.autonomy_policy = None
                        _app.state.suggestion_inbox = None

                    _reflection_cycle = ReflectionCycle(
                        llm=_agent_llm,
                        unified_memory=_agent_unified,
                        cognitive_state=_cognitive_state,
                        bus=bus,
                        recent_events_fn=_recent_events,
                        metacognition_pass=_meta_pass,
                        reformer=_reformer,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "reflection_cycle.build_failed err=%s", exc,
                    )
                    _reflection_cycle = None
                # Sprint 3 #5: wire SkillProposer into the daemon's
                # heartbeat so journal patterns auto-surface as
                # SKILL_CANDIDATE_PROPOSED events. The ProposalMaterializer
                # (started earlier in lifespan) subscribes to these and
                # turns them into real registry entries.
                _skill_proposer = None
                if agent is not None and config is not None:
                    _agent_llm = getattr(agent, "_llm", None)
                    if _agent_llm is not None:
                        try:
                            from xmclaw.core.evolution.proposer import SkillProposer
                            from xmclaw.core.journal import JournalReader
                            from xmclaw.daemon.llm_extractors import (
                                build_skill_extractor,
                            )
                            _sp_cfg = (
                                ((config or {}).get("evolution") or {})
                                .get("skill_proposer") or {}
                            )
                            _skill_proposer = SkillProposer(
                                reader=JournalReader(),
                                extractor_callable=build_skill_extractor(
                                    _agent_llm,
                                ),
                                history_window=int(
                                    _sp_cfg.get("history_window", 50),
                                ),
                                min_pattern_count=int(
                                    _sp_cfg.get("min_pattern_count", 3),
                                ),
                                min_confidence=float(
                                    _sp_cfg.get("min_confidence", 0.5),
                                ),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "skill_proposer.build_failed err=%s", exc,
                            )

                # Phase 6.8: SelfExperimentLoop — wired even without
                # factories so that tick() can propose experiments; factories
                # are injected later when a treatment is available.
                _experiment_loop = None
                try:
                    from xmclaw.cognition.self_experiment import SelfExperimentLoop

                    _experiment_loop = SelfExperimentLoop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("cognition.experiment_loop_init_failed err=%s", exc)

                # Phase 6.8b: inject factories so tick() can execute full
                # A/B cycles, not just propose.  Baseline re-uses the live
                # agent (each benchmark task gets a fresh session_id, so
                # history isolation is preserved).  Treatment currently
                # mirrors baseline — a future phase wires the
                # MutationOrchestrator to supply a mutated variant.
                if _experiment_loop is not None and agent is not None:
                    try:
                        from xmclaw.eval import SUITE_REGISTRY

                        def _baseline_factory():
                            return agent

                        def _treatment_factory(
                            overrides: dict[str, int] | None = None,
                        ):
                            # If overrides are passed (from
                            # SelfExperimentLoop isolation), use them
                            # directly.  Otherwise fall back to scanning
                            # the registry for all non-HEAD candidates.
                            if _evo_registry is not None:
                                if overrides is None:
                                    overrides = {}
                                    for sid in _evo_registry.list_skill_ids():
                                        head = _evo_registry.active_version(sid)
                                        for v in _evo_registry.list_versions(sid):
                                            if v != head:
                                                overrides[sid] = v
                                                break
                                if overrides:
                                    import copy

                                    from xmclaw.providers.tool.composite import (
                                        CompositeToolProvider,
                                    )
                                    from xmclaw.skills.registry import (
                                        SkillRegistryView,
                                    )
                                    from xmclaw.skills.tool_bridge import (
                                        SkillToolProvider,
                                    )

                                    view = SkillRegistryView(
                                        _evo_registry, overrides,
                                    )
                                    mutant_skills = SkillToolProvider(view)

                                    def _replace(root: Any) -> Any:
                                        if isinstance(
                                            root, SkillToolProvider,
                                        ):
                                            return mutant_skills
                                        _kids = (
                                            getattr(root, "children", None)
                                            or getattr(root, "_children", None)
                                            or []
                                        )
                                        if _kids:
                                            _new = [
                                                _replace(c) for c in _kids
                                            ]
                                            return CompositeToolProvider(
                                                *_new,
                                            )
                                        return root

                                    treatment = copy.copy(agent)
                                    if agent._tools is not None:
                                        treatment._tools = _replace(
                                            agent._tools,
                                        )
                                    return treatment
                            return agent

                        def _load_suite(suite_id: str):
                            cls = SUITE_REGISTRY.get(suite_id)
                            if cls is None:
                                raise KeyError(
                                    f"unknown suite {suite_id!r}; "
                                    f"registered: {list(SUITE_REGISTRY.keys())}"
                                )
                            return cls()

                        _experiment_loop.set_factories(
                            baseline_factory=_baseline_factory,
                            treatment_factory=_treatment_factory,
                            load_suite=_load_suite,
                            suite_id="longmemeval_mini",
                        )
                        # Phase E debt-2: wire candidate resolver so
                        # multi-skill experiments are isolated one-at-a-time.
                        if _evo_registry is not None:
                            def _resolve_candidates() -> dict[str, int]:
                                out: dict[str, int] = {}
                                for sid in _evo_registry.list_skill_ids():
                                    head = _evo_registry.active_version(sid)
                                    for v in _evo_registry.list_versions(sid):
                                        if v != head:
                                            out[sid] = v
                                            break
                                return out

                            _experiment_loop.set_candidate_resolver(
                                _resolve_candidates,
                            )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "cognition.experiment_loop_factories_failed err=%s",
                            exc,
                        )

                # Phase 6.2: ReasoningEngine — uses the agent's LLM, the
                # memory graph, and the strategy bank if available.
                _reasoning = None
                try:
                    from xmclaw.cognition.reasoning import ReasoningEngine

                    _reasoning = ReasoningEngine(
                        llm=getattr(agent, "_llm", None),
                        graph=getattr(_app.state, "memory_graph", None),
                        bank=getattr(agent, "_strategy_bank", None),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("cognition.reasoning_engine_init_failed err=%s", exc)

                # Phase 6.3: Planner — drives the plan → dispatch pipeline
                # inside CognitiveDaemon._react_to_percept.
                _planner = None
                try:
                    from xmclaw.cognition.planner import Planner

                    _planner = Planner(
                        llm=getattr(agent, "_llm", None),
                        skill_registry=_evo_registry,
                        reasoning_engine=_reasoning,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("cognition.planner_init_failed err=%s", exc)

                # Phase D: tick history store for /daemon/history.
                _tick_store = None
                try:
                    from xmclaw.cognition.tick_store import TickStore

                    _tick_store = TickStore()
                except Exception as exc:  # noqa: BLE001
                    log.warning("cognition.tick_store_init_failed err=%s", exc)

                _cognitive_daemon = CognitiveDaemon(
                    config=_cd_cfg,
                    bus=_percept_bus,
                    attention=_attention,
                    cognitive_state=_cognitive_state,
                    dispatcher=ActionDispatcher(
                        agent_loop=agent,
                        skill_registry=_evo_registry,
                        tool_provider=getattr(agent, "_tools", None),
                    ),
                    reflection_cycle=_reflection_cycle,
                    skill_proposer=_skill_proposer,
                    event_bus=bus,
                    experiment_loop=_experiment_loop,
                    reasoning=_reasoning,
                    planner=_planner,
                    process_watcher=getattr(_app.state, "process_watcher", None),
                    tick_store=_tick_store,
                )
                _app.state.perception_bus = _percept_bus
                # Phase 6 wiring A: subscribe existing event sources
                # to the PerceptionBus. Each attach is a per-source
                # opt-in: the source must already be built (its own
                # config flag is on) for us to wire it up. Wiring
                # failures must not kill startup, so the whole block
                # is wrapped in try/except.
                _percept_sources: Any = None
                try:
                    from xmclaw.cognition.percept_sources import (
                        PerceptSourceRegistry,
                    )
                    _percept_sources = PerceptSourceRegistry(_percept_bus)
                    _fw = getattr(_app.state, "file_watcher", None)
                    if _fw is not None:
                        await _percept_sources.attach_file_watcher(_fw)
                    _pw = getattr(_app.state, "process_watcher", None)
                    if _pw is not None:
                        await _percept_sources.attach_process_watcher(_pw)
                    if agent is not None:
                        _percept_sources.attach_user_message_hook(agent)
                    if cron_tick is not None:
                        _percept_sources.attach_cron_hook(cron_tick)
                    # Phase B: forward high-signal internal events
                    # (skill promoted, goals groomed, etc.) as percepts.
                    if bus is not None:
                        _percept_sources.attach_internal_events(bus)
                except Exception as exc:  # noqa: BLE001
                    log.warning("percept_sources.attach_failed err=%s", exc)
                    _percept_sources = None
                _app.state.percept_sources = _percept_sources

                # R4 (2026-05-10) — multi-modal perception watchers.
                # Off by default per privacy posture; operator opts
                # in via cfg.cognition.perception.{screen,window,
                # clipboard,calendar}.enabled. Each watcher gracefully
                # degrades when its optional dep isn't installed —
                # the factory drops unavailable ones.
                _multimodal_sources: list = []
                try:
                    from xmclaw.cognition.perception import (
                        build_perception_sources_from_config,
                    )
                    _multimodal_sources = (
                        build_perception_sources_from_config(
                            config, bus=_percept_bus,
                        )
                    )
                    for src in _multimodal_sources:
                        try:
                            await src.start()
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "perception.%s.start_failed err=%s",
                                getattr(src, "name", "?"), exc,
                            )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "multimodal_perception.build_failed err=%s",
                        exc,
                    )
                    _multimodal_sources = []
                _app.state.multimodal_perception = _multimodal_sources
                await _cognitive_daemon.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("cognitive_daemon.start_failed err=%s", exc)
                _cognitive_daemon = None
        _app.state.cognitive_daemon = _cognitive_daemon
        _app.state.experiment_loop = _experiment_loop

        # 2026-05-11: log lifespan startup duration. /api/v2/status
        # surfaces this so the UI can show "daemon ready in 1.2s"
        # and we can spot regressions (Epic #25 broke this when
        # feishu's lark_oapi import was on the critical path —
        # 3.78s startup → /health timeout). The channel adapter
        # warmup task is intentionally NOT awaited here; if a user
        # wants to know "are channels ready", that's a separate
        # status flag we surface via channel_dispatcher_warmup_task.
        _lifespan_elapsed_s = round(
            time_module.perf_counter() - _lifespan_t0, 3,
        )
        _app.state.lifespan_startup_duration_s = _lifespan_elapsed_s
        log.info(
            "lifespan.startup_complete duration_s=%.3f",
            _lifespan_elapsed_s,
        )

        # Sprint 1 Wave 2: AutobiographicalMemory — structured "who
        # the user is" store. Hooked into AgentLoop for extraction on
        # user message + recall at turn start.
        autobio_cfg = (
            (config.get("cognition") or {}).get("autobiographical", {})
            if isinstance(config, dict) else {}
        )
        autobio_mem = None
        if (
            not isinstance(autobio_cfg, dict)
            or autobio_cfg.get("enabled", True)
        ):
            try:
                from xmclaw.cognition.autobiographical_memory import (
                    AutobiographicalMemory,
                )
                autobio_mem = AutobiographicalMemory()
                _app.state.autobio_memory = autobio_mem
                if agent is not None:
                    try:
                        agent._autobio_memory = autobio_mem
                    except Exception:  # noqa: BLE001
                        pass
                # Wave 25.6: bridge ProfileExtractor's LLM-derived
                # deltas into the autobio SQL tables. Without this,
                # users who give commands instead of "我喜欢 X" self-
                # statements never populate the structured store.
                try:
                    sub = autobio_mem.subscribe_to_bus(bus)
                    if sub is not None:
                        _app.state.autobio_bus_subscription = sub
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "autobiographical_memory.bus_subscribe_failed err=%s",
                        exc,
                    )
                log.info("autobiographical_memory.started")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "autobiographical_memory.start_failed err=%s", exc,
                )

        # Wave 27: Memory v2 — Fact/Relation + LanceDB-backed L1 +
        # deterministic key-info extractor on every user message.
        # Opt-in via cognition.memory_v2.enabled (default False during
        # rollout). When enabled:
        #   * facts live under ~/.xmclaw/v2/facts/ (LanceDB dataset)
        #   * agent_loop.run_turn force-extracts URL/account/numeric-
        #     goal/explicit-remember patterns from every user message
        #   * future Phase 4a will inject these into the LLM's system
        #     prompt so the agent sees the v2 facts automatically
        # See docs/MEMORY_EVOLUTION_REDESIGN.md for the full plan.
        memory_v2_cfg = (
            (config.get("cognition") or {}).get("memory_v2", {})
            if isinstance(config, dict) else {}
        )
        memory_v2_service = None
        if (
            isinstance(memory_v2_cfg, dict)
            and memory_v2_cfg.get("enabled", False)
        ):
            try:
                from xmclaw.memory.v2 import (
                    MemoryService,
                    build_embedding_service,
                    get_lancedb_graph_backend,
                    get_lancedb_vector_backend,
                )
                from xmclaw.utils.paths import data_dir as _data_dir
                facts_dir = _data_dir() / "v2" / "facts"
                facts_dir.mkdir(parents=True, exist_ok=True)
                # Build embedding service; None falls back to keyword.
                embedder = build_embedding_service(cfg=config)
                # Embedding dim must match the configured embedding
                # model. Default 1536 (OpenAI text-embedding-3-small).
                dim = embedder.dim if embedder else 1536
                vec_backend = get_lancedb_vector_backend(
                    str(facts_dir), embedding_dim=dim,
                )
                graph_backend = get_lancedb_graph_backend(str(facts_dir))
                memory_v2_service = MemoryService(
                    vector_backend=vec_backend,
                    graph_backend=graph_backend,
                    embedder=embedder,
                )
                _app.state.memory_v2_service = memory_v2_service
                if agent is not None:
                    try:
                        agent._memory_service_v2 = memory_v2_service
                    except Exception:  # noqa: BLE001
                        pass
                log.info(
                    "memory_v2.started path=%s dim=%d embedder=%s",
                    facts_dir, dim,
                    embedder.name if embedder else "(none, keyword fallback)",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "memory_v2.start_failed err=%s "
                    "(daemon continues without v2)", exc,
                )

        # Sprint 1: ProactiveAgent — periodic trigger evaluator that
        # publishes PROACTIVE_PROPOSAL events when the agent should
        # speak without being asked. Reads cognition.proactive.*
        # config; opt-out via cognition.proactive.enabled=false.
        proactive_agent = None
        proactive_cfg = (
            (config.get("cognition") or {}).get("proactive", {})
            if isinstance(config, dict) else {}
        )
        if (
            not isinstance(proactive_cfg, dict)
            or proactive_cfg.get("enabled", True)
        ):
            try:
                from xmclaw.cognition.proactive_agent import (
                    ProactiveAgent,
                    IdleCheckInTrigger,
                    SystemHealthTrigger,
                )

                async def _publish_proactive(type_str: str, payload: dict):
                    from xmclaw.core.bus import EventType, make_event
                    ev = make_event(
                        session_id="proactive",
                        agent_id="proactive",
                        type=EventType.PROACTIVE_PROPOSAL,
                        payload=payload,
                    )
                    await bus.publish(ev)

                proactive_agent = ProactiveAgent(
                    publish=_publish_proactive,
                    tick_interval_s=float(
                        proactive_cfg.get("tick_interval_s", 30.0)
                        if isinstance(proactive_cfg, dict) else 30.0
                    ),
                    global_min_gap_s=float(
                        proactive_cfg.get("global_min_gap_s", 60.0)
                        if isinstance(proactive_cfg, dict) else 60.0
                    ),
                    quiet_start_hour=int(
                        proactive_cfg.get("quiet_start_hour", 23)
                        if isinstance(proactive_cfg, dict) else 23
                    ),
                    quiet_end_hour=int(
                        proactive_cfg.get("quiet_end_hour", 7)
                        if isinstance(proactive_cfg, dict) else 7
                    ),
                    memory=getattr(_app.state, "memory", None),
                    perception_bus=getattr(
                        _app.state, "perception_bus", None,
                    ),
                    cron_store=getattr(_app.state, "cron_store", None),
                    agent_loop=agent,
                )
                # Default trigger set. User can disable individual
                # triggers via cognition.proactive.disabled_triggers.
                disabled = set(
                    proactive_cfg.get("disabled_triggers") or []
                    if isinstance(proactive_cfg, dict) else []
                )
                if "idle_check_in" not in disabled:
                    proactive_agent.register_trigger(IdleCheckInTrigger())
                if "system_health" not in disabled:
                    proactive_agent.register_trigger(SystemHealthTrigger())
                # Sprint 2 Wave 5: environment-aware triggers. ICS
                # calendar reminder requires user to point at an
                # exported .ics file. Stale project trigger reads
                # autobiographical_memory projects.
                try:
                    from xmclaw.cognition.triggers_environment import (
                        CalendarReminderTrigger,
                        StaleProjectTrigger,
                    )
                    ics_path = (
                        proactive_cfg.get("calendar_ics_path")
                        if isinstance(proactive_cfg, dict) else None
                    )
                    if (
                        "calendar_reminder" not in disabled
                        and isinstance(ics_path, str) and ics_path.strip()
                    ):
                        proactive_agent.register_trigger(
                            CalendarReminderTrigger(
                                ics_path=ics_path.strip(),
                            ),
                        )
                    if (
                        "stale_project" not in disabled
                        and autobio_mem is not None
                    ):
                        proactive_agent.register_trigger(
                            StaleProjectTrigger(),
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "environment_triggers.register_failed err=%s", exc,
                    )

                # Sprint 2 Wave 11: cron-scheduled triggers
                # (config.cognition.proactive.cron_jobs).
                try:
                    from xmclaw.cognition.triggers_cron import (
                        build_cron_triggers_from_config,
                    )
                    cron_jobs_cfg = (
                        proactive_cfg.get("cron_jobs")
                        if isinstance(proactive_cfg, dict) else None
                    )
                    cron_triggers = build_cron_triggers_from_config(
                        cron_jobs_cfg,
                    )
                    for t in cron_triggers:
                        if t.name in disabled:
                            continue
                        proactive_agent.register_trigger(t)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "cron_triggers.register_failed err=%s", exc,
                    )

                # Sprint 2 Wave 16: daily digest trigger. Reads
                # cognition.proactive.daily_digest.{enabled, schedule,
                # lookback_h, urgency}; default schedule "0 22 * * *"
                # = 10pm every day. Opt-out via daily_digest.enabled=
                # false or "daily_digest" in disabled set.
                try:
                    digest_cfg = (
                        proactive_cfg.get("daily_digest", {})
                        if isinstance(proactive_cfg, dict) else {}
                    )
                    if not isinstance(digest_cfg, dict):
                        digest_cfg = {}
                    # 2026-05-14 default-flip: 22:00 read-only summary,
                    # no risk surface. Explicit ``enabled: false`` opts
                    # out; "daily_digest" in the disabled set also opts
                    # out.
                    if (
                        digest_cfg.get("enabled", True)
                        and "daily_digest" not in disabled
                    ):
                        from xmclaw.cognition.triggers_digest import (
                            DailyDigestTrigger,
                        )
                        digest_trigger = DailyDigestTrigger(
                            bus=bus,
                            schedule_expr=str(
                                digest_cfg.get("schedule")
                                or "0 22 * * *",
                            ),
                            lookback_h=float(
                                digest_cfg.get("lookback_h") or 24.0,
                            ),
                            urgency=str(
                                digest_cfg.get("urgency") or "normal",
                            ),
                            agent_loop=agent,
                        )
                        if digest_trigger._next_fire_ts is not None:
                            proactive_agent.register_trigger(
                                digest_trigger,
                            )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "daily_digest.register_failed err=%s", exc,
                    )

                await proactive_agent.start()
                _app.state.proactive_agent = proactive_agent
                # Back-reference so AgentLoop can call note_user_message
                # without hardcoding a lifespan dependency.
                if agent is not None:
                    try:
                        agent._proactive_agent = proactive_agent
                    except Exception:  # noqa: BLE001
                        pass
                log.info(
                    "proactive_agent.started triggers=%s",
                    proactive_agent.trigger_names(),
                )

                # Sprint 2 Wave 9: fan out PROACTIVE_PROPOSAL events to
                # configured IM channels (飞书 / Telegram / …) so phone
                # users get a native push instead of having to keep the
                # web UI open. Each channel must opt in by setting
                # ``proactive_chat_id`` in its config block.
                try:
                    from xmclaw.cognition.proactive_channel_bridge import (
                        build_bridge_from_config,
                    )
                    _channel_dispatcher = getattr(
                        _app.state, "channel_dispatcher", None,
                    )
                    _adapters_list: list[Any] = []
                    if _channel_dispatcher is not None:
                        _adapters_list = list(
                            getattr(_channel_dispatcher, "_adapters", [])
                            or [],
                        )
                    _channel_push_cfg = (
                        proactive_cfg.get("channel_push", {})
                        if isinstance(proactive_cfg, dict) else {}
                    )
                    _bridge = build_bridge_from_config(
                        bus=bus,
                        channels_config=(
                            (config or {}).get("channels") or {}
                        ),
                        proactive_push_config=_channel_push_cfg,
                        adapters=_adapters_list,
                    )
                    if _bridge is not None:
                        await _bridge.start()
                        _app.state.proactive_channel_bridge = _bridge
                        log.info(
                            "proactive_channel_bridge.started targets=%d",
                            _bridge.target_count(),
                        )
                    else:
                        _app.state.proactive_channel_bridge = None
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "proactive_channel_bridge.start_failed err=%s",
                        exc,
                    )
                    _app.state.proactive_channel_bridge = None
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "proactive_agent.start_failed err=%s", exc,
                )
                proactive_agent = None

        try:
            yield
        finally:
            # B-67: every stop wrapped in try/except. Previously the
            # first two (sweep_task, backup_scheduler) raised bare; if
            # either's stop raised (e.g. cancelled-task collision on
            # rapid restart), every subsequent shutdown step was
            # skipped and background tasks leaked across the daemon's
            # lifetime. Now: each step is independent.
            if proactive_agent is not None:
                try:
                    await proactive_agent.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            if sweep_task is not None:
                try:
                    await sweep_task.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            if backup_scheduler is not None:
                try:
                    await backup_scheduler.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            if events_retention_task is not None:
                try:
                    await events_retention_task.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            if cron_tick is not None:
                try:
                    await cron_tick.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-41: stop the memory indexer.
            _idx = getattr(_app.state, "memory_indexer", None)
            if _idx is not None:
                try:
                    await _idx.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-142: stop every MCP subprocess so we don't leak
            # JSON-RPC stdio clients across daemon restarts.
            _mcp = getattr(_app.state, "mcp_hub", None)
            if _mcp is not None:
                try:
                    await _mcp.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-145: stop every channel adapter (飞书 WS, 钉钉 stream,
            # telegram poll loop). Same try-each posture so a hanging
            # SDK shutdown doesn't strand the others.
            # 2026-05-11 perf fix: also cancel the warmup task in
            # case startup is interrupted before adapters finish
            # connecting (e.g. fast SIGINT during cold start). Without
            # this the cancelled lifespan would leave the warmup task
            # dangling, importing lark_oapi after the bus is gone.
            _chdisp_warmup = getattr(
                _app.state, "channel_dispatcher_warmup_task", None,
            )
            if _chdisp_warmup is not None and not _chdisp_warmup.done():
                _chdisp_warmup.cancel()
                try:
                    await _chdisp_warmup
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            _chdisp = getattr(_app.state, "channel_dispatcher", None)
            if _chdisp is not None:
                try:
                    await _chdisp.stop_all()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-51: stop the dream cron.
            _dream_cron = getattr(_app.state, "dream_cron", None)
            if _dream_cron is not None:
                try:
                    await _dream_cron.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-109: stop the config-file watcher.
            _cw = getattr(_app.state, "config_watcher", None)
            if _cw is not None:
                try:
                    await _cw.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # Epic #17 Phase 7: stop all workspace background work
            # before tearing down the bus + memory store. Evolution
            # observers cancel their subscriptions here; LLM workspaces
            # are a no-op.
            for _ws_id in agents_manager.list_ids():
                _ws = agents_manager.get(_ws_id)
                if _ws is None:
                    continue
                try:
                    await _ws.stop()
                except Exception as exc:  # noqa: BLE001 — one bad stop must not abort shutdown
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            if orchestrator is not None:
                try:
                    await orchestrator.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-294: stop the evaluation trigger BEFORE the observer so
            # any in-flight debounce timer doesn't try to call .evaluate()
            # on a stopped observer.
            _eval_trig = getattr(_app.state, "evolution_evaluation_trigger", None)
            if _eval_trig is not None:
                try:
                    await _eval_trig.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-295: stop the variant selector. Same ordering rationale
            # as eval_trigger — stop subscribers before the observer
            # so an in-flight ingest doesn't crash on a torn-down bus.
            _vs = getattr(_app.state, "variant_selector", None)
            if _vs is not None:
                try:
                    await _vs.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # Epic #24 Phase 1: stop the default EvolutionAgent observer.
            _evo_obs = getattr(_app.state, "evolution_observer", None)
            if _evo_obs is not None:
                try:
                    await _evo_obs.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # Epic #24 Phase 2.3: stop the JournalWriter + ProfileExtractor.
            # Both flush in-flight session buffers so SIGINT mid-session
            # doesn't drop the pending journal row / delta lines.
            _jw = getattr(_app.state, "journal_writer", None)
            if _jw is not None:
                try:
                    await _jw.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            _pe = getattr(_app.state, "profile_extractor", None)
            if _pe is not None:
                try:
                    await _pe.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-164: stop the realtime trigger first so it doesn't
            # try to fire run_once() while skill_dream is shutting down.
            _rt = getattr(_app.state, "realtime_evolution", None)
            if _rt is not None:
                try:
                    await _rt.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-167: stop the proposal materializer so it doesn't try
            # to register skills mid-shutdown when the registry is
            # about to go away with the orchestrator.
            _pm = getattr(_app.state, "proposal_materializer", None)
            if _pm is not None:
                try:
                    await _pm.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # 2026-05-12: stop the reflection materializer.
            _rm = getattr(_app.state, "reflection_materializer", None)
            if _rm is not None:
                try:
                    await _rm.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-173: stop the skills watcher so a tick doesn't fire
            # mid-shutdown.
            _sw = getattr(_app.state, "skills_watcher", None)
            if _sw is not None:
                try:
                    await _sw.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # B-172: stop the mutation orchestrator so an in-flight
            # DSPy compile doesn't keep the loop busy past shutdown.
            _mo = getattr(_app.state, "mutation_orchestrator", None)
            if _mo is not None:
                try:
                    await _mo.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # Sprint 3 #3: stop the SleepWorker BEFORE skill_dream /
            # memory_sweep so an in-flight idle-fired task doesn't try
            # to call ``run_once()`` / ``sweep_once()`` on a stopped
            # downstream. SleepWorker.stop() cancels the in-flight
            # task with rollback (SLEEP_INTERRUPTED published) so any
            # buffered writes are discarded cleanly.
            _sw = getattr(_app.state, "sleep_worker", None)
            if _sw is not None:
                try:
                    await _sw.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # Epic #24 Phase 3.2: stop the skill_dream periodic task.
            _sd = getattr(_app.state, "skill_dream", None)
            if _sd is not None:
                try:
                    await _sd.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            if memory is not None and hasattr(memory, "close"):
                try:
                    memory.close()
                except Exception:  # noqa: BLE001
                    pass
            # Phase 6.7: stop the continuous cognitive daemon BEFORE
            # the rest of cognition shuts down, so a final tick can't
            # try to drain a dying PerceptionBus / call a torn-down
            # AttentionFilter / dispatcher.
            _cd = getattr(_app.state, "cognitive_daemon", None)
            if _cd is not None:
                try:
                    await _cd.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            # Phase 6 wiring A: detach percept sources so the upstream
            # producers stop pushing into a bus we're tearing down.
            _ps = getattr(_app.state, "percept_sources", None)
            if _ps is not None:
                try:
                    await _ps.detach_all()
                except Exception:  # noqa: BLE001
                    pass
            # Jarvisification: stop cognitive modules.
            # Persist cognitive state before shutting down.
            if _cognitive_state is not None:
                try:
                    from xmclaw.utils.paths import default_cognitive_state_path
                    _state_path = default_cognitive_state_path()
                    _state_path.parent.mkdir(parents=True, exist_ok=True)
                    import json
                    _state_path.write_text(
                        json.dumps(_cognitive_state.to_dict(), indent=2),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001
                    pass
            _task_sched = getattr(_app.state, "task_scheduler", None)
            if _task_sched is not None:
                try:
                    if hasattr(_task_sched, "stop"):
                        await _task_sched.stop()
                except Exception as exc:  # noqa: BLE001
                    # Pre-existing bug fix (R2 follow-up, 2026-05-10):
                    # ``except Exception:`` (no ``as``) followed by
                    # ``type(exc).__name__`` raised UnboundLocalError
                    # — surfaced when a fake scheduler without ``stop``
                    # was injected for tests, but real-world too.
                    log.warning(
                        "task_scheduler stop failed during shutdown: %s",
                        type(exc).__name__, exc_info=True,
                    )
            _evo_loop = getattr(_app.state, "evolution_loop", None)
            if _evo_loop is not None:
                try:
                    await _evo_loop.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            _fw = getattr(_app.state, "file_watcher", None)
            if _fw is not None:
                try:
                    await _fw.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            _pw = getattr(_app.state, "process_watcher", None)
            if _pw is not None:
                try:
                    await _pw.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed during shutdown", type(exc).__name__, exc_info=True)
            _graph = getattr(_app.state, "memory_graph", None)
            if _graph is not None:
                try:
                    _graph.close()
                except Exception:  # noqa: BLE001
                    pass
            # R4: stop multi-modal perception sources before tearing
            # down the perception bus.
            for _src in getattr(_app.state, "multimodal_perception", []) or []:
                try:
                    await _src.stop()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "multimodal_perception.%s.stop_failed err=%s",
                        getattr(_src, "name", "?"), exc,
                    )

    return _lifespan

