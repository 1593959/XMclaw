"""B-331: BuiltinTools write-path workspace-containment audit.

Pre-B-331 ``WorkspaceManager.resolve_path_to_root`` had zero callers —
the docstring promised "used by tools to gate writes inside workspace"
but no tool actually consulted it. ``file_write`` /
``apply_patch`` / ``file_delete`` happily wrote to any path the model
picked, regardless of the workspace roots the user configured via the
Web UI. anti-req #5 (sandbox / containment) was unmet.

This test set covers the visibility-only fix:
  * ``BuiltinTools(workspace_manager_provider=...)`` accepts the
    provider
  * ``file_write`` outside any configured root → WARNING logged with
    ``tool.write_outside_workspace``
  * ``file_write`` inside a configured root → no warning (happy path)
  * No provider wired → no warning (test / echo-mode behaviour)
  * Provider returns no-roots manager → no warning (fresh install,
    pre-onboard)
  * The write itself still succeeds — visibility, not enforcement.

Behavior change (ASK-confirm / deny) is intentionally a separate
epic; this commit only surfaces the previously-silent escape.
"""
from __future__ import annotations

import logging as _logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


class _StubWorkspaceManager:
    """Quacks like xmclaw.core.workspace.manager.WorkspaceManager for
    BuiltinTools' purposes — exposes ``get().roots`` + the
    ``resolve_path_to_root`` containment check."""

    def __init__(self, root_paths: list[Path]) -> None:
        self._roots = [SimpleNamespace(path=p, name=p.name) for p in root_paths]

    def get(self):
        return SimpleNamespace(roots=tuple(self._roots), primary=None)

    def resolve_path_to_root(self, path: Path | str):
        try:
            target = Path(str(path)).expanduser().resolve()
        except OSError:
            return None
        for root in self._roots:
            try:
                target.relative_to(root.path)
            except ValueError:
                continue
            return root
        return None


@pytest.mark.asyncio
async def test_b331_write_outside_workspace_logs_warning(
    tmp_path: Path, caplog,
) -> None:
    """Pre-B-331 a file_write to a path outside the configured
    workspace was silent — no daemon-log breadcrumb. Now it emits a
    WARNING that names the op + the resolved path + the configured
    roots so an operator auditing daemon.log can spot it."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    outside_path = tmp_path / "outside" / "leaked.txt"
    outside_path.parent.mkdir()

    mgr = _StubWorkspaceManager([workspace_root.resolve()])
    tools = BuiltinTools(
        workspace_manager_provider=lambda: mgr,
    )

    call = ToolCall(
        id="c1", name="file_write",
        args={"path": str(outside_path), "content": "leaked!"},
        provenance="anthropic",
    )
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.providers.tool.builtin",
    ):
        result = await tools.invoke(call)

    # Visibility-only: write still succeeded.
    assert result.ok is True
    assert outside_path.read_text(encoding="utf-8") == "leaked!"

    msgs = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
    assert any(
        "tool.write_outside_workspace" in m
        and "file_write" in m
        and "leaked.txt" in m
        for m in msgs
    ), f"expected workspace-containment warning, got: {msgs!r}"


@pytest.mark.asyncio
async def test_b331_write_inside_workspace_silent(
    tmp_path: Path, caplog,
) -> None:
    """Happy path: when the write is inside a configured root, no
    warning fires. Otherwise enabling the audit would flood the log
    on normal operation."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    target = workspace_root / "ok.txt"

    mgr = _StubWorkspaceManager([workspace_root.resolve()])
    tools = BuiltinTools(workspace_manager_provider=lambda: mgr)

    call = ToolCall(
        id="c1", name="file_write",
        args={"path": str(target), "content": "ok"},
        provenance="anthropic",
    )
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.providers.tool.builtin",
    ):
        result = await tools.invoke(call)

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "ok"
    msgs = [r.getMessage() for r in caplog.records]
    assert not any(
        "tool.write_outside_workspace" in m for m in msgs
    ), f"unexpected warning on inside-workspace write: {msgs!r}"


