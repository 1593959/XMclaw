"""BaseToolGuardian ABC."""
from __future__ import annotations

import abc
from typing import Any

from .models import GuardFinding


class BaseToolGuardian(abc.ABC):
    """A single security lens applied to a tool invocation."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable guardian identifier (e.g. ``rule_based``)."""
        ...

    @abc.abstractmethod
    def guard(self, tool_name: str, params: dict[str, Any]) -> list[GuardFinding]:
        """Inspect the tool call and return zero or more findings."""
        ...

    def reload(self) -> None:
        """Hot-reload rules / config. Default no-op."""
        pass
