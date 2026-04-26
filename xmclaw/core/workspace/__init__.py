"""Workspace = a directory on disk.

Direct port of cline ``src/shared/multi-root/types.ts:11-16`` —
``WorkspaceRoot`` is a tiny dataclass (path + name + vcs + commit_hash)
that callers can resolve paths against. Continue's overlay convention
(``<root>/.continue/<type>/*.yaml``) is layered on top: each project's
``.xmclaw/`` subdir holds project-scoped agents / skills / rules /
prompts / mcpServers / memory.

State persistence is a single ``~/.xmclaw/state.json`` file (cline's
VSCode-globalState equivalent). One running daemon == one workspace
list == one primary index.
"""
from xmclaw.core.workspace.manager import (
    WorkspaceManager,
    state_path,
)
from xmclaw.core.workspace.types import WorkspaceRoot, WorkspaceState

__all__ = [
    "WorkspaceManager",
    "WorkspaceRoot",
    "WorkspaceState",
    "state_path",
]
