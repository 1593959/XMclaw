"""xm-auto-evo bridge — exports XMclaw events into CoPaw dialog format
and manages the Node.js heartbeat subprocess.

Phase B-16. Connects the Python daemon to the user's pre-existing
Node.js evolution system at ``C:\\Users\\15978\\Desktop\\xm-auto-evo``
without re-implementing it. Two halves:

* ``DialogExporter`` subscribes to the BehavioralEvent bus and
  appends every user / assistant / tool turn to
  ``<workspace>/dialog/YYYY-MM-DD.jsonl`` in the CoPaw schema that
  ``signals.js`` knows how to parse:

      { id, role, content: [{type, text|thinking|tool_use|...}],
        metadata: {...}, timestamp }

  This is the bridge: as far as xm-auto-evo is concerned, XMclaw
  conversations look like CoPaw conversations.

* ``AutoEvoProcess`` spawns ``node xm-auto-evo/index.js heartbeat``
  as a managed child of the FastAPI lifespan. The child:
    - reads ``<workspace>/dialog/`` (which DialogExporter feeds)
    - writes ``<workspace>/events.jsonl`` + ``<workspace>/genes.json``
      + ``<workspace>/capsules.jsonl`` etc as it observes / evolves
    - logs to ``<workspace>/auto_evo.log`` (so the UI Logs page can
      tail it via the existing /api/v2/logs?file=auto_evo route)

The bridge is gated on ``cfg["evolution"]["xm_auto_evo"]`` — disabled
by default, opt-in via Web UI.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from xmclaw.core.bus.events import BehavioralEvent, EventType
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


def auto_evo_workspace() -> Path:
    """Where xm-auto-evo runs. Separate from XMclaw's main data dir so
    the JSON / JSONL files xm-auto-evo writes don't clutter the daemon
    state, and so a wipe of one doesn't nuke the other."""
    from xmclaw.utils.paths import data_dir
    return data_dir() / "auto_evo"


def auto_evo_repo_path(cfg: dict[str, Any] | None = None) -> Path:
    """Resolve the on-disk path to the xm-auto-evo Node project.

    Resolution order:
      1. cfg["evolution"]["auto_evo"]["path"]   (canonical, B-18)
      2. cfg["evolution"]["xm_auto_evo"]["path"] (legacy alias, B-17 typo)
      3. env XMC_AUTO_EVO_PATH
      4. **vendored copy** at xmclaw/evolution_core/ (B-17) — the
         project ships with its evolution core so a fresh install
         has it out of the box, no separate clone needed
      5. fallback: ~/Desktop/xm-auto-evo (legacy dev location)
    """
    evo_root = (cfg or {}).get("evolution", {}) or {}
    section = evo_root.get("auto_evo") or evo_root.get("xm_auto_evo") or {}
    raw = section.get("path") if isinstance(section, dict) else None
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    env_raw = os.environ.get("XMC_AUTO_EVO_PATH")
    if env_raw:
        return Path(env_raw).expanduser()
    # Vendored: xmclaw/evolution_core/ relative to this module.
    here = Path(__file__).resolve().parent.parent  # xmclaw/
    vendored = here / "evolution_core"
    if (vendored / "index.js").is_file():
        return vendored
    return Path.home() / "Desktop" / "xm-auto-evo"


# ──────────────────────────────────────────────────────────────────────
# DialogExporter — CoPaw-format JSONL fan-out
# ──────────────────────────────────────────────────────────────────────


