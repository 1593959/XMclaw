"""Execution policy helpers for high-risk builtin tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ShellExecutionPolicyName = Literal["host_guarded", "docker", "disabled"]

_VALID_POLICIES: set[str] = {"host_guarded", "docker", "disabled"}


@dataclass(frozen=True, slots=True)
class ShellExecutionPolicy:
    name: ShellExecutionPolicyName

    @property
    def enabled(self) -> bool:
        return self.name != "disabled"

    @property
    def requires_external_sandbox(self) -> bool:
        return self.name == "docker"

    def refusal_reason(self) -> str | None:
        if self.name == "disabled":
            return "bash tool is disabled by tools.shell.execution_policy=disabled"
        return None

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name}


def resolve_shell_execution_policy(value: object | None) -> ShellExecutionPolicy:
    if value is None:
        return ShellExecutionPolicy("host_guarded")
    name = str(value).strip().lower().replace("-", "_")
    if name not in _VALID_POLICIES:
        raise ValueError(
            "invalid tools.shell.execution_policy "
            f"{value!r}; expected one of {sorted(_VALID_POLICIES)}"
        )
    return ShellExecutionPolicy(name)  # type: ignore[arg-type]


__all__ = [
    "ShellExecutionPolicy",
    "ShellExecutionPolicyName",
    "resolve_shell_execution_policy",
]
