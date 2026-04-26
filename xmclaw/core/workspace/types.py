"""WorkspaceRoot data model — direct port of cline's TypeScript shape.

Source: ``cline/src/shared/multi-root/types.ts:11-16``::

    export interface WorkspaceRoot {
      path: string;
      name?: string;
      vcs: VcsType;
      commitHash?: string;
    }

The Python equivalent is a frozen dataclass so it can be hashed +
serialized to ``state.json`` without surprises. ``path`` is always
absolute + resolved (symlinks followed) to avoid containment-check
bypasses (we did this lesson already in :mod:`xmclaw.daemon.routers.files`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

VcsType = Literal["git", "none"]


@dataclass(frozen=True, slots=True)
class WorkspaceRoot:
    path: Path
    name: str
    vcs: VcsType = "none"
    commit_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.name,
            "vcs": self.vcs,
            "commit_hash": self.commit_hash,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkspaceRoot":
        path = Path(str(raw.get("path", ""))).expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass
        name = raw.get("name") or path.name or str(path)
        vcs = raw.get("vcs") or "none"
        if vcs not in ("git", "none"):
            vcs = "none"
        commit_hash = raw.get("commit_hash")
        return cls(
            path=path,
            name=str(name),
            vcs=vcs,  # type: ignore[arg-type]
            commit_hash=str(commit_hash) if commit_hash else None,
        )

    @classmethod
    def from_path(cls, path: Path | str, *, name: str | None = None) -> "WorkspaceRoot":
        p = Path(str(path)).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        return cls(
            path=p,
            name=name or p.name or str(p),
            vcs=detect_vcs(p),
            commit_hash=None,
        )


def detect_vcs(path: Path) -> VcsType:
    try:
        if (path / ".git").exists():
            return "git"
    except OSError:
        return "none"
    return "none"


@dataclass
class WorkspaceState:
    """Persistent state for the daemon's workspace list.

    Mirrors cline's VSCode globalState keys
    ``workspaceRoots`` + ``primaryRootIndex`` (``disk.ts:559-574``).
    Stored as JSON at :func:`state_path`.
    """
    roots: list[WorkspaceRoot] = field(default_factory=list)
    primary_index: int = 0

    @property
    def primary(self) -> WorkspaceRoot | None:
        if not self.roots:
            return None
        idx = max(0, min(self.primary_index, len(self.roots) - 1))
        return self.roots[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": [r.to_dict() for r in self.roots],
            "primary_index": self.primary_index,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkspaceState":
        raw_roots = raw.get("roots") or []
        roots: list[WorkspaceRoot] = []
        if isinstance(raw_roots, list):
            for r in raw_roots:
                if isinstance(r, dict):
                    try:
                        roots.append(WorkspaceRoot.from_dict(r))
                    except (TypeError, ValueError):
                        continue
        idx_raw = raw.get("primary_index", 0)
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            idx = 0
        return cls(roots=roots, primary_index=idx)
