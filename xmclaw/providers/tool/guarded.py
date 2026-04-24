"""GuardedToolProvider — wraps a ToolProvider with pre-invocation security."""
from __future__ import annotations

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.security.approval_service import ApprovalService
from xmclaw.security.tool_guard.engine import ToolGuardEngine
from xmclaw.security.tool_guard.models import GuardSeverity


class GuardedToolProvider(ToolProvider):
    """Wraps an inner ``ToolProvider`` and enforces the 4-path decision flow
    before every ``invoke()``.

    Paths:
      * **auto_denied** — tool is in the denied list or findings contain
        ``CRITICAL`` → returns a blocked ``ToolResult`` immediately.
      * **consume_approval** — user already approved this exact call →
        bypasses the guard and delegates to the inner provider (one-shot).
      * **preapproved** — tool is guarded but scan comes back clean →
        delegates to the inner provider.
      * **needs_approval** — tool is guarded and scan finds ``HIGH`` or
        above → creates a pending approval and returns a ``ToolResult``
        with ``error="NEEDS_APPROVAL:<request_id>"``.
      * **fall_through** — tool is not guarded and no always-run guardian
        fired → delegates to the inner provider.
    """

    def __init__(
        self,
        inner: ToolProvider,
        engine: ToolGuardEngine,
        approval_service: ApprovalService | None = None,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._approval_service = approval_service

    def list_tools(self) -> list:
        return self._inner.list_tools()

    async def invoke(self, call: ToolCall) -> ToolResult:
        tool_name = call.name
        params = call.args

        # 1. auto_denied
        if self._engine.is_denied(tool_name):
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=f"Tool '{tool_name}' is blocked by security policy (denied list).",
            )

        # 2. One-shot replay: if user already approved this exact call,
        #    bypass the guard entirely.
        if self._approval_service is not None:
            consumed = await self._approval_service.consume_approval(
                call.session_id or "", tool_name, params
            )
            if consumed:
                return await self._inner.invoke(call)

        # 3. Run guardians
        is_guarded = self._engine.is_guarded(tool_name)
        result = self._engine.guard(
            tool_name, params, only_always_run=not is_guarded
        )

        # 4. auto_denied (CRITICAL findings)
        if result.max_severity == GuardSeverity.CRITICAL:
            summary = _format_findings_summary(result.findings)
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=summary,
                error=f"Tool '{tool_name}' blocked: CRITICAL security finding(s).",
            )

        # 5. needs_approval (HIGH or above findings on guarded tools)
        if is_guarded and result.max_severity in (
            GuardSeverity.HIGH,
            GuardSeverity.CRITICAL,
        ):
            summary = _format_findings_summary(result.findings)
            request_id = ""
            if self._approval_service is not None:
                request_id = await self._approval_service.create(
                    session_id=call.session_id or "",
                    tool_name=tool_name,
                    tool_params=params,
                    findings_summary=summary,
                )
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=summary,
                error=f"NEEDS_APPROVAL:{request_id}",
            )

        # 6. preapproved or fall_through — delegate to inner provider
        return await self._inner.invoke(call)


def _format_findings_summary(findings: list) -> str:
    lines: list[str] = [f"Security scan found {len(findings)} issue(s):"]
    for f in findings:
        lines.append(
            f"  [{f.severity.value.upper()}] {f.rule_id}: {f.description}"
        )
        if f.remediation:
            lines.append(f"       Remediation: {f.remediation}")
    return "\n".join(lines)
