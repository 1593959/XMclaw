"""HTTP router modules mounted by :func:`xmclaw.daemon.app.create_app`.

Split out from ``app.py`` so that each web-UI surface area (file
browser, workspaces, profiles, memory) lives in its own module with
targeted unit tests — ``app.py`` already carries the WebSocket plumbing,
pairing check, and event-replay logic.

New routers are registered in ``app.py`` via
``app.include_router(...)`` — they do not self-register here.
"""
from __future__ import annotations

from xmclaw.daemon.routers import files, memory, profiles, workspaces

__all__ = ["files", "memory", "profiles", "workspaces"]
