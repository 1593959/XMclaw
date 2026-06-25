"""Named error templates for tool failures.

The strings stay plain-text because ToolResult.error is currently a string,
but the bracketed kind gives planners, retry logic, and self-critique a stable
surface that is easier to parse than raw stderr.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolErrorTemplate:
    kind: str
    summary: str
    hint: str | None = None
    detail: str | None = None

    def render(self) -> str:
        parts = [f"[{self.kind}] {self.summary}"]
        if self.detail:
            parts.append(f"detail: {self.detail}")
        if self.hint:
            parts.append(f"hint: {self.hint}")
        return " | ".join(parts)


def shell_check_error_template(
    summary: str,
    *,
    detail: str | None = None,
    hint: str | None = None,
) -> str:
    return ToolErrorTemplate(
        kind="shell_check_error",
        summary=summary,
        detail=detail,
        hint=hint or "Inspect the command, working directory, and last stderr line before retrying.",
    ).render()


def tool_timeout_error_template(
    tool_name: str,
    timeout_s: float,
    *,
    detail: str | None = None,
) -> str:
    return ToolErrorTemplate(
        kind="tool_timeout",
        summary=f"{tool_name} timed out after {timeout_s:g}s",
        detail=detail,
        hint="Retry with a smaller command, narrower input, or an explicit longer timeout when appropriate.",
    ).render()


def tool_sandbox_error_template(
    summary: str,
    *,
    detail: str | None = None,
) -> str:
    return ToolErrorTemplate(
        kind="tool_sandbox_error",
        summary=summary,
        detail=detail,
        hint="Check tools.shell execution_policy and sandbox runtime availability.",
    ).render()


def path_not_found_error_template(path: str) -> str:
    return ToolErrorTemplate(
        kind="path_not_found",
        summary=f"path not found: {path}",
        hint="List the parent directory or ask the user for the correct path before retrying.",
    ).render()


def permission_denied_error_template(path: str | None = None) -> str:
    detail = f"path: {path}" if path else None
    return ToolErrorTemplate(
        kind="permission_denied",
        summary="permission denied",
        detail=detail,
        hint="Choose a permitted path or ask the user for permission instead of retrying blindly.",
    ).render()


__all__ = [
    "ToolErrorTemplate",
    "path_not_found_error_template",
    "permission_denied_error_template",
    "shell_check_error_template",
    "tool_sandbox_error_template",
    "tool_timeout_error_template",
]
