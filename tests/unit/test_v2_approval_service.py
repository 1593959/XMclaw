"""Tests for xmclaw.security.approval_service."""
from __future__ import annotations

import time

import pytest

from xmclaw.core.ir import ToolCall, ToolResult
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.guarded import GuardedToolProvider
from xmclaw.security.approval_service import ApprovalService, _GC_MAX_PENDING
from xmclaw.security.tool_guard.engine import ToolGuardEngine
from xmclaw.security.tool_guard.file_guardian import FilePathToolGuardian
from xmclaw.security.tool_guard.rule_guardian import RuleBasedToolGuardian
from xmclaw.security.tool_guard.shell_evasion_guardian import ShellEvasionGuardian


class DummyProvider(ToolProvider):
    def list_tools(self):
        return []

    async def invoke(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, ok=True, content="ok")


# ---------------------------------------------------------------------------
# ApprovalService core
# ---------------------------------------------------------------------------

class TestApprovalService:
    @pytest.fixture
    def svc(self):
        return ApprovalService()

    @pytest.mark.anyio
    async def test_create_and_resolve(self, svc):
        req_id = await svc.create("sess-1", "file_read", {"path": "/etc/shadow"}, "summary")
        assert req_id
        pending = await svc.list_pending("sess-1")
        assert len(pending) == 1
        assert pending[0].status == "pending"

        ok = await svc.approve(req_id)
        assert ok
        assert (await svc.list_pending("sess-1")) == []

        # consume_approval should match
        consumed = await svc.consume_approval("sess-1", "file_read", {"path": "/etc/shadow"})
        assert consumed is True

        # one-shot: second consume fails
        consumed2 = await svc.consume_approval("sess-1", "file_read", {"path": "/etc/shadow"})
        assert consumed2 is False

    @pytest.mark.anyio
    async def test_consume_requires_exact_params(self, svc):
        req_id = await svc.create("sess-1", "file_read", {"path": "/etc/shadow"}, "summary")
        await svc.approve(req_id)
        # Different params → no match
        consumed = await svc.consume_approval("sess-1", "file_read", {"path": "/etc/passwd"})
        assert consumed is False

    @pytest.mark.anyio
    async def test_gc_timeout(self, svc, monkeypatch):
        # Create a pending record
        req_id = await svc.create("sess-1", "file_read", {"path": "/etc/shadow"}, "summary")
        # Manually age the record beyond the 30min threshold
        record = svc._pending[req_id]
        record.created_at = time.monotonic() - 31 * 60
        # Trigger GC via a new create
        await svc.create("sess-2", "file_read", {"path": "/etc/passwd"}, "summary")
        # Old record should have been timed out and moved to completed
        assert req_id not in svc._pending
        completed = svc._completed[req_id]
        assert completed.status == "timeout"

    @pytest.mark.anyio
    async def test_gc_capacity(self, svc):
        # Fill pending beyond the 200 cap
        for i in range(_GC_MAX_PENDING + 10):
            await svc.create(f"sess-{i}", "file_read", {"path": f"/tmp/{i}"}, "summary")
        assert len(svc._pending) == _GC_MAX_PENDING
        # Oldest records should have been evicted to completed
        assert len(svc._completed) == 10


# ---------------------------------------------------------------------------
# GuardedToolProvider integration
# ---------------------------------------------------------------------------

class TestGuardedToolProviderWithApproval:
    @pytest.fixture
    def inner(self):
        return DummyProvider()

    @pytest.fixture
    def engine(self):
        return ToolGuardEngine(guardians=[
            FilePathToolGuardian(sensitive_files=["~/.ssh"]),
            RuleBasedToolGuardian(),
            ShellEvasionGuardian(),
        ])

    @pytest.fixture
    def svc(self):
        return ApprovalService()

    @pytest.mark.anyio
    async def test_consume_approval_bypasses_guard(self, inner, engine, svc):
        provider = GuardedToolProvider(inner, engine, approval_service=svc)
        call = ToolCall(
            id="c1",
            name="execute_shell_command",
            args={"command": "rm -rf /home/user/old_project"},
            provenance="synthetic",
            session_id="sess-1",
        )
        # First call blocked (HIGH, not CRITICAL)
        result1 = await provider.invoke(call)
        assert result1.ok is False
        assert "NEEDS_APPROVAL" in (result1.error or "")

        # Approve
        request_id = result1.error.split(":", 1)[1]
        await svc.approve(request_id)

        # Second identical call passes via consume_approval
        result2 = await provider.invoke(call)
        assert result2.ok is True
        assert result2.content == "ok"

    @pytest.mark.anyio
    async def test_creates_pending_on_needs_approval(self, inner, engine, svc):
        provider = GuardedToolProvider(inner, engine, approval_service=svc)
        call = ToolCall(
            id="c1",
            name="execute_shell_command",
            args={"command": "rm -rf /"},
            provenance="synthetic",
            session_id="sess-2",
        )
        result = await provider.invoke(call)
        assert result.ok is False
        assert "NEEDS_APPROVAL" in (result.error or "")
        request_id = result.error.split(":", 1)[1]
        assert request_id
        record = await svc.get(request_id)
        assert record is not None
        assert record.status == "pending"
        assert record.tool_name == "execute_shell_command"
