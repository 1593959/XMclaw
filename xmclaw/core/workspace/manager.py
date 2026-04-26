"""WorkspaceManager — thread-safe accessor for ``~/.xmclaw/state.json``.

Persistence shape (see :mod:`xmclaw.core.workspace.types`):

    {
      "roots": [
        {"path": "/abs/path", "name": "myproject", "vcs": "git", ...},
        ...
      ],
      "primary_index": 0
    }

API mirrors cline's ``WorkspaceRootManager`` (``setup.ts:49-89``):
``add``, ``remove``, ``set_primary``, ``resolve_path_to_root`` (which
tool calls use to reject out-of-workspace writes — port follows in
Phase 4 with the security/approval_service work).

Atomic write via temp + rename so a crash mid-flush can't truncate
``state.json`` to a half-row JSON document. The lock is a process-local
``threading.Lock``; cross-process serialization is not needed because
only one daemon runs per data dir.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from xmclaw.core.workspace.types import WorkspaceRoot, WorkspaceState
from xmclaw.utils.paths import data_dir


def state_path() -> Path:
    """Where the daemon's workspace state lives."""
    return data_dir() / "state.json"


class WorkspaceManager:
    """Lazy-loaded, lock-guarded JSON-backed workspace registry.

    ``primary`` is the active workspace — what new turns default to. The
    REST endpoint :mod:`xmclaw.daemon.routers.workspace` exposes
    add/remove/set_primary; the AgentLoop reads ``primary`` to scope
    persona overlays + skills.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or state_path()
        self._lock = threading.Lock()
        self._state: WorkspaceState | None = None

    # ── load / persist ────────────────────────────────────────────────

    def _load(self) -> WorkspaceState:
        if not self._path.exists():
            return WorkspaceState()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorkspaceState()
        if not isinstance(raw, dict):
            return WorkspaceState()
        return WorkspaceState.from_dict(raw)

    def _save(self, state: WorkspaceState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".write.tmp")
        try:
            tmp.write_text(
                json.dumps(state.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    def get(self) -> WorkspaceState:
        """Return a snapshot of the current state."""
        with self._lock:
            if self._state is None:
                self._state = self._load()
            # Return a fresh dict-equivalent copy so callers can't mutate
            # our cached state by accident.
            return WorkspaceState(
                roots=list(self._state.roots),
                primary_index=self._state.primary_index,
            )

    # ── mutations ─────────────────────────────────────────────────────

    def add(
        self, path: Path | str, *, name: str | None = None
    ) -> WorkspaceRoot:
        """Add a workspace root. If already present, return the existing
        entry (no duplicates). Mirrors cline ``add()`` semantics."""
        root = WorkspaceRoot.from_path(path, name=name)
        with self._lock:
            self._state = self._state if self._state is not None else self._load()
            for existing in self._state.roots:
                if existing.path == root.path:
                    return existing
            self._state.roots.append(root)
            # New root becomes primary by default (cline default behaviour).
            self._state.primary_index = len(self._state.roots) - 1
            self._save(self._state)
            return root

    def remove(self, path: Path | str) -> bool:
        """Drop a workspace root by path. Returns True iff something was
        removed."""
        target = Path(str(path)).expanduser()
        try:
            target = target.resolve()
        except OSError:
            pass
        with self._lock:
            self._state = self._state if self._state is not None else self._load()
            new_roots = [r for r in self._state.roots if r.path != target]
            if len(new_roots) == len(self._state.roots):
                return False
            self._state.roots = new_roots
            self._state.primary_index = (
                min(self._state.primary_index, len(new_roots) - 1)
                if new_roots else 0
            )
            self._save(self._state)
            return True

    def set_primary(self, index: int) -> bool:
        """Set the primary index. Returns True iff it actually moved."""
        with self._lock:
            self._state = self._state if self._state is not None else self._load()
            if not self._state.roots:
                return False
            new_idx = max(0, min(int(index), len(self._state.roots) - 1))
            if new_idx == self._state.primary_index:
                return False
            self._state.primary_index = new_idx
            self._save(self._state)
            return True

    # ── containment helper (used by tool approvals in Phase 4) ────────

    def resolve_path_to_root(self, path: Path | str) -> WorkspaceRoot | None:
        """Return the workspace root that ``path`` belongs to, or ``None``.

        Mirrors cline ``resolvePathToRoot`` (``WorkspaceRootManager.ts:
        100-111``). Used by tools to gate "writes only inside workspace"
        policies — port for Phase 4 file_write integration.
        """
        try:
            target = Path(str(path)).expanduser().resolve()
        except OSError:
            return None
        for root in self.get().roots:
            try:
                target.relative_to(root.path)
            except ValueError:
                continue
            return root
        return None
