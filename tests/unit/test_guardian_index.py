"""Tests for ToolGuardEngine's tool-name -> guardian index.

Covers:
1. Index construction correctness.
2. guard() only consults relevant guardians.
3. is_guarded() / is_denied() behaviour is unchanged.
4. Performance: 100 targeted guardians → <5 consulted per call.
5. Backward compatibility with real guardian implementations.
"""
from __future__ import annotations

import pytest

from xmclaw.security.tool_guard.base import BaseToolGuardian
from xmclaw.security.tool_guard.engine import ToolGuardEngine, _ALWAYS_RUN_GUARDIANS
from xmclaw.security.tool_guard.models import GuardFinding, GuardSeverity


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class _MockGuardian(BaseToolGuardian):
    """A mock guardian that records invocations and optionally returns findings."""

    def __init__(self, name: str, tool_names: tuple[str, ...] = ("*",), findings: list[GuardFinding] | None = None) -> None:
        self._name = name
        self._tool_names = tool_names
        self._findings = findings or []
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    def guarded_tool_names(self) -> tuple[str, ...]:
        return self._tool_names

    def guard(self, tool_name: str, params: dict) -> list[GuardFinding]:
        self.calls.append((tool_name, params))
        return list(self._findings)


class _LegacyGuardian(BaseToolGuardian):
    """A guardian that does NOT implement guarded_tool_names() —
    simulates pre-index existing guardians."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    def guard(self, tool_name: str, params: dict) -> list[GuardFinding]:
        self.calls.append((tool_name, params))
        return []


# ---------------------------------------------------------------------------
# 1. Index construction
# ---------------------------------------------------------------------------

class TestIndexConstruction:
    def test_specific_tools_indexed(self):
        g_bash = _MockGuardian("bash_only", ("bash",))
        g_file = _MockGuardian("file_only", ("file_read", "file_write"))
        engine = ToolGuardEngine(guardians=[g_bash, g_file])

        assert engine._guardian_index["bash"] == [g_bash]
        assert engine._guardian_index["file_read"] == [g_file]
        assert engine._guardian_index["file_write"] == [g_file]
        assert "other_tool" not in engine._guardian_index
        assert engine._universal_guardians == []

    def test_universal_star_guardians(self):
        g_all = _MockGuardian("all_tools", ("*",))
        engine = ToolGuardEngine(guardians=[g_all])

        assert engine._guardian_index == {}
        assert engine._universal_guardians == [g_all]

    def test_legacy_guardian_defaults_to_universal(self):
        """Guardians without guarded_tool_names() default to '*'."""
        g_legacy = _LegacyGuardian("legacy")
        engine = ToolGuardEngine(guardians=[g_legacy])

        assert engine._guardian_index == {}
        assert engine._universal_guardians == [g_legacy]

    def test_mixed_index(self):
        g_specific = _MockGuardian("specific", ("bash",))
        g_universal = _MockGuardian("universal", ("*",))
        g_legacy = _LegacyGuardian("legacy")
        engine = ToolGuardEngine(guardians=[g_specific, g_universal, g_legacy])

        assert engine._guardian_index["bash"] == [g_specific]
        assert engine._universal_guardians == [g_universal, g_legacy]

    def test_duplicate_tool_names(self):
        g1 = _MockGuardian("g1", ("bash",))
        g2 = _MockGuardian("g2", ("bash",))
        engine = ToolGuardEngine(guardians=[g1, g2])

        assert engine._guardian_index["bash"] == [g1, g2]

    def test_invalidate_index_rebuilds(self):
        g1 = _MockGuardian("g1", ("bash",))
        engine = ToolGuardEngine(guardians=[g1])
        assert engine._guardian_index["bash"] == [g1]

        g2 = _MockGuardian("g2", ("file_read",))
        engine._guardians.append(g2)  # runtime mutation
        engine.invalidate_index()
        assert engine._guardian_index["bash"] == [g1]
        assert engine._guardian_index["file_read"] == [g2]


# ---------------------------------------------------------------------------
# 2. guard() only calls relevant guardians
# ---------------------------------------------------------------------------

class TestGuardSelectiveInvocation:
    def test_specific_guardian_not_called_for_other_tools(self):
        g_bash = _MockGuardian("bash_only", ("bash",))
        engine = ToolGuardEngine(guardians=[g_bash])
        result = engine.guard("file_read", {"file_path": "/tmp/x"})

        assert g_bash.calls == []
        assert result.is_safe
        assert result.guardians_used == []

    def test_specific_guardian_called_for_its_tool(self):
        g_bash = _MockGuardian("bash_only", ("bash",), findings=[
            GuardFinding(
                rule_id="R1", category="x", severity=GuardSeverity.HIGH,
                title="t", description="d", tool_name="bash", guardian="bash_only",
            )
        ])
        engine = ToolGuardEngine(guardians=[g_bash])
        result = engine.guard("bash", {"command": "ls"})

        assert g_bash.calls == [("bash", {"command": "ls"})]
        assert result.guardians_used == ["bash_only"]

    def test_universal_guardian_called_for_every_tool(self):
        g_all = _MockGuardian("all_tools", ("*",), findings=[
            GuardFinding(
                rule_id="R1", category="x", severity=GuardSeverity.HIGH,
                title="t", description="d", tool_name="bash", guardian="all_tools",
            )
        ])
        engine = ToolGuardEngine(guardians=[g_all])
        result = engine.guard("any_tool", {})

        assert g_all.calls == [("any_tool", {})]
        assert not result.is_safe

    def test_mixed_specific_and_universal(self):
        g_specific = _MockGuardian("specific", ("bash",))
        g_universal = _MockGuardian("universal", ("*",))
        engine = ToolGuardEngine(guardians=[g_specific, g_universal])

        engine.guard("bash", {})
        assert g_specific.calls == [("bash", {})]
        assert g_universal.calls == [("bash", {})]

        g_specific.calls.clear()
        g_universal.calls.clear()
        engine.guard("file_read", {})
        assert g_specific.calls == []
        assert g_universal.calls == [("file_read", {})]

    def test_legacy_guardian_always_called(self):
        g_legacy = _LegacyGuardian("legacy")
        engine = ToolGuardEngine(guardians=[g_legacy])
        engine.guard("anything", {})
        assert g_legacy.calls == [("anything", {})]

    def test_only_always_run_flag(self):
        """only_always_run=True must still only consult _ALWAYS_RUN_GUARDIANS."""
        g_file = _MockGuardian("file_path", ("*",), findings=[
            GuardFinding(
                rule_id="R1", category="x", severity=GuardSeverity.HIGH,
                title="t", description="d", tool_name="bash", guardian="file_path",
            )
        ])
        g_other = _MockGuardian("other", ("*",))
        engine = ToolGuardEngine(guardians=[g_file, g_other])
        result = engine.guard("bash", {"command": "ls"}, only_always_run=True)

        assert g_file.calls == [("bash", {"command": "ls"})]
        assert g_other.calls == []
        assert result.guardians_used == ["file_path"]

    def test_guardian_failure_is_swallowed(self):
        class _Broken(_MockGuardian):
            def guard(self, tool_name, params):
                raise RuntimeError("boom")

        g = _Broken("broken", ("bash",))
        engine = ToolGuardEngine(guardians=[g])
        result = engine.guard("bash", {})
        # Must not raise, and must be safe (no findings because it failed)
        assert result.is_safe
        assert result.guardians_used == []


# ---------------------------------------------------------------------------
# 3. is_guarded() / is_denied() unchanged
# ---------------------------------------------------------------------------

class TestPublicApiUnchanged:
    def test_is_denied(self):
        engine = ToolGuardEngine(denied_tools={"bad"})
        assert engine.is_denied("bad")
        assert not engine.is_denied("good")

    def test_is_guarded(self):
        engine = ToolGuardEngine(guarded_tools={"bash", "file_read"})
        assert engine.is_guarded("bash")
        assert not engine.is_guarded("todo_write")

    def test_defaults_intact(self):
        from xmclaw.security.tool_guard.engine import _DEFAULT_GUARDED_TOOLS, _DEFAULT_DENIED_TOOLS
        engine = ToolGuardEngine()
        assert engine._guarded_tools == _DEFAULT_GUARDED_TOOLS
        assert engine._denied_tools == _DEFAULT_DENIED_TOOLS


# ---------------------------------------------------------------------------
# 4. Performance: 100 guardians → <5 consulted per call
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_100_guardians_only_few_consulted(self):
        """With 100 tool-specific guardians, a single tool call should
        consult only the guardians registered for that tool."""
        guardians: list[BaseToolGuardian] = []
        for i in range(100):
            g = _MockGuardian(f"g_{i}", (f"tool_{i}",))
            guardians.append(g)
        engine = ToolGuardEngine(guardians=guardians)

        engine.guard("tool_42", {})

        # Only the guardian for tool_42 should have been called.
        consulted = [g for g in guardians if g.calls]
        assert len(consulted) == 1
        assert consulted[0].name == "g_42"

    def test_mixed_100_with_universal(self):
        """99 specific + 1 universal → 2 consulted per call."""
        specific = [_MockGuardian(f"g_{i}", (f"tool_{i}",)) for i in range(99)]
        universal = _MockGuardian("universal", ("*",))
        engine = ToolGuardEngine(guardians=specific + [universal])

        engine.guard("tool_50", {})
        consulted = [g for g in specific if g.calls]
        assert len(consulted) == 1
        assert universal.calls == [("tool_50", {})]


# ---------------------------------------------------------------------------
# 5. Backward compatibility with real guardian implementations
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_real_guardians_via_engine(self):
        from xmclaw.security.tool_guard.file_guardian import FilePathToolGuardian
        from xmclaw.security.tool_guard.rule_guardian import RuleBasedToolGuardian
        from xmclaw.security.tool_guard.shell_evasion_guardian import ShellEvasionGuardian

        engine = ToolGuardEngine(guardians=[
            FilePathToolGuardian(sensitive_files=["~/.ssh"]),
            RuleBasedToolGuardian(),
            ShellEvasionGuardian(),
        ])
        # A dangerous command should still be caught.
        result = engine.guard("bash", {"command": "rm -rf /"})
        assert not result.is_safe
        assert result.max_severity == GuardSeverity.CRITICAL

    def test_real_guardians_clean_call(self):
        from xmclaw.security.tool_guard.file_guardian import FilePathToolGuardian
        from xmclaw.security.tool_guard.rule_guardian import RuleBasedToolGuardian
        from xmclaw.security.tool_guard.shell_evasion_guardian import ShellEvasionGuardian

        engine = ToolGuardEngine(guardians=[
            FilePathToolGuardian(),
            RuleBasedToolGuardian(),
            ShellEvasionGuardian(),
        ])
        result = engine.guard("bash", {"command": "echo hello"})
        assert result.is_safe
        assert result.max_severity == GuardSeverity.SAFE

    def test_only_always_run_with_real_guardians(self):
        from xmclaw.security.tool_guard.file_guardian import FilePathToolGuardian
        from xmclaw.security.tool_guard.rule_guardian import RuleBasedToolGuardian

        engine = ToolGuardEngine(guardians=[
            FilePathToolGuardian(sensitive_files=["~/.ssh"]),
            RuleBasedToolGuardian(),
        ])
        # only_always_run=True → only file_path runs (RuleBased is skipped)
        result = engine.guard("bash", {"command": "cat ~/.ssh/id_rsa"}, only_always_run=True)
        # RuleBased would have caught rm -rf /, but it is skipped.
        # FilePath should still catch the sensitive path.
        assert not result.is_safe
        assert result.guardians_used == ["file_path"]