@pytest.mark.asyncio
async def test_b331_no_provider_no_warning(tmp_path: Path, caplog) -> None:
    """Tests / echo-mode that build BuiltinTools without a workspace
    manager provider must not see any warnings — the audit is opt-in
    via the provider kwarg, default off."""
    target = tmp_path / "out.txt"
    tools = BuiltinTools()  # no workspace_manager_provider
    call = ToolCall(
        id="c1", name="file_write",
        args={"path": str(target), "content": "x"},
        provenance="anthropic",
    )
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.providers.tool.builtin",
    ):
        result = await tools.invoke(call)

    assert result.ok is True
    msgs = [r.getMessage() for r in caplog.records]
    assert not any(
        "tool.write_outside_workspace" in m for m in msgs
    ), msgs


@pytest.mark.asyncio
async def test_b331_no_roots_no_warning(tmp_path: Path, caplog) -> None:
    """Fresh install — provider exists but the manager reports zero
    configured roots. Don't spam the log on every write before the
    user has actually picked a workspace."""
    target = tmp_path / "out.txt"
    mgr = _StubWorkspaceManager([])  # no roots
    tools = BuiltinTools(workspace_manager_provider=lambda: mgr)
    call = ToolCall(
        id="c1", name="file_write",
        args={"path": str(target), "content": "x"},
        provenance="anthropic",
    )
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.providers.tool.builtin",
    ):
        result = await tools.invoke(call)

    assert result.ok is True
    msgs = [r.getMessage() for r in caplog.records]
    assert not any(
        "tool.write_outside_workspace" in m for m in msgs
    )


@pytest.mark.asyncio
async def test_b331_apply_patch_also_audited(tmp_path: Path, caplog) -> None:
    """apply_patch is a write op too — same audit applies."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hello world\n", encoding="utf-8")

    mgr = _StubWorkspaceManager([workspace_root.resolve()])
    tools = BuiltinTools(workspace_manager_provider=lambda: mgr)

    call = ToolCall(
        id="c1", name="apply_patch",
        args={
            "path": str(outside),
            "edits": [{"old_text": "hello", "new_text": "GREETINGS"}],
        },
        provenance="anthropic",
    )
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.providers.tool.builtin",
    ):
        result = await tools.invoke(call)

    assert result.ok is True
    assert "GREETINGS" in outside.read_text(encoding="utf-8")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "tool.write_outside_workspace" in m and "apply_patch" in m
        for m in msgs
    ), f"apply_patch should also surface; got: {msgs!r}"


@pytest.mark.asyncio
async def test_b331_file_delete_audited(tmp_path: Path, caplog) -> None:
    """file_delete is the most destructive write — extra valuable to
    surface when the agent reaches outside the workspace."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    outside = tmp_path / "outside_to_delete.txt"
    outside.write_text("doomed", encoding="utf-8")

    mgr = _StubWorkspaceManager([workspace_root.resolve()])
    tools = BuiltinTools(workspace_manager_provider=lambda: mgr)

    call = ToolCall(
        id="c1", name="file_delete",
        args={"path": str(outside)},
        provenance="anthropic",
    )
    with caplog.at_level(
        _logging.WARNING, logger="xmclaw.providers.tool.builtin",
    ):
        result = await tools.invoke(call)

    assert result.ok is True
    assert not outside.exists()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "tool.write_outside_workspace" in m and "file_delete" in m
        for m in msgs
    ), f"file_delete should also surface; got: {msgs!r}"


def test_b331_resolve_path_to_root_now_has_callers() -> None:
    """B-331 closes the audit's ``0 callers`` complaint — both
    BuiltinTools (via _audit_workspace_containment) and the daemon
    factory's _workspace_manager_provider now touch the manager.
    This test is paranoia: a future refactor that removes the audit
    must also update the comment that justified adding the helper.
    """
    import inspect

    src = inspect.getsource(BuiltinTools._audit_workspace_containment)
    assert "resolve_path_to_root" in src, (
        "BuiltinTools._audit_workspace_containment must call "
        "WorkspaceManager.resolve_path_to_root or the helper "
        "is back to having zero callers"
    )
