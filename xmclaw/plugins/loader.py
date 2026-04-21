"""Plugin discovery via setuptools entry-points.

Third-party plugins ship as packages that declare entry-points under:
  xmclaw.plugins.llm
  xmclaw.plugins.memory
  xmclaw.plugins.channel
  xmclaw.plugins.tool
  xmclaw.plugins.runtime
  xmclaw.plugins.grader
  xmclaw.plugins.scheduler

The daemon iterates ``importlib.metadata.entry_points(group=...)`` on start.
Phase 2 deliverable.
"""
from __future__ import annotations

from typing import Any


def load_plugins(kind: str) -> list[Any]:  # noqa: ARG001
    raise NotImplementedError("Phase 2")
