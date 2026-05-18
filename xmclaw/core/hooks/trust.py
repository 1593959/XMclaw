"""Workspace trust marker — gates command/function hook execution.

A workspace marked ``untrusted`` blocks the ``command`` and ``function``
runner kinds (both can execute arbitrary local code; ``http``,
``prompt``, ``agent`` are safer because the payload doesn't reach
the user's shell or Python interpreter directly).

The trust marker is a single-line file inside the workspace root:

    <workspace_root>/.xmclaw-trust

When the file exists, the workspace is trusted. Operators add the
marker explicitly the first time they review a hook config (matches
the Claude Code "do you trust this workspace?" prompt).

The marker file's contents are ignored — presence is the signal. We
deliberately don't sign / hash it because attackers who can write
inside the workspace can already run scripts via ``bash`` tool; the
goal is to prevent ACCIDENTAL hook execution on a fresh clone, not
to defend against a compromised workspace.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal


_TRUST_MARKER = ".xmclaw-trust"


TrustLevel = Literal["trusted", "untrusted"]


def workspace_trust_level(
    workspace_root: str | Path | None,
) -> TrustLevel:
    """Return ``"trusted"`` if the workspace has an ``.xmclaw-trust``
    marker, else ``"untrusted"``.

    ``workspace_root=None`` → ``"untrusted"`` (no workspace, can't
    verify trust).
    """
    if workspace_root is None:
        return "untrusted"
    root = Path(str(workspace_root))
    marker = root / _TRUST_MARKER
    try:
        return "trusted" if marker.is_file() else "untrusted"
    except OSError:
        return "untrusted"


def mark_workspace_trusted(workspace_root: str | Path) -> Path:
    """Create the ``.xmclaw-trust`` marker. Returns the path written.

    Used by ``xmclaw trust <dir>`` CLI command. Idempotent: re-running
    on an already-trusted workspace is a no-op.
    """
    root = Path(str(workspace_root))
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _TRUST_MARKER
    if not marker.exists():
        marker.write_text(
            "# Marks this workspace as trusted by XMclaw.\n"
            "# Command + function hooks may run when this file is present.\n"
            "# Remove the file to revoke trust.\n",
            encoding="utf-8",
        )
    return marker


def unmark_workspace_trusted(workspace_root: str | Path) -> bool:
    """Remove the trust marker. Returns True when one was removed."""
    root = Path(str(workspace_root))
    marker = root / _TRUST_MARKER
    if marker.is_file():
        marker.unlink()
        return True
    return False


__all__ = [
    "TrustLevel",
    "workspace_trust_level",
    "mark_workspace_trusted",
    "unmark_workspace_trusted",
]