class DialogExporter:
    """Subscribes to the bus and appends to dialog/YYYY-MM-DD.jsonl
    in **XMclaw native Message format** (NOT CoPaw — see B-16 commit
    note: the user explicitly asked xm-auto-evo to adapt to our
    shape, not the other way around).

    XMclaw shape — flat, OpenAI-compatible:

      { id, session_id, ts, role, content (string),
        tool_calls?: [{id, name, args}],
        tool_call_id?: <set on role="tool"> }

    One row per user message, assistant turn, or tool result. The
    matching xm-auto-evo signals.js patch (this commit) recognises
    both string ``content`` (XMclaw) and array ``content`` (legacy
    CoPaw) so it works against either source.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._dialog_dir = workspace / "dialog"
        self._dialog_dir.mkdir(parents=True, exist_ok=True)
        # Bounded LRU of "this turn already exported" keys.
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._seen_max = 4096
        # Buffer tool_calls observed for an in-flight assistant turn
        # (LLM_CHUNK / TOOL_CALL_EMITTED arrive before LLM_RESPONSE).
        # Flushed on LLM_RESPONSE so each assistant message lands in
        # a single JSONL row with its tool_calls already attached.
        self._pending_tool_calls: dict[str, list[dict[str, Any]]] = {}

    def _today_path(self) -> Path:
        return self._dialog_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

    def _record_seen(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > self._seen_max:
            evict = self._seen_order.pop(0)
            self._seen.discard(evict)
        return True

    def _append(self, entry: dict[str, Any]) -> None:
        try:
            with self._today_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            _log.warning("dialog_export_failed", extra={"err": str(exc)})

    async def on_event(self, ev: BehavioralEvent) -> None:
        try:
            t = ev.type
            sid = ev.session_id or "unknown"
            corr = ev.correlation_id or ev.id or ""
            payload = ev.payload or {}

            if t == EventType.USER_MESSAGE:
                key = f"user:{corr}" if corr else f"user:{sid}:{ev.ts}"
                if not self._record_seen(key):
                    return
                self._append({
                    "id": key,
                    "session_id": sid,
                    "ts": ev.ts,
                    "role": "user",
                    "content": payload.get("content") or "",
                    "correlation_id": corr,
                })
                return

            if t == EventType.LLM_RESPONSE:
                key = f"asst:{corr}" if corr else f"asst:{sid}:{ev.ts}"
                if not self._record_seen(key):
                    return
                final_text = payload.get("content") or payload.get("text") or ""
                tool_calls = self._pending_tool_calls.pop(corr, [])
                entry: dict[str, Any] = {
                    "id": key,
                    "session_id": sid,
                    "ts": ev.ts,
                    "role": "assistant",
                    "content": final_text,
                    "correlation_id": corr,
                }
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                self._append(entry)
                return

            if t == EventType.TOOL_CALL_EMITTED:
                self._pending_tool_calls.setdefault(corr, []).append({
                    "id": payload.get("tool_call_id") or payload.get("id") or "",
                    "name": payload.get("name") or payload.get("tool_name") or "tool",
                    "args": payload.get("args") or payload.get("arguments") or {},
                })
                return

            if t == EventType.TOOL_INVOCATION_FINISHED:
                key = f"tool:{payload.get('tool_call_id') or corr}:{ev.ts}"
                if not self._record_seen(key):
                    return
                result = payload.get("result")
                err = payload.get("error")
                content_text: str
                if err:
                    content_text = str(err)
                elif isinstance(result, str):
                    content_text = result
                else:
                    content_text = json.dumps(result or {}, ensure_ascii=False)
                self._append({
                    "id": key,
                    "session_id": sid,
                    "ts": ev.ts,
                    "role": "tool",
                    "tool_call_id": payload.get("tool_call_id") or "",
                    "content": content_text[:8000],
                    "is_error": bool(err),
                    "correlation_id": corr,
                })
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "dialog_export_handler_failed",
                extra={"err": str(exc), "type": str(getattr(ev, "type", "?"))},
            )


# ──────────────────────────────────────────────────────────────────────
# AutoEvoProcess — managed Node.js subprocess
# ──────────────────────────────────────────────────────────────────────


class AutoEvoProcess:
    """Manages ``node xm-auto-evo/index.js heartbeat`` lifecycle.

    Started from the FastAPI lifespan when config enables it; stopped
    on shutdown. Captures stdout to a log file under the auto_evo
    workspace so the UI Logs page can tail it.
    """

    def __init__(
        self,
        repo_path: Path,
        workspace: Path,
        *,
        interval_min: int = 30,
    ) -> None:
        self._repo_path = repo_path
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._interval_min = max(1, int(interval_min))
        self._proc: subprocess.Popen | None = None
        self._log_path = workspace / "auto_evo.log"

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    async def start(self) -> dict[str, Any]:
        """Spawn the heartbeat subprocess. Idempotent — if already
        running, returns the existing pid without respawning."""
        if self.is_running:
            return {"ok": True, "running": True, "pid": self._proc.pid, "noop": True}

        index_js = self._repo_path / "index.js"
        if not index_js.is_file():
            return {
                "ok": False,
                "error": f"xm-auto-evo not found at {index_js}",
                "hint": "set evolution.auto_evo.path in config",
            }

        # Pre-truncate the log so each daemon run starts fresh — old
        # heartbeat logs are noise once the process restarts.
        try:
            self._log_path.write_text(
                f"# xm-auto-evo heartbeat started @ "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# repo={self._repo_path}\n"
                f"# workspace={self._workspace}\n"
                f"# interval={self._interval_min}min\n",
                encoding="utf-8",
            )
        except OSError:
            pass

        env = os.environ.copy()
        env["WORKSPACE"] = str(self._workspace)
        # B-18 unification: route xm-auto-evo's MEMORY.md / PROFILE.md
        # reads + writes to the XMclaw-canonical persona files. Without
        # these env vars the JS side writes a parallel MEMORY.md inside
        # ~/.xmclaw/auto_evo/ that the agent's system-prompt assembler
        # never sees — closed-loop breaks.
        try:
            from xmclaw.utils.paths import persona_dir as _persona_dir
            _persona_root = _persona_dir().parent / "profiles" / "default"
            _persona_root.mkdir(parents=True, exist_ok=True)
            env.setdefault("XMC_MEMORY_PATH", str(_persona_root / "MEMORY.md"))
            env.setdefault("XMC_PROFILE_PATH", str(_persona_root / "USER.md"))
        except Exception:  # noqa: BLE001
            pass

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | 0x08000000  # CREATE_NO_WINDOW
            )

        try:
            log_fp = self._log_path.open("ab")
            self._proc = subprocess.Popen(  # noqa: S603
                ["node", str(index_js), "heartbeat"],
                cwd=str(self._repo_path),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=creationflags,
            )
        except (OSError, FileNotFoundError) as exc:
            return {"ok": False, "error": f"spawn failed: {exc}"}

        return {
            "ok": True,
            "running": True,
            "pid": self._proc.pid,
            "log_path": str(self._log_path),
            "workspace": str(self._workspace),
        }

    async def stop(self) -> dict[str, Any]:
        if self._proc is None:
            return {"ok": True, "running": False, "noop": True}
        if self._proc.poll() is not None:
            self._proc = None
            return {"ok": True, "running": False, "exited": True}
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except OSError as exc:
            return {"ok": False, "error": f"stop failed: {exc}"}
        self._proc = None
        return {"ok": True, "running": False, "stopped": True}

    async def run_once(
        self, command: str = "start",
    ) -> dict[str, Any]:
        """Fire one ``node index.js <command>`` cycle synchronously
        (observe / learn / evolve / start). Used by the REST 'run now'
        button without disturbing the heartbeat process."""
        index_js = self._repo_path / "index.js"
        if not index_js.is_file():
            return {"ok": False, "error": f"not found: {index_js}"}

        env = os.environ.copy()
        env["WORKSPACE"] = str(self._workspace)
        # B-18 unification: route xm-auto-evo's MEMORY.md / PROFILE.md
        # reads + writes to the XMclaw-canonical persona files. Without
        # these env vars the JS side writes a parallel MEMORY.md inside
        # ~/.xmclaw/auto_evo/ that the agent's system-prompt assembler
        # never sees — closed-loop breaks.
        try:
            from xmclaw.utils.paths import persona_dir as _persona_dir
            _persona_root = _persona_dir().parent / "profiles" / "default"
            _persona_root.mkdir(parents=True, exist_ok=True)
            env.setdefault("XMC_MEMORY_PATH", str(_persona_root / "MEMORY.md"))
            env.setdefault("XMC_PROFILE_PATH", str(_persona_root / "USER.md"))
        except Exception:  # noqa: BLE001
            pass

        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        try:
            proc = await asyncio.create_subprocess_exec(
                "node", str(index_js), command,
                cwd=str(self._repo_path),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=creationflags,
            )
        except (OSError, FileNotFoundError) as exc:
            return {"ok": False, "error": f"spawn failed: {exc}"}

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=120.0,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": "timeout (120s)"}

        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": (stdout or b"").decode("utf-8", "replace")[-8000:],
        }
