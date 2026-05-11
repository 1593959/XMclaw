"""GuardedToolProvider — wraps a ToolProvider with pre-invocation security."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.security.approval_service import ApprovalService
from xmclaw.security.tool_guard.engine import ToolGuardEngine
from xmclaw.security.tool_guard.models import (
    GuardianAction,
    GuardianPolicy,
    GuardSeverity,
)
from xmclaw.utils.i18n import _

if TYPE_CHECKING:
    from xmclaw.security.auditor import SecurityAuditor


class GuardedToolProvider(ToolProvider):
    """Wraps an inner ``ToolProvider`` and enforces a policy-driven
    decision flow before every ``invoke()``.

    The flow is:

    1. **denied_list** — tool is unconditionally blocked → return
       blocked ``ToolResult``.
    2. **consume_approval** — user already approved this exact
       ``(session_id, tool_name, params)`` tuple → one-shot bypass
       and delegate to the inner provider.
    3. **scan** — run guardians (full scan if ``is_guarded``, only
       always-run guardians otherwise).
    4. **policy lookup** — consult :class:`GuardianPolicy` with the
       scan's ``max_severity``. The result is a :class:`GuardianAction`:

       - ``DENY``    → block with findings summary.
       - ``APPROVE`` → create a pending approval via
         :class:`ApprovalService` and return
         ``error="NEEDS_APPROVAL:<request_id>"``.
       - ``ALLOW``   → delegate to the inner provider.

    Default policy (set in :class:`GuardianPolicy`) preserves the
    original hard-coded behavior: CRITICAL→DENY, HIGH→APPROVE, the
    rest ALLOW.
    """

    def __init__(
        self,
        inner: ToolProvider,
        engine: ToolGuardEngine,
        approval_service: ApprovalService | None = None,
        policy: GuardianPolicy | None = None,
        auditor: "SecurityAuditor | None" = None,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._approval_service = approval_service
        self._policy = policy or GuardianPolicy()
        self._auditor = auditor

    def list_tools(self) -> list[Any]:
        return self._inner.list_tools()

    async def invoke(self, call: ToolCall) -> ToolResult:
        tool_name = call.name
        params = call.args

        # 1. auto_denied (explicit deny list)
        if self._engine.is_denied(tool_name):
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=_("guard.blocked.denied_list", tool_name=tool_name),
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

        # 4. No findings at all — fall through without consulting policy.
        #    Saves an enum lookup on the hot path (most calls are clean).
        if not result.findings:
            return await self._inner.invoke(call)

        # 5. Policy lookup on max severity.
        max_sev = result.max_severity or GuardSeverity.SAFE
        action = self._policy.action_for(max_sev)

        _findings_dicts = [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "description": f.description,
                "remediation": f.remediation,
            }
            for f in result.findings
        ]

        if action == GuardianAction.DENY:
            summary = _format_findings_summary(result.findings)
            if self._auditor is not None:
                self._auditor.record_tool_guard(
                    session_id=call.session_id,
                    tool_name=tool_name,
                    action="deny",
                    findings=_findings_dicts,
                )
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=summary,
                error=_("guard.blocked.severity", tool_name=tool_name, severity=max_sev.name),
            )

        if action == GuardianAction.APPROVE:
            summary = _format_findings_summary(result.findings)
            request_id = ""
            if self._approval_service is not None:
                request_id = await self._approval_service.create(
                    session_id=call.session_id or "",
                    tool_name=tool_name,
                    tool_params=params,
                    findings_summary=summary,
                )
            if self._auditor is not None:
                self._auditor.record_tool_guard(
                    session_id=call.session_id,
                    tool_name=tool_name,
                    action="approve",
                    findings=_findings_dicts,
                )
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=summary,
                error=f"NEEDS_APPROVAL:{request_id}",
            )

        # ALLOW — fall through to the inner provider.
        return await self._inner.invoke(call)


def _format_findings_summary(findings: list[Any]) -> str:
    lines: list[str] = [_("guard.scan_summary_header", count=len(findings))]
    for f in findings:
        lines.append(
            _(
                "guard.scan_summary_item",
                severity=f.severity.value.upper(),
                rule_id=f.rule_id,
                description=f.description,
            )
        )
        if f.remediation:
            lines.append(
                _(
                    "guard.scan_summary_remediation",
                    remediation=f.remediation,
                )
            )
    return "\n".join(lines)
