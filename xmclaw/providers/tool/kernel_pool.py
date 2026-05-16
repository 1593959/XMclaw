"""KernelPool — persistent IPython kernels per agent session.

Wave-27 fix-LAT2 (2026-05-16): the original ``code_python`` tool spawned
a fresh ``python -c <snippet>`` subprocess on every call. That meant
variables, imports, and in-memory data ALL died between calls, so the
LLM (Kimi / Claude) would write::

    code_python: df = pd.read_csv("orders.csv")     # 1: ok, df in proc-A memory
    code_python: df.groupby("user").sum()           # 2: NameError, proc-A is dead

…then go "变量丢了，让我把完整代码一次性写好" forever. Empirical: 5
``code_python`` calls in a row reciting the same "let me rewrite the
whole thing" comment because the model's Jupyter mental model didn't
match the subprocess execution model.

This module fixes that by running each session's snippets inside a
long-lived IPython kernel (via ``jupyter_client``). The kernel keeps
its namespace across calls so ``df`` from call N is still bound at
call N+1.

Per-session isolation: kernels are keyed by session_id. Two parallel
chats don't share a ``df`` — kernel #A for session-x, kernel #B for
session-y. The pool spins kernels lazily (first call creates one);
idle kernels are reaped after ``idle_timeout_s`` (default 30 min).

Backstop: if ``jupyter_client`` / ``ipykernel`` aren't installed, the
pool's ``execute`` raises ``KernelDepsMissing`` and the caller is
expected to fall through to the legacy subprocess path. The system
keeps working even on installations without the optional deps.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import time
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)


class KernelDepsMissing(RuntimeError):
    """Raised when ``jupyter_client`` / ``ipykernel`` aren't importable.

    Caller should treat as "fallback to subprocess one-shot" rather than
    surfacing as a hard error — the optional deps are intentionally
    optional.
    """


def _check_deps() -> None:
    """Eager import probe used by callers + tests."""
    try:
        import jupyter_client  # noqa: F401
        import ipykernel  # noqa: F401
    except ImportError as exc:
        raise KernelDepsMissing(
            "code_python persistent-kernel mode needs "
            "`jupyter_client` + `ipykernel`. Install with: "
            "pip install jupyter_client ipykernel"
        ) from exc


@dataclass
class _KernelEntry:
    """One per-session kernel + the bookkeeping the pool needs."""

    session_id: str
    manager: Any  # jupyter_client.KernelManager
    client: Any   # jupyter_client.BlockingKernelClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    # Sticky failure state: set if the kernel died and re-start failed.
    # Until cleared by ``reset_session``, calls return a structured error
    # instead of busy-looping the kernel-start failure.
    dead: bool = False


@dataclass
class ExecutionResult:
    """Structured result of one ``execute`` call.

    Mirrors the shape the old subprocess path returned (stdout / stderr
    / returncode) so callers don't branch on which backend ran the
    code. ``returncode`` is synthesised from kernel status:
        0  = ok
        1  = Python exception during execution
        137 = kernel died (or timeout-kill)
    """

    stdout: str
    stderr: str
    returncode: int
    truncated_stdout: bool = False
    truncated_stderr: bool = False
    # Optional: last expression value, when the snippet ended in an
    # expression (Jupyter's "Out[N]"). Empty for statement-only code.
    result_repr: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated_stdout": self.truncated_stdout,
            "truncated_stderr": self.truncated_stderr,
        }
        if self.result_repr:
            out["result"] = self.result_repr
        return out


class KernelPool:
    """One persistent IPython kernel per session_id.

    Thread-safety: the pool is owned by the event loop. Each session's
    kernel has its own ``asyncio.Lock`` so concurrent calls into the
    SAME session serialise (ipykernel's blocking client isn't reentrant),
    while calls across different sessions run in parallel.

    Lifecycle:
        - First ``execute(session_id, code)`` lazily spawns the kernel.
        - ``reset_session(session_id)`` kills + drops the kernel for that
          session (used by ``/restart`` / ``%reset`` style requests).
        - ``shutdown_all()`` kills every kernel — called by app_lifespan
          on daemon shutdown so we don't leak python.exe children.
        - ``reap_idle()`` (optional) kills kernels not touched in N
          seconds. Run by the daemon's background tick if wanted.
    """

    def __init__(
        self,
        *,
        idle_timeout_s: float = 1800.0,
        max_kernels: int = 16,
    ) -> None:
        self._entries: dict[str, _KernelEntry] = {}
        self._global_lock = asyncio.Lock()
        self._idle_timeout_s = idle_timeout_s
        self._max_kernels = max_kernels

    async def execute(
        self,
        session_id: str,
        code: str,
        *,
        timeout_s: float = 30.0,
        stdout_cap: int = 16_000,
        stderr_cap: int = 16_000,
    ) -> ExecutionResult:
        """Run ``code`` inside the kernel bound to ``session_id``.

        Raises ``KernelDepsMissing`` when the optional deps aren't
        installed — caller is expected to fall back to subprocess.

        Returns ``ExecutionResult`` for both success and Python-level
        errors. Only real infrastructure problems (kernel died, can't
        start) raise.
        """
        _check_deps()
        entry = await self._get_or_create(session_id)
        async with entry.lock:
            return await self._execute_locked(
                entry, code,
                timeout_s=timeout_s,
                stdout_cap=stdout_cap,
                stderr_cap=stderr_cap,
            )

    async def reset_session(self, session_id: str) -> bool:
        """Kill the kernel for ``session_id`` (next call spins a new one).

        Returns True if a kernel was actually killed, False if none was
        bound. Used by ``%reset`` / ``/restart`` style requests when
        the user wants a clean slate.
        """
        async with self._global_lock:
            entry = self._entries.pop(session_id, None)
        if entry is None:
            return False
        await self._kill_entry(entry)
        return True

    async def shutdown_all(self) -> None:
        """Kill every kernel in the pool. Called on daemon shutdown.

        Best-effort: per-kernel kill errors are logged + swallowed so
        one bad kernel can't block shutdown of the rest.
        """
        async with self._global_lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            try:
                await self._kill_entry(entry)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "kernel_pool.shutdown_failed sid=%s err=%s",
                    entry.session_id[:24], exc,
                )

    async def reap_idle(self) -> int:
        """Kill kernels untouched for > ``idle_timeout_s``. Returns count."""
        now = time.monotonic()
        async with self._global_lock:
            doomed = [
                e for e in self._entries.values()
                if (now - e.last_used) > self._idle_timeout_s
            ]
            for e in doomed:
                self._entries.pop(e.session_id, None)
        for e in doomed:
            try:
                await self._kill_entry(e)
            except Exception:  # noqa: BLE001
                pass
        return len(doomed)

    def session_ids(self) -> list[str]:
        """Snapshot of session_ids with live kernels — for /status pages."""
        return list(self._entries.keys())

    # ── internals ────────────────────────────────────────────────────

    async def _get_or_create(self, session_id: str) -> _KernelEntry:
        async with self._global_lock:
            entry = self._entries.get(session_id)
            if entry is not None and not entry.dead:
                entry.last_used = time.monotonic()
                return entry
            # Evict oldest if over cap (LRU by last_used).
            if len(self._entries) >= self._max_kernels:
                victim_sid = min(
                    self._entries,
                    key=lambda sid: self._entries[sid].last_used,
                )
                victim = self._entries.pop(victim_sid)
                # Drop the lock for the kill itself — _kill_entry blocks.
                asyncio.create_task(self._kill_entry(victim))
            entry = await self._spawn(session_id)
            self._entries[session_id] = entry
            return entry

    async def _spawn(self, session_id: str) -> _KernelEntry:
        """Synchronous-via-to_thread: start a kernel + wait for it ready."""
        def _start() -> tuple[Any, Any]:
            from jupyter_client import KernelManager
            # PYTHONUTF8=1 → consistent stdio encoding on Windows.
            # PYTHONIOENCODING=utf-8 → belt-and-braces. Both are read
            # by the kernel's Python interpreter on startup. We do NOT
            # pass ``-X utf8`` via extra_arguments because that goes
            # to IPKernelApp's argparse (which treats ``-X`` as a
            # config-option alias, not a Python flag) and kills the
            # kernel before it can register.
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            km = KernelManager()
            km.start_kernel(env=env)
            kc = km.client()
            kc.start_channels()
            # Block until kernel signals ready. 30s ceiling to keep a
            # stuck start from hanging the daemon.
            kc.wait_for_ready(timeout=30)
            return km, kc

        try:
            km, kc = await asyncio.to_thread(_start)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "kernel_pool.start_failed sid=%s err=%s: %r",
                session_id[:24], type(exc).__name__, exc,
            )
            raise
        _log.info(
            "kernel_pool.started sid=%s kernel_id=%s",
            session_id[:24], getattr(km, "kernel_id", "?"),
        )
        return _KernelEntry(
            session_id=session_id, manager=km, client=kc,
        )

    async def _kill_entry(self, entry: _KernelEntry) -> None:
        """Shut down the kernel + close ZMQ channels. Sync, off-loop."""
        def _kill() -> None:
            with contextlib.suppress(Exception):
                entry.client.stop_channels()
            with contextlib.suppress(Exception):
                entry.manager.shutdown_kernel(now=True)

        await asyncio.to_thread(_kill)
        _log.info(
            "kernel_pool.killed sid=%s",
            entry.session_id[:24],
        )

    async def _execute_locked(
        self,
        entry: _KernelEntry,
        code: str,
        *,
        timeout_s: float,
        stdout_cap: int,
        stderr_cap: int,
    ) -> ExecutionResult:
        """The actual execute — runs with entry.lock held.

        Drains the kernel's iopub + shell channels until we get a
        terminal ``status: idle`` on iopub. Wall-clock-bound by
        ``timeout_s``; on timeout we send ``interrupt`` (kernel-level
        equivalent of Ctrl+C) and surface a structured error.
        """
        kc = entry.client

        def _do_execute() -> ExecutionResult:
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []
            result_repr = ""
            had_error = False

            # Drain any leftover iopub messages from prior turns so we
            # don't mis-attribute them to this execute. Best-effort.
            while True:
                try:
                    kc.get_iopub_msg(timeout=0)
                except queue.Empty:
                    break
                except Exception:  # noqa: BLE001
                    break
            # Also drain any leftover shell replies (e.g. from a prior
            # interrupted call that was being cleaned up when we sent
            # the new request).
            while True:
                try:
                    kc.get_shell_msg(timeout=0)
                except queue.Empty:
                    break
                except Exception:  # noqa: BLE001
                    break

            # When the kernel was just interrupted, ipykernel briefly
            # enters an ``aborting`` state and silently skips any
            # execute request it receives in that window (emits only
            # status:busy + status:idle, no execute_input). We retry
            # up to N times if the shell-channel reply comes back
            # with status='aborted'.
            msg_id = ""
            for _retry in range(4):
                msg_id = kc.execute(
                    code, silent=False, store_history=True,
                )
                # Pull the shell reply (kernel's per-execute ACK) up to 5s.
                shell_reply = None
                try:
                    shell_reply = kc.get_shell_msg(timeout=5.0)
                except queue.Empty:
                    shell_reply = None
                except Exception:  # noqa: BLE001
                    shell_reply = None
                if shell_reply is None:
                    break  # no reply — fall through to iopub drain
                status = (shell_reply.get("content") or {}).get("status")
                if status != "aborted":
                    break  # ok / error → proceed with iopub drain
                # Aborted — kernel still settling from prior interrupt.
                # Brief wait then retry.
                time.sleep(0.2)
            else:
                # All retries returned aborted — surface as error.
                return ExecutionResult(
                    stdout="",
                    stderr=(
                        "[kernel: request kept being aborted; kernel may "
                        "be stuck. Try ``reset=True`` for a clean kernel.]"
                    ),
                    returncode=137,
                )
            deadline = time.monotonic() + timeout_s
            # Drain iopub until we see the matching idle status.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Interrupt + surface timeout. Drain the kernel's
                    # post-interrupt messages (KeyboardInterrupt error
                    # + final idle status for THIS msg_id) before
                    # returning, so the next execute() doesn't fight
                    # leftover iopub traffic and lose its own stream
                    # messages in the noise.
                    with contextlib.suppress(Exception):
                        entry.manager.interrupt_kernel()
                    stderr_parts.append(
                        f"\n[kernel: execution exceeded {timeout_s}s — interrupted]"
                    )
                    drain_deadline = time.monotonic() + 5.0
                    while time.monotonic() < drain_deadline:
                        try:
                            tail = kc.get_iopub_msg(timeout=0.5)
                        except queue.Empty:
                            continue
                        except Exception:  # noqa: BLE001
                            break
                        if (tail.get("parent_header") or {}).get("msg_id") != msg_id:
                            continue
                        if (
                            tail.get("msg_type") == "status"
                            and (tail.get("content") or {}).get(
                                "execution_state"
                            ) == "idle"
                        ):
                            break
                    return ExecutionResult(
                        stdout="".join(stdout_parts)[:stdout_cap],
                        stderr="".join(stderr_parts)[:stderr_cap],
                        returncode=124,
                        truncated_stdout=sum(len(p) for p in stdout_parts) > stdout_cap,
                        truncated_stderr=sum(len(p) for p in stderr_parts) > stderr_cap,
                    )
                try:
                    msg = kc.get_iopub_msg(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                except Exception as exc:  # noqa: BLE001
                    stderr_parts.append(
                        f"\n[kernel: iopub error {type(exc).__name__}: {exc}]"
                    )
                    return ExecutionResult(
                        stdout="".join(stdout_parts)[:stdout_cap],
                        stderr="".join(stderr_parts)[:stderr_cap],
                        returncode=137,
                    )
                parent_id = (msg.get("parent_header") or {}).get("msg_id")
                if parent_id != msg_id:
                    # A leftover message from a previous execute that
                    # we didn't fully drain; ignore.
                    continue
                msg_type = msg.get("msg_type", "")
                content = msg.get("content") or {}
                if msg_type == "stream":
                    name = content.get("name", "stdout")
                    text = content.get("text", "")
                    if name == "stderr":
                        stderr_parts.append(text)
                    else:
                        stdout_parts.append(text)
                elif msg_type == "error":
                    had_error = True
                    tb = content.get("traceback") or []
                    # ipykernel returns ANSI-colored traceback lines;
                    # strip the escape codes so they don't pollute the
                    # tool result rendering in the chat UI.
                    import re as _re
                    ansi_re = _re.compile(r"\x1b\[[0-9;]*[mGKHF]")
                    stripped = [ansi_re.sub("", line) for line in tb]
                    stderr_parts.append("\n".join(stripped))
                elif msg_type in ("execute_result", "display_data"):
                    data = content.get("data") or {}
                    text = data.get("text/plain", "")
                    if text and msg_type == "execute_result":
                        result_repr = str(text)
                    elif text:
                        # display_data (e.g. plot repr) → stdout
                        stdout_parts.append(str(text) + "\n")
                elif msg_type == "status":
                    if content.get("execution_state") == "idle":
                        break

            stdout = "".join(stdout_parts)
            stderr = "".join(stderr_parts)
            return ExecutionResult(
                stdout=stdout[:stdout_cap],
                stderr=stderr[:stderr_cap],
                returncode=1 if had_error else 0,
                truncated_stdout=len(stdout) > stdout_cap,
                truncated_stderr=len(stderr) > stderr_cap,
                result_repr=result_repr[:2000],
            )

        try:
            result = await asyncio.to_thread(_do_execute)
        except Exception as exc:  # noqa: BLE001 — kernel died
            _log.warning(
                "kernel_pool.execute_failed sid=%s err=%s",
                entry.session_id[:24], exc,
            )
            entry.dead = True
            raise
        entry.last_used = time.monotonic()
        return result


# Module-level singleton — owned by app_lifespan, accessed by the tool.
_DEFAULT_POOL: KernelPool | None = None


def default_pool() -> KernelPool | None:
    return _DEFAULT_POOL


def set_default_pool(pool: KernelPool | None) -> None:
    """Wire the pool. Called from app_lifespan on startup with a fresh
    pool; called again with ``None`` on shutdown after ``shutdown_all``
    finishes so subsequent ``default_pool()`` calls return None and the
    code_python tool falls back to subprocess.
    """
    global _DEFAULT_POOL
    _DEFAULT_POOL = pool


__all__ = [
    "ExecutionResult",
    "KernelDepsMissing",
    "KernelPool",
    "default_pool",
    "set_default_pool",
]
