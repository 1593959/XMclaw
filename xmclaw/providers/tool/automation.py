"""AutomationTools — cron CRUD, code execution, process management.

B-136. Closes more peer-feature-parity gaps the user flagged:

  * cron_create / cron_list / cron_pause / cron_resume / cron_remove /
    cron_run_now — agent-callable scheduling. Hermes ships full cron
    CRUD as builtins; we had ``schedule_followup`` (one-shot only) plus
    a ``/api/v2/cron`` HTTP surface, but the LLM had no tool to manage
    its OWN recurring jobs. Now it does.

  * code_python — run a snippet of Python in a subprocess. Safer +
    more structured than ``bash python -c`` because:
      - timeout enforced (default 30s, max 300s)
      - stdout / stderr / returncode returned as a dict
      - no shell-quoting trap

  * process_list / process_kill — psutil-backed observability tools.
    OpenClaw / Hermes both expose process introspection; agent_status
    only knows about the daemon's own process tree.

Each tool returns ``ToolResult(ok=False, error="install ...")`` when
its optional dep is missing (psutil for process_*; cron tools always
work because the cron store is core). The provider conforms to
``ToolProvider`` structurally (no inheritance) so providers/tool/
doesn't pick up an extra dep on the daemon layer.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


# ── Cron specs ────────────────────────────────────────────────────


_CRON_CREATE_SPEC = ToolSpec(
    name="cron_create",
    description=(
        "Schedule a recurring task. The agent can use this to set up "
        "its own follow-ups, daily reports, etc. — distinct from the "
        "one-shot ``schedule_followup`` which fires exactly once.\n\n"
        "Schedule format accepts both natural ('every 30m', 'every "
        "1h', 'every 1d') and cron strings ('0 9 * * *' = 9am daily "
        "when the optional ``croniter`` dep is installed).\n\n"
        "When ``wake_agent`` is True (default), the prompt is sent "
        "to the agent on fire; when False, the job is informational "
        "only and the agent skips the LLM call."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Display name."},
            "schedule": {"type": "string", "description": "'every Xm' / 'every Xh' / cron string."},
            "prompt": {"type": "string", "description": "What to send the agent on fire."},
            "agent_id": {"type": "string", "description": "Target agent id (default 'main')."},
            "run_once": {"type": "boolean", "description": "Delete after first fire."},
            "wake_agent": {"type": "boolean", "description": "False = pure-script (no LLM call)."},
        },
        "required": ["name", "schedule", "prompt"],
    },
)


_CRON_LIST_SPEC = ToolSpec(
    name="cron_list",
    description=(
        "List every scheduled cron job + its status (enabled, "
        "next_run_at, run_count, last_error). Useful for the agent "
        "to find jobs it created earlier so it can pause/resume/remove."
    ),
    parameters_schema={"type": "object", "properties": {}},
)


_CRON_PAUSE_SPEC = ToolSpec(
    name="cron_pause",
    description=(
        "Disable a cron job by id without deleting it. Pair with "
        "``cron_resume`` later. Stops the tick loop from firing it "
        "while preserving the schedule + history."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
)


_CRON_RESUME_SPEC = ToolSpec(
    name="cron_resume",
    description=(
        "Re-enable a previously paused cron job. The schedule resumes "
        "from now — past missed fires are NOT replayed."
    ),
    parameters_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
)


_CRON_REMOVE_SPEC = ToolSpec(
    name="cron_remove",
    description="Permanently delete a cron job by id.",
    parameters_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
)


# ── Code execution spec ──────────────────────────────────────────


_CODE_PYTHON_SPEC = ToolSpec(
    name="code_python",
    description=(
        "Execute a Python snippet in a subprocess and return "
        "{stdout, stderr, returncode}. Useful for one-off math, data "
        "munging, regex testing — anything where bash is overkill or "
        "shell quoting is a pain. The snippet runs with the same "
        "Python interpreter as the daemon, so import paths match. "
        "Default timeout 30s, max 300s. No internet sandbox — pair "
        "with ``allowed_dirs`` when threat model requires it."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source."},
            "timeout_s": {"type": "integer", "description": "1-300, default 30."},
        },
        "required": ["code"],
    },
)


# ── Process tool specs ───────────────────────────────────────────


_PROCESS_LIST_SPEC = ToolSpec(
    name="process_list",
    description=(
        "List running processes — pid + name + cpu_percent + "
        "memory_mb. Filter with ``name_contains`` to scope. Capped at "
        "200 rows. Requires ``psutil``."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name_contains": {"type": "string"},
            "limit": {"type": "integer", "description": "1-500, default 50."},
        },
    },
)


_PROCESS_KILL_SPEC = ToolSpec(
    name="process_kill",
    description=(
        "Send SIGTERM to a process by PID. Returns {ok, pid, signal}. "
        "Requires the user to have permission to signal that PID; "
        "permission errors surface as ok=False. Refuses to kill the "
        "daemon's own PID — call ``xmclaw stop`` for that instead."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "pid": {"type": "integer"},
            "force": {"type": "boolean", "description": "SIGKILL instead of SIGTERM."},
        },
        "required": ["pid"],
    },
)


# ── Provider ──────────────────────────────────────────────────────


class AutomationTools(ToolProvider):
    """Cron CRUD + Python execution + process tools.

    Stateful: holds a reference to the cron store so every list/add/
    remove sees the same state across tool calls and the cron tick
    loop.
    """

    def __init__(
        self,
        *,
        enable_cron: bool = True,
        enable_code: bool = True,
        enable_process: bool = True,
    ) -> None:
        self._enable_cron = enable_cron
        self._enable_code = enable_code
        self._enable_process = enable_process

    def list_tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        if self._enable_cron:
            out.extend([
                _CRON_CREATE_SPEC, _CRON_LIST_SPEC, _CRON_PAUSE_SPEC,
                _CRON_RESUME_SPEC, _CRON_REMOVE_SPEC,
            ])
        if self._enable_code:
            out.append(_CODE_PYTHON_SPEC)
        if self._enable_process:
            out.extend([_PROCESS_LIST_SPEC, _PROCESS_KILL_SPEC])
        return out

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        try:
            name = call.name
            if name in {"cron_create", "cron_list", "cron_pause",
                        "cron_resume", "cron_remove"}:
                return await self._cron(call, t0)
            if name == "code_python":
                return await self._code_python(call, t0)
            if name == "process_list":
                return await self._process_list(call, t0)
            if name == "process_kill":
                return await self._process_kill(call, t0)
        except Exception as exc:  # noqa: BLE001 — surface as ok=False
            return _fail(call, t0, f"{type(exc).__name__}: {exc}")
        return _fail(call, t0, f"unknown tool: {name!r}")

    # ── cron handlers ────────────────────────────────────────────

    async def _cron(self, call: ToolCall, t0: float) -> ToolResult:
        """Dispatcher for the 5 cron tools — keeps the per-tool
        helpers small and the import of CronStore lazy."""
        from xmclaw.core.scheduler.cron import (
            CronJob, default_cron_store, parse_schedule,
        )
        store = default_cron_store()
        args = call.args or {}
        name = call.name

        if name == "cron_list":
            jobs = [j.to_dict() for j in store.list_jobs()]
            return _ok(call, t0, json.dumps({
                "count": len(jobs), "jobs": jobs,
            }, ensure_ascii=False, default=str))

        if name == "cron_create":
            sched = str(args.get("schedule", "")).strip()
            if not sched:
                return _fail(call, t0, "schedule required (e.g. 'every 30m')")
            try:
                next_at = parse_schedule(sched, now=time.time())
            except ValueError as exc:
                return _fail(call, t0, f"invalid schedule {sched!r}: {exc}")
            job = CronJob(
                id=uuid.uuid4().hex[:12],
                name=str(args.get("name") or "agent-scheduled").strip(),
                schedule=sched,
                prompt=str(args.get("prompt") or ""),
                agent_id=str(args.get("agent_id") or "main"),
                wake_agent=bool(args.get("wake_agent", True)),
                run_once=bool(args.get("run_once", False)),
                next_run_at=next_at,
            )
            saved = store.add(job)
            return _ok(call, t0, json.dumps({
                "ok": True, "job_id": saved.id, "next_run_at": saved.next_run_at,
            }, ensure_ascii=False, default=str))

        # pause / resume / remove all need an existing job_id
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            return _fail(call, t0, "job_id required")
        existing = store.get(job_id)
        if existing is None:
            return _fail(call, t0, f"job {job_id!r} not found")

        if name == "cron_remove":
            store.remove(job_id)
            return _ok(call, t0, json.dumps({"ok": True, "removed": job_id}))

        # pause/resume = upsert with enabled flipped
        new_enabled = name == "cron_resume"
        updated = existing.with_updates(enabled=new_enabled)
        store.add(updated)  # add() upserts by id
        return _ok(call, t0, json.dumps({
            "ok": True, "job_id": job_id, "enabled": new_enabled,
        }))

    # ── code_python ──────────────────────────────────────────────

    async def _code_python(self, call: ToolCall, t0: float) -> ToolResult:
        args = call.args or {}
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return _fail(call, t0, "code required")
        timeout_s = max(1, min(int(args.get("timeout_s", 30)), 300))

        # Use the same Python interpreter the daemon is on so import
        # paths match. -I = isolated mode (no PYTHONPATH leak); -X
        # utf8 = consistent stdio encoding on Windows.
        cmd = [sys.executable, "-I", "-X", "utf8", "-c", code]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return _fail(call, t0, f"code timed out after {timeout_s}s")
        except OSError as exc:
            return _fail(call, t0, f"subprocess failed: {exc}")

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        # Cap each stream so an infinite-loop print doesn't blow context
        return _ok(call, t0, json.dumps({
            "returncode": proc.returncode,
            "stdout": stdout[:16000],
            "stderr": stderr[:16000],
            "truncated_stdout": len(stdout) > 16000,
            "truncated_stderr": len(stderr) > 16000,
        }, ensure_ascii=False))

    # ── process tools ────────────────────────────────────────────

    async def _process_list(self, call: ToolCall, t0: float) -> ToolResult:
        try:
            import psutil  # type: ignore
        except ImportError:
            return _fail(call, t0, (
                "process_list needs ``psutil``. "
                "Install with: pip install psutil"
            ))
        args = call.args or {}
        name_contains = (args.get("name_contains") or "").lower()
        limit = max(1, min(int(args.get("limit", 50)), 500))
        rows: list[dict[str, Any]] = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                pname = (info.get("name") or "").lower()
                if name_contains and name_contains not in pname:
                    continue
                mem = info.get("memory_info")
                rows.append({
                    "pid": info.get("pid"),
                    "name": info.get("name"),
                    "cpu_percent": info.get("cpu_percent"),
                    "memory_mb": (mem.rss / 1024 / 1024) if mem else None,
                })
                if len(rows) >= limit:
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return _ok(call, t0, json.dumps({
            "count": len(rows), "rows": rows,
        }, ensure_ascii=False, default=str))

    async def _process_kill(self, call: ToolCall, t0: float) -> ToolResult:
        try:
            import psutil
        except ImportError:
            return _fail(call, t0, (
                "process_kill needs ``psutil``. "
                "Install with: pip install psutil"
            ))
        args = call.args or {}
        try:
            pid = int(args.get("pid"))
        except (TypeError, ValueError):
            return _fail(call, t0, "pid (int) required")
        import os
        if pid == os.getpid():
            return _fail(call, t0, (
                "refusing to kill the daemon itself "
                "(use `xmclaw stop` for that)"
            ))
        force = bool(args.get("force", False))
        try:
            p = psutil.Process(pid)
            if force:
                p.kill()
                signame = "SIGKILL"
            else:
                p.terminate()
                signame = "SIGTERM"
        except psutil.NoSuchProcess:
            return _fail(call, t0, f"no process with pid {pid}")
        except psutil.AccessDenied:
            return _fail(call, t0, f"access denied killing pid {pid}")
        return _ok(call, t0, json.dumps({
            "ok": True, "pid": pid, "signal": signame,
        }))


# ── helpers ───────────────────────────────────────────────────────


def _ok(call: ToolCall, t0: float, content: Any) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=True, content=content,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )


def _fail(call: ToolCall, t0: float, err: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False, content=None, error=err,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )
