"""Per-session live workspace + git history + change-event publisher.

F1 (2026-05-30) — backs the chat-page right-side "WorkspacePanel". The
agent has no awareness of this manager; the manager passively listens for
``TOOL_INVOCATION_FINISHED`` events on the bus, inspects each result's
``expected_side_effects`` (resolved absolute paths the tool wrote / patched
/ deleted), filters those that landed under the session's workspace dir,
and:

  1. Lazily initialises a git repo under that workspace dir
     (``git init`` + an initial empty commit on first touch).
  2. Stages + commits the change with a message like
     ``agent: file_write README.md`` so each tool call produces one commit
     the UI's "改动" tab can render as a timeline + per-commit diff.
  3. Republishes a :class:`WORKSPACE_FILE_CHANGED` event with structured
     ``{path, rel_path, action, tool, commit_sha, summary, bytes}`` so the
     UI doesn't have to re-derive which side_effect was the workspace one.

Design choices worth knowing:

* **Git is optional, never blocking.** If ``git`` isn't on PATH, the
  manager still publishes the change event with ``commit_sha=""``. The UI
  degrades to a no-diff timeline; nothing crashes.
* **All git work is dispatched to a thread** via ``asyncio.to_thread`` —
  ``subprocess.run`` would block the event loop and we're on the
  publish-handler hot path.
* **Action inference is cheap.** We don't try to re-read the file to tell
  ``modified`` from ``created`` precisely; instead the tool name + a
  pre-call existence cache is enough. The UI just needs the badge color
  right.

This module owns *only* the side-effect plumbing. HTTP surface lives in
:mod:`xmclaw.daemon.routers.workspaces`; UI rendering lives in the
``WorkspacePanel`` JS component.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from xmclaw.core.bus.events import (
    BehavioralEvent,
    EventType,
    make_event,
)
from xmclaw.utils.log import get_logger
from xmclaw.utils.paths import (
    session_workspace_dir,
    session_workspaces_root,
)

_log = get_logger(__name__)


# Tool names whose results we care about. Anything outside this set is
# ignored even if ``side_effects`` carries a workspace-internal path —
# spurious matches (a search tool reporting a "touched" path it only
# read) would pollute the timeline with non-changes.
_WRITE_TOOLS: frozenset[str] = frozenset({
    "file_write",
    "apply_patch",
    "file_delete",
    "file_create",
    "file_edit",
    # Plugin / MCP tools using the conventional names also flow through.
    "create_file",
    "edit_file",
})


def _git_available() -> bool:
    """Cheap once-per-process check that ``git`` is callable."""
    return shutil.which("git") is not None


class WorkspaceManager:
    """Lifespan-owned subscriber + filesystem helper.

    One instance per daemon. Wired in :mod:`xmclaw.daemon.app_lifespan`
    after the bus exists. Holds no per-session state in memory beyond a
    set of ``session_id`` we've already git-init'd, so it scales to many
    sessions without bloat.
    """

    def __init__(self, *, bus: Any) -> None:
        self._bus = bus
        self._git_ok = _git_available()
        # Sessions whose git repo has been initialised in this process.
        # Persists across reads but not across restarts (cheap to redo).
        self._inited: set[str] = set()
        # Author identity for auto-commits — short-circuits the
        # "Please tell me who you are" git error on a fresh box.
        self._author_env = {
            "GIT_AUTHOR_NAME": "XMclaw agent",
            "GIT_AUTHOR_EMAIL": "agent@xmclaw.local",
            "GIT_COMMITTER_NAME": "XMclaw agent",
            "GIT_COMMITTER_EMAIL": "agent@xmclaw.local",
        }
        # Lock per session to keep two simultaneous tool finishes from
        # racing into ``git add`` at the same time on Windows (file
        # locks bite). asyncio.Lock is fine — git work is in a thread but
        # the awaiting coroutine serialises by lock.
        self._locks: dict[str, asyncio.Lock] = {}

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to the bus. Must be called once after bus + manager
        both exist (lifespan ordering)."""
        try:
            self._bus.subscribe(
                lambda e: e.type == EventType.TOOL_INVOCATION_FINISHED,
                self._on_tool_finished,
            )
            try:
                session_workspaces_root().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _log.warning(
                    "workspace_manager.root_mkdir_failed err=%s", exc,
                )
            _log.info(
                "workspace_manager.started git_available=%s",
                self._git_ok,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("workspace_manager.subscribe_failed err=%s", exc)

    # ── public helpers (used by router) ────────────────────────────

    def ensure_dir(self, session_id: str) -> Path:
        """Return the session's workspace dir, creating it if needed.

        Idempotent. Safe to call from the router even when the agent
        hasn't written anything yet — the UI may want to render an empty
        tree pre-emptively.
        """
        d = session_workspace_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_tree(self, session_id: str) -> list[dict[str, Any]]:
        """Return a flat list of ``{rel_path, kind, size, mtime}`` entries
        for everything under the session's workspace.

        Flat (not nested) because the UI builds its own tree from
        slash-separated paths — easier to diff against the previous
        snapshot when a ``workspace_file_changed`` event arrives.
        """
        root = session_workspace_dir(session_id)
        if not root.exists():
            return []
        out: list[dict[str, Any]] = []
        for p in root.rglob("*"):
            # Skip the git repo itself — it's machinery, not content.
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if rel.parts and rel.parts[0] == ".git":
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            out.append({
                "rel_path": rel.as_posix(),
                "kind": "dir" if p.is_dir() else "file",
                "size": stat.st_size if p.is_file() else 0,
                "mtime": stat.st_mtime,
            })
        out.sort(key=lambda e: e["rel_path"])
        return out

    def resolve_safe(self, session_id: str, rel_path: str) -> Path | None:
        """Containment-checked resolve of ``rel_path`` inside the session
        workspace. Returns ``None`` when the path escapes the workspace,
        can't be resolved, or doesn't point at an existing file. Used by
        the ``/raw`` route to serve images / PDFs / HTML with the right
        mime type without duplicating the escape check."""
        root = session_workspace_dir(session_id).resolve()
        try:
            target = (root / rel_path).resolve()
        except OSError:
            return None
        try:
            target.relative_to(root)
        except ValueError:
            return None
        if not target.is_file():
            return None
        return target

    def read_file(
        self, session_id: str, rel_path: str, max_bytes: int = 1024 * 1024,
    ) -> dict[str, Any]:
        """Read ``rel_path`` from the session workspace with containment
        check + size cap. Returns ``{ok, content?, error?, bytes,
        truncated, kind}``."""
        root = session_workspace_dir(session_id).resolve()
        try:
            target = (root / rel_path).resolve()
        except OSError as exc:
            return {"ok": False, "error": f"resolve_failed: {exc}"}
        try:
            target.relative_to(root)
        except ValueError:
            return {"ok": False, "error": "path_escapes_workspace"}
        if not target.exists():
            return {"ok": False, "error": "not_found"}
        if not target.is_file():
            return {"ok": False, "error": "not_a_file"}
        try:
            data = target.read_bytes()
        except OSError as exc:
            return {"ok": False, "error": f"read_failed: {exc}"}
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        # Best-effort text decode; UI handles binary fallback by mime.
        try:
            content = data.decode("utf-8")
            kind = "text"
        except UnicodeDecodeError:
            content = ""
            kind = "binary"
        return {
            "ok": True,
            "content": content,
            "bytes": target.stat().st_size,
            "truncated": truncated,
            "kind": kind,
        }

    async def list_commits(
        self, session_id: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent auto-commits as ``{sha, ts, subject, files}``."""
        if not self._git_ok:
            return []
        root = session_workspace_dir(session_id)
        if not (root / ".git").exists():
            return []
        try:
            out = await asyncio.to_thread(
                self._run_git,
                root,
                ["log", f"-n{int(limit)}", "--name-only",
                 "--pretty=format:%H%x09%ct%x09%s"],
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("workspace.log_failed sid=%s err=%s", session_id, exc)
            return []
        commits: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for line in (out or "").splitlines():
            if not line.strip():
                if cur is not None:
                    commits.append(cur)
                    cur = None
                continue
            if "\t" in line and (cur is None or cur.get("subject") is not None
                                  and (cur.get("files") or [])):
                # New commit header.
                if cur is not None:
                    commits.append(cur)
                parts = line.split("\t", 2)
                cur = {
                    "sha": parts[0],
                    "ts": float(parts[1]) if len(parts) > 1 else 0.0,
                    "subject": parts[2] if len(parts) > 2 else "",
                    "files": [],
                }
            elif cur is not None:
                cur["files"].append(line)
        if cur is not None:
            commits.append(cur)
        return commits

    async def commit_diff(
        self, session_id: str, sha: str,
    ) -> dict[str, Any]:
        """Return ``{ok, diff}`` for a given commit. The diff is the raw
        unified output; the UI parses + colours it."""
        if not self._git_ok:
            return {"ok": False, "error": "git_unavailable"}
        # Defensive: SHA must be hex, no shell tricks.
        if not all(c in "0123456789abcdefABCDEF" for c in sha) or len(sha) < 4:
            return {"ok": False, "error": "bad_sha"}
        root = session_workspace_dir(session_id)
        if not (root / ".git").exists():
            return {"ok": False, "error": "no_repo"}
        try:
            diff = await asyncio.to_thread(
                self._run_git, root, ["show", "--format=", sha],
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"show_failed: {exc}"}
        return {"ok": True, "diff": diff or ""}

    # ── event handler ─────────────────────────────────────────────

    async def _on_tool_finished(self, event: BehavioralEvent) -> None:
        payload = event.payload or {}
        tool_name = payload.get("name") or ""
        if tool_name not in _WRITE_TOOLS:
            return
        side_effects = payload.get("expected_side_effects") or []
        if not isinstance(side_effects, list) or not side_effects:
            return
        session_id = event.session_id or ""
        if not session_id:
            return
        root = session_workspace_dir(session_id).resolve()
        # Find paths under this session's workspace. We compare resolved
        # forms so symlinked tmp dirs / case-insensitive Windows paths
        # don't fool the containment check.
        try:
            root_str = str(root)
        except Exception:  # noqa: BLE001
            return
        hits: list[Path] = []
        for raw in side_effects:
            if not isinstance(raw, str) or not raw:
                continue
            try:
                p = Path(raw).resolve()
            except OSError:
                continue
            # On Windows, ``str(p).startswith(root_str)`` is the most
            # robust containment check — ``Path.is_relative_to`` doesn't
            # honour case folding the same way the filesystem does, and
            # we already canonicalised both sides via ``resolve()``.
            try:
                ps = str(p)
            except Exception:  # noqa: BLE001
                continue
            if os.path.normcase(ps).startswith(
                os.path.normcase(root_str) + os.sep,
            ):
                hits.append(p)
        if not hits:
            return
        # Serialise per-session to keep two finishes from racing into
        # ``git add`` simultaneously.
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            for path in hits:
                try:
                    await self._handle_change(
                        session_id=session_id,
                        root=root,
                        path=path,
                        tool_name=tool_name,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "workspace.handle_failed sid=%s path=%s err=%s",
                        session_id, path, exc,
                    )

    async def _handle_change(
        self,
        *,
        session_id: str,
        root: Path,
        path: Path,
        tool_name: str,
    ) -> None:
        rel = path.relative_to(root).as_posix()
        # Pre-determine action — file may have just been deleted.
        if not path.exists():
            action = "deleted"
            bytes_size = 0
        else:
            try:
                bytes_size = path.stat().st_size
            except OSError:
                bytes_size = 0
            action = (
                "created" if tool_name in ("file_create", "create_file")
                else "modified"
            )

        # Git work — best-effort; skipped if git missing or repo init fails.
        commit_sha = ""
        if self._git_ok:
            try:
                await self._ensure_repo(session_id, root)
                commit_sha = await asyncio.to_thread(
                    self._commit_change,
                    root, rel, tool_name, action,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "workspace.commit_failed sid=%s rel=%s err=%s",
                    session_id, rel, exc,
                )

        # Republish the structured event the UI subscribes to.
        try:
            await self._bus.publish(make_event(
                session_id=session_id,
                agent_id="workspace_manager",
                type=EventType.WORKSPACE_FILE_CHANGED,
                payload={
                    "path": str(path),
                    "rel_path": rel,
                    "action": action,
                    "tool": tool_name,
                    "commit_sha": commit_sha,
                    "summary": f"{action} {rel}",
                    "bytes": bytes_size,
                },
            ))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "workspace.publish_failed sid=%s err=%s", session_id, exc,
            )

    # ── git plumbing ──────────────────────────────────────────────

    async def _ensure_repo(self, session_id: str, root: Path) -> None:
        if session_id in self._inited and (root / ".git").exists():
            return
        if (root / ".git").exists():
            self._inited.add(session_id)
            return
        # First-time init: git init + empty initial commit so subsequent
        # commits always have HEAD to diff against.
        try:
            root.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._run_git, root, ["init", "-q"])
            await asyncio.to_thread(
                self._run_git, root,
                ["commit", "--allow-empty", "-q", "-m",
                 "workspace: init"],
            )
            self._inited.add(session_id)
            _log.info("workspace.repo_init sid=%s", session_id)
        except Exception as exc:  # noqa: BLE001
            # init failure is non-fatal — caller will fall back to
            # commit_sha="".
            _log.warning(
                "workspace.repo_init_failed sid=%s err=%s",
                session_id, exc,
            )
            raise

    def _commit_change(
        self,
        root: Path,
        rel: str,
        tool_name: str,
        action: str,
    ) -> str:
        # ``git add -A`` so deletes register, then ``git commit``.
        self._run_git(root, ["add", "-A", "--", rel])
        # If there's nothing staged (e.g. write produced byte-identical
        # content), ``git commit`` exits non-zero — swallow that case.
        try:
            self._run_git(
                root,
                ["commit", "-q", "-m",
                 f"agent: {tool_name} {action} {rel}"],
            )
        except subprocess.CalledProcessError:
            return ""
        sha = self._run_git(root, ["rev-parse", "HEAD"]).strip()
        return sha

    def _run_git(self, cwd: Path, args: list[str]) -> str:
        env = dict(os.environ)
        env.update(self._author_env)
        # Quiet down hooks / signing / pager so we don't deadlock on
        # interactive prompts and don't have to handle pager output.
        env.setdefault("GIT_PAGER", "")
        env.setdefault("PAGER", "")
        env.setdefault("GIT_OPTIONAL_LOCKS", "0")
        result = subprocess.run(  # noqa: S603 — args are static + sha-validated
            ["git", *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            # text=True alone decodes with the locale codepage (GBK on
            # zh-CN Windows) and a UTF-8 diff kills the reader thread →
            # stdout=None → the UI gets an empty diff. Git output is
            # UTF-8; decode it as such and never throw mid-read.
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=10.0,
        )
        return result.stdout
