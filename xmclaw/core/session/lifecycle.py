"""Session — first-class OS-primitive with explicit lifecycle.

Phase 1: stub. Session management wires up alongside the CLI demo.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionPhase(str, Enum):
    CREATE = "create"
    ACTIVE = "active"
    CHECKPOINT = "checkpoint"
    DESTROY = "destroy"


@dataclass
class Session:
    id: str
    agent_id: str
    phase: SessionPhase
