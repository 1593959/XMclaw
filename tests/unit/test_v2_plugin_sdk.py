"""Epic #2 — plugin SDK surface-freeze + isolation guard tests.

Two concerns:

1. The public re-export list (``xmclaw.plugin_sdk.__all__``) must match
   ``FROZEN_SURFACE`` exactly. Adding a name without bumping the tuple
   is a compatibility leak; removing one silently is worse. This keeps
   both in lock-step so a CHANGELOG entry is forced.

2. Every symbol in ``__all__`` must actually import and be the *same
   object* as the canonical definition. Shadowing (re-defining
   ``ToolCall`` here) would break ``isinstance`` checks in plugins —
   catch that at test time.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import subprocess
import sys
import textwrap

import pytest


def test_all_matches_frozen_surface() -> None:
    """``FROZEN_SURFACE`` is the compatibility anchor; bumping one
    without the other is a CHANGELOG miss waiting to happen."""
    from xmclaw import plugin_sdk

    assert tuple(sorted(plugin_sdk.__all__)) == plugin_sdk.FROZEN_SURFACE


def test_every_exported_name_resolves() -> None:
    """No stale names in ``__all__`` — every entry must be importable."""
    from xmclaw import plugin_sdk

    for name in plugin_sdk.__all__:
        assert hasattr(plugin_sdk, name), f"{name} in __all__ but not exported"


def test_exports_are_canonical_identities() -> None:
    """``plugin_sdk.X`` must *be* the canonical ``xmclaw.Y.X`` object —
    re-defining a type here would break isinstance checks silently."""
    from xmclaw import plugin_sdk
    from xmclaw.core.bus.events import BehavioralEvent, EventType
    from xmclaw.core.ir import ToolCall, ToolCallShape, ToolResult, ToolSpec
    from xmclaw.providers.channel.base import (
        ChannelAdapter, ChannelTarget, InboundMessage, OutboundMessage,
    )
    from xmclaw.providers.llm.base import (
        LLMChunk, LLMProvider, LLMResponse, Message, Pricing,
    )
    from xmclaw.providers.memory.base import MemoryItem, MemoryProvider
    from xmclaw.providers.runtime.base import SkillRuntime
    from xmclaw.providers.tool.base import ToolProvider
    from xmclaw.skills.base import Skill, SkillInput, SkillOutput

    canonical = {
        "BehavioralEvent": BehavioralEvent, "EventType": EventType,
        "ToolCall": ToolCall, "ToolCallShape": ToolCallShape,
        "ToolResult": ToolResult, "ToolSpec": ToolSpec,
        "ChannelAdapter": ChannelAdapter, "ChannelTarget": ChannelTarget,
        "InboundMessage": InboundMessage, "OutboundMessage": OutboundMessage,
        "LLMChunk": LLMChunk, "LLMProvider": LLMProvider,
        "LLMResponse": LLMResponse, "Message": Message, "Pricing": Pricing,
        "MemoryItem": MemoryItem, "MemoryProvider": MemoryProvider,
        "SkillRuntime": SkillRuntime, "ToolProvider": ToolProvider,
        "Skill": Skill, "SkillInput": SkillInput, "SkillOutput": SkillOutput,
    }
    for name, obj in canonical.items():
        assert getattr(plugin_sdk, name) is obj, (
            f"plugin_sdk.{name} drifted from canonical {obj.__module__}.{name}"
        )


def test_import_has_no_side_effects(tmp_path: pathlib.Path) -> None:
    """Fresh Python: ``import xmclaw.plugin_sdk`` must produce no
    stdout/stderr — a surface module shouldn't print, warn, or touch
    the filesystem on import."""
    result = subprocess.run(
        [sys.executable, "-c", "import xmclaw.plugin_sdk"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_plugin_sdk_init_is_reexports_only() -> None:
    """AGENTS.md §4 hard-no: no logic here. The module body should be
    imports and ``__all__`` / ``FROZEN_SURFACE`` assignments only.
    AST inspection enforces this mechanically."""
    import xmclaw.plugin_sdk as sdk

    source = pathlib.Path(sdk.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = (
        ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
        ast.Expr,  # docstring + string literals
        ast.ImportFrom,
    )
    for node in tree.body:
        # Docstring (Expr(Constant(str)))
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, allowed):
            continue
        pytest.fail(
            f"plugin_sdk/__init__.py contains disallowed top-level node "
            f"{type(node).__name__} at line {node.lineno}; SDK should be "
            f"re-exports only (see xmclaw/plugin_sdk/AGENTS.md §4)"
        )


# ── check_plugin_isolation.py behavior ────────────────────────────────


def _write_fake_plugin(root: pathlib.Path, body: str) -> None:
    """Lay out a ``xmclaw/plugins/fake.py`` tree under ``root`` so we
    can run the scanner against synthetic code."""
    plugins = root / "xmclaw" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "__init__.py").write_text("", encoding="utf-8")
    (plugins / "fake.py").write_text(textwrap.dedent(body), encoding="utf-8")


def test_isolation_script_passes_on_clean_sdk_only_import(
    tmp_path: pathlib.Path,
) -> None:
    """A plugin that only imports from ``xmclaw.plugin_sdk`` is clean."""
    _write_fake_plugin(tmp_path, """
        from xmclaw.plugin_sdk import Skill, SkillInput, SkillOutput

        class MyPlugin(Skill):
            async def run(self, inp):
                return SkillOutput(ok=True, result=None, side_effects=[])
    """)
    _run_scanner(tmp_path, expect_ok=True)


def test_isolation_script_rejects_core_import(tmp_path: pathlib.Path) -> None:
    """A plugin reaching into ``xmclaw.core`` is a layering leak."""
    _write_fake_plugin(tmp_path, """
        from xmclaw.core.bus.memory import InProcessEventBus  # forbidden

        class Sneaky:
            pass
    """)
    out = _run_scanner(tmp_path, expect_ok=False)
    assert "xmclaw.core.bus.memory" in out
    assert "plug" in out.lower()


def test_isolation_script_rejects_providers_import(tmp_path: pathlib.Path) -> None:
    """Same rule for ``xmclaw.providers`` — that's the internal ABCs'
    home, plugins should go through the SDK re-export."""
    _write_fake_plugin(tmp_path, """
        from xmclaw.providers.llm.base import LLMProvider  # forbidden
    """)
    out = _run_scanner(tmp_path, expect_ok=False)
    assert "xmclaw.providers.llm.base" in out


def test_isolation_script_allows_plugin_sibling_import(
    tmp_path: pathlib.Path,
) -> None:
    """Plugins referring to their own helpers (``xmclaw.plugins.shared``)
    is fine — the rule only fires on *other* xmclaw subpackages."""
    _write_fake_plugin(tmp_path, """
        from xmclaw.plugins.shared import helper  # allowed sibling
        from xmclaw.plugin_sdk import Skill
    """)
    # Create the sibling so AST parse is clean.
    (tmp_path / "xmclaw" / "plugins" / "shared.py").write_text(
        "def helper(): pass\n", encoding="utf-8"
    )
    _run_scanner(tmp_path, expect_ok=True)


def test_isolation_script_exempts_loader_module(tmp_path: pathlib.Path) -> None:
    """``loader.py`` is plugin *machinery*, not a plugin — it's allowed to
    reach into xmclaw internals (to wire the bus, resolve entry points,
    etc.). The scanner must skip it."""
    plugins = tmp_path / "xmclaw" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "__init__.py").write_text("", encoding="utf-8")
    (plugins / "loader.py").write_text(textwrap.dedent("""
        from xmclaw.core.bus.memory import InProcessEventBus  # machinery-allowed
    """), encoding="utf-8")
    _run_scanner(tmp_path, expect_ok=True)


def test_isolation_script_reports_scan_count_on_clean(
    tmp_path: pathlib.Path,
) -> None:
    """On clean pass, the OK line should tell the reader how many files
    were checked — otherwise an empty directory looks identical to a
    directory scanned with zero violations."""
    _write_fake_plugin(tmp_path, """
        from xmclaw.plugin_sdk import Skill
    """)
    out = _run_scanner(tmp_path, expect_ok=True)
    assert "plugin file" in out or "plugin_files" in out or "scanned" in out


# ── helpers ──────────────────────────────────────────────────────────


def _run_scanner(tmp_root: pathlib.Path, *, expect_ok: bool) -> str:
    """Execute ``check_plugin_isolation.py`` as if it were rooted at
    ``tmp_root`` (its ``ROOT`` resolves to the parent of the script's
    location, so copy the script into ``tmp_root/scripts/``).
    """
    real_script = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "scripts" / "check_plugin_isolation.py"
    )
    staged = tmp_root / "scripts" / "check_plugin_isolation.py"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(real_script.read_bytes())
    result = subprocess.run(
        [sys.executable, str(staged)],
        capture_output=True, text=True, cwd=tmp_root, timeout=15,
    )
    output = result.stdout + result.stderr
    if expect_ok:
        assert result.returncode == 0, (
            f"expected OK but got rc={result.returncode}:\n{output}"
        )
    else:
        assert result.returncode == 1, (
            f"expected FAIL but got rc={result.returncode}:\n{output}"
        )
    return output


def test_real_tree_is_clean() -> None:
    """Regression guard: the actual ``xmclaw/plugins/`` tree in this
    checkout must pass the isolation scan. Any future plugin that
    sneaks a direct internal import past review gets caught here
    even if the CI hook is removed."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    script = repo_root / "scripts" / "check_plugin_isolation.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, cwd=repo_root, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_plugin_sdk_is_documented() -> None:
    """The AGENTS.md contract doc must exist alongside ``__init__.py``."""
    import xmclaw.plugin_sdk as sdk

    agents_md = pathlib.Path(sdk.__file__).parent / "AGENTS.md"
    assert agents_md.exists(), "xmclaw/plugin_sdk/AGENTS.md is missing"
    text = agents_md.read_text(encoding="utf-8")
    # Sanity: the contract must mention the key terms so a grep for them
    # lands here, not somewhere more confusing.
    assert "check_plugin_isolation" in text
    assert "plugin_sdk" in text


def test_importlib_fresh_reload_round_trips() -> None:
    """Invalidate caches + re-import — the module must remain well-formed
    after a reload (catches ordering issues in future edits)."""
    import xmclaw.plugin_sdk as sdk  # noqa: F401

    importlib.invalidate_caches()
    reloaded = importlib.reload(sdk)
    assert tuple(sorted(reloaded.__all__)) == reloaded.FROZEN_SURFACE
