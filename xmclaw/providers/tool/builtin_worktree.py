from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool._helpers import _fail as _fail

# B-94: process-wide memo of the workspace path each currently-active
# worktree was originally entered from.
_WORKTREE_ORIGIN: dict[str, Path] = {}


class BuiltinToolsWorktreeMixin:
    """Worktree tools: enter_worktree, exit_worktree."""

    async def _enter_worktree(self, call: ToolCall, t0: float) -> ToolResult:
        """B-94 + B-235: create ``.xmworktrees/<name>/`` + new branch and
        switch the daemon's primary workspace into it.

        Refuses when:
          * not inside a git repo (``git rev-parse`` fails)
          * already inside a worktree (path under .xmworktrees/ OR the
            legacy .claude/worktrees/ — both checked for back-compat)
        Both check messages tell the agent what to do next.

        B-235 path migration: pre-B-235 worktrees lived under
        ``.claude/worktrees/<name>/`` — Claude Code's project-level
        namespace. ``enter_worktree`` now writes to ``.xmworktrees/``
        instead so XMclaw stays out of other agents' territory.
        ``exit_worktree`` accepts both paths for back-compat — users
        with in-flight ``.claude/worktrees/`` worktrees can still wind
        them down without the daemon refusing.
        """
        from xmclaw.core.workspace import WorkspaceManager

        # 1. Resolve current primary root.
        wm = WorkspaceManager()
        state = wm.get()
        if state.primary is None:
            return _fail(
                call, t0,
                "no primary workspace — register one with the "
                "WorkspaceManager first (or call from a daemon that "
                "auto-loaded a project root)",
            )
        original_root = state.primary.path
        # Reject if already in a worktree — nesting just creates
        # confusion and the cleanup path can't tell what to undo.
        # B-235: detect both new (.xmworktrees) AND legacy
        # (.claude/worktrees) layouts so the "already in a worktree"
        # guard still fires for users still inside a pre-B-235 worktree.
        _root_parts = original_root.parts
        _in_xm_worktree = (
            "xmworktrees" in _root_parts
            and any(
                p == ".xmworktrees" for p in _root_parts
            )
        )
        _in_legacy_worktree = (
            ".claude" in _root_parts and "worktrees" in _root_parts
        )
        if _in_xm_worktree or _in_legacy_worktree:
            return _fail(
                call, t0,
                "already inside a worktree — call ``exit_worktree`` "
                "first if you want to swap into a new one",
            )

        # 2. Confirm the original root is a git repo.
        try:
            check = subprocess.run(
                ["git", "-C", str(original_root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return _fail(call, t0, f"git unavailable: {exc}")
        if check.returncode != 0 or check.stdout.strip() != "true":
            return _fail(
                call, t0,
                f"{original_root} is not a git repository — "
                "worktrees only work inside git repos",
            )

        # 3. Pick worktree name + branch. Strip slashes etc — git rejects
        # weird tokens but a clean name keeps the dir layout neat.
        raw_name = str(call.args.get("name") or "").strip()
        raw_name = re.sub(r"[^a-zA-Z0-9._-]", "-", raw_name).strip("-._")
        if not raw_name:
            # Random adjective-noun: stable enough for humans to type
            # without relying on a wordlist file.
            import random as _rnd
            adjectives = ("quick", "calm", "spicy", "bold", "nimble", "still")
            nouns = ("otter", "panda", "ember", "river", "forge", "pebble")
            raw_name = (
                f"{_rnd.choice(adjectives)}-{_rnd.choice(nouns)}-"
                f"{uuid.uuid4().hex[:6]}"
            )
        # B-235: write to <repo>/.xmworktrees/<name>/ instead of
        # <repo>/.claude/worktrees/<name>/.
        wt_path = original_root / ".xmworktrees" / raw_name
        if wt_path.exists():
            return _fail(
                call, t0,
                f"worktree path already exists: {wt_path} — "
                "pick a different name or remove the leftover dir",
            )
        # Branch name: prefix to avoid colliding with regular branches
        # the user creates manually.
        branch = f"wt/{raw_name}"
        base_branch = str(call.args.get("base_branch") or "").strip()

        # 4. ``git worktree add -b <branch> <path> [<base>]``.
        cmd = [
            "git", "-C", str(original_root),
            "worktree", "add", "-b", branch, str(wt_path),
        ]
        if base_branch:
            cmd.append(base_branch)
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            return _fail(call, t0, f"git worktree add timed out: {exc}")
        if res.returncode != 0:
            return _fail(
                call, t0,
                f"git worktree add failed: {(res.stderr or res.stdout).strip()}",
            )

        # 5. Register the new worktree as primary; remember the origin
        # so ``exit_worktree`` can walk back.
        wm.add(wt_path, name=raw_name)
        _WORKTREE_ORIGIN[str(wt_path.resolve())] = original_root

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "worktree_path": str(wt_path),
                "branch": branch,
                "original_root": str(original_root),
                "base_branch": base_branch or "HEAD",
            },
            side_effects=(str(wt_path),),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _exit_worktree(self, call: ToolCall, t0: float) -> ToolResult:
        """B-94: leave the current worktree, optionally remove it.

        Validates that the current primary actually IS a worktree
        before doing anything destructive — refuses to run otherwise.
        """
        from xmclaw.core.workspace import WorkspaceManager

        keep = bool(call.args.get("keep", False))

        wm = WorkspaceManager()
        state = wm.get()
        if state.primary is None:
            return _fail(call, t0, "no primary workspace registered")
        wt_path = state.primary.path
        # B-235: worktree directory must live under .xmworktrees/ (new
        # default) OR .claude/worktrees/ (legacy, back-compat). The
        # check is the cheap heuristic that prevents an accidental
        # ``exit_worktree`` from wiping the user's main checkout.
        wt_str = str(wt_path).replace("\\", "/") + "/"
        _under_xm = "/.xmworktrees/" in wt_str
        _under_legacy = "/.claude/worktrees/" in wt_str
        if not (_under_xm or _under_legacy):
            return _fail(
                call, t0,
                "current primary is not a worktree under "
                ".xmworktrees/ (or legacy .claude/worktrees/) — "
                "refusing to act",
            )

        # Look up the origin we recorded on enter. Fall back to git's
        # own ``worktree list`` if we lost track (daemon restart, etc).
        origin = _WORKTREE_ORIGIN.get(str(wt_path.resolve()))
        if origin is None:
            try:
                res = subprocess.run(
                    ["git", "-C", str(wt_path), "rev-parse", "--show-superproject-working-tree"],
                    capture_output=True, text=True, timeout=10,
                )
                if res.returncode == 0 and res.stdout.strip():
                    origin = Path(res.stdout.strip())
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            if origin is None:
                # B-235: walk up to recover origin repo.
                # New layout: <repo>/.xmworktrees/<name> → parents
                #   [0]=.xmworktrees, [1]=<repo>; origin = parents[1]
                # Legacy:    <repo>/.claude/worktrees/<name> → parents
                #   [0]=worktrees, [1]=.claude, [2]=<repo>; origin = parents[2]
                _parts = wt_path.parts
                if len(_parts) >= 3 and _parts[-2] == ".xmworktrees":
                    origin = wt_path.parents[1]
                elif len(_parts) >= 4 and _parts[-3:-1] == (
                    ".claude", "worktrees",
                ):
                    origin = wt_path.parents[2]
                elif wt_path.parents[1].name == "worktrees":
                    # Defensive fallback for unusual layouts.
                    origin = wt_path.parents[2]
        if origin is None or not origin.exists():
            return _fail(
                call, t0,
                "couldn't determine origin repo for this worktree — "
                "the agent may need to manually `cd` to the parent",
            )

        # Read the current branch name so we can drop it after removal.
        branch_name: str | None = None
        try:
            br = subprocess.run(
                ["git", "-C", str(wt_path), "branch", "--show-current"],
                capture_output=True, text=True, timeout=10,
            )
            if br.returncode == 0:
                branch_name = (br.stdout or "").strip() or None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Switch primary back to the origin BEFORE removing the worktree
        # dir — otherwise WorkspaceManager could end up with a dangling
        # primary entry pointing at a vanished path.
        wm.add(origin)  # add() returns existing entry when already present + makes it primary
        wm.remove(wt_path)
        _WORKTREE_ORIGIN.pop(str(wt_path.resolve()), None)

        removed = False
        if not keep:
            try:
                rm = subprocess.run(
                    ["git", "-C", str(origin), "worktree", "remove", "--force", str(wt_path)],
                    capture_output=True, text=True, timeout=30,
                )
                removed = rm.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                removed = False
            # Drop the branch too — it has no commits worth keeping in
            # the default-discard path. Best-effort; keep returning OK
            # even if the branch delete fails (the worktree is gone,
            # which is the user-visible cleanup goal).
            if removed and branch_name:
                try:
                    subprocess.run(
                        ["git", "-C", str(origin), "branch", "-D", branch_name],
                        capture_output=True, text=True, timeout=10,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass

        return ToolResult(
            call_id=call.id, ok=True,
            content={
                "returned_to": str(origin),
                "worktree_path": str(wt_path),
                "branch": branch_name,
                "kept": keep,
                "removed": removed,
            },
            side_effects=(),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

