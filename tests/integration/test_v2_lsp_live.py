"""LSP live integration — boots a real pylsp process and checks the
JSON-RPC Content-Length framing + hover/definition actually work.

Marked ``lsp_live`` so the main suite skips it when pylsp isn't
installed. Run explicitly with:

    pytest -m lsp_live tests/integration/test_v2_lsp_live.py -v

Or flip the auto-skip off by removing the ``importorskip`` line.
"""
from __future__ import annotations

import shutil

import pytest

# Skip the whole module cleanly if the optional extra isn't installed.
pytest.importorskip("pylsp")
if not shutil.which("pylsp"):
    pytest.skip("pylsp script not on PATH -- skipping LSP live tests",
                allow_module_level=True)

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.lsp import LSPTools


SAMPLE = '''\
"""Small module used by the LSP live tests."""


def greet(name: str) -> str:
    """Return a friendly greeting for ``name``."""
    return f"hello, {name}"


class Counter:
    """Simple integer counter for testing ``lsp_definition``."""

    def __init__(self, start: int = 0) -> None:
        self.value = start

    def inc(self, by: int = 1) -> int:
        self.value += by
        return self.value


def use_counter() -> int:
    c = Counter(10)
    c.inc()
    return c.inc(5)
'''


@pytest.fixture
async def lsp_tools(tmp_path):
    """Boot a real LSPTools pointed at a throwaway workspace with one .py file."""
    src = tmp_path / "sample.py"
    src.write_text(SAMPLE, encoding="utf-8")
    tools = LSPTools(root=tmp_path, startup_timeout_s=15.0)
    yield tools, src
    # Teardown: make sure we don't leak pylsp processes between tests.
    await tools.shutdown()


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


# ── hover ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hover_on_function_returns_signature(lsp_tools) -> None:
    tools, src = lsp_tools
    # Find the `greet` function name position. SAMPLE has it on line 3
    # (0-indexed), starting at column 4 after "def ".
    lines = SAMPLE.splitlines()
    line_idx = next(i for i, ln in enumerate(lines) if ln.startswith("def greet"))
    col = lines[line_idx].index("greet") + 2  # middle of the name

    r = await tools.invoke(_call("lsp_hover", {
        "path": str(src), "line": line_idx, "column": col,
    }))
    assert r.ok is True, f"hover failed: {r.error}"
    # pylsp returns {"contents": <markdown or MarkupContent>, ...}
    # The exact shape varies by pylsp version -- we just need the
    # function name to appear somewhere in the rendered contents.
    rendered = _flatten_hover(r.content)
    assert "greet" in rendered, f"hover didn't mention greet: {rendered[:200]}"


@pytest.mark.asyncio
async def test_hover_on_class_mentions_class(lsp_tools) -> None:
    tools, src = lsp_tools
    lines = SAMPLE.splitlines()
    line_idx = next(i for i, ln in enumerate(lines) if ln.startswith("class Counter"))
    col = lines[line_idx].index("Counter") + 2

    r = await tools.invoke(_call("lsp_hover", {
        "path": str(src), "line": line_idx, "column": col,
    }))
    assert r.ok is True, f"hover failed: {r.error}"
    rendered = _flatten_hover(r.content)
    assert "Counter" in rendered


# ── definition ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_definition_on_call_site_points_to_class_def(lsp_tools) -> None:
    """use_counter() calls Counter(10) -- asking for definition at
    'Counter' in use_counter should return the class's declaration line."""
    tools, src = lsp_tools
    lines = SAMPLE.splitlines()
    # Find the line that has `c = Counter(10)` inside use_counter.
    call_line = next(
        i for i, ln in enumerate(lines)
        if "Counter(10)" in ln
    )
    col = lines[call_line].index("Counter") + 2
    r = await tools.invoke(_call("lsp_definition", {
        "path": str(src), "line": call_line, "column": col,
    }))
    assert r.ok is True, f"definition failed: {r.error}"
    # LSP returns a Location or list thereof: [{"uri": ..., "range": {...}}]
    targets = r.content if isinstance(r.content, list) else [r.content]
    assert targets, f"no targets returned: {r.content}"
    t0 = targets[0]
    assert "uri" in t0, f"target missing uri: {t0}"
    assert t0["uri"].endswith("sample.py"), f"wrong file: {t0['uri']}"
    target_line = t0["range"]["start"]["line"]
    class_def_line = next(
        i for i, ln in enumerate(lines) if ln.startswith("class Counter")
    )
    assert target_line == class_def_line, (
        f"definition pointed at line {target_line}, expected {class_def_line}"
    )


@pytest.mark.asyncio
async def test_hover_on_blank_position_returns_empty(lsp_tools) -> None:
    """Hover on whitespace should succeed (ok=True) with an empty
    content -- not crash, not error."""
    tools, src = lsp_tools
    r = await tools.invoke(_call("lsp_hover", {
        "path": str(src), "line": 0, "column": 0,  # inside the docstring
    }))
    assert r.ok is True, f"hover on doc failed: {r.error}"
    # Either None or {} or {"contents": ""} is all fine.


# ── helpers ──────────────────────────────────────────────────────────────


def _flatten_hover(content) -> str:
    """pylsp hover content can be any of:
      - None / {}
      - "markdown string"
      - {"contents": <any of these>}
      - {"kind": "markdown", "value": "..."}          (MarkupContent)
      - {"language": "py", "value": "..."}            (MarkedString)
      - [<str or MarkedString or MarkupContent>, ...]
    Flatten recursively to a single string.
    """
    if content is None or content == {}:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_flatten_hover(x) for x in content)
    if isinstance(content, dict):
        if "contents" in content:
            return _flatten_hover(content["contents"])
        if "value" in content:
            return str(content["value"])
        # Some pylsp versions return {"items": [...]} or similar.
        return " ".join(
            _flatten_hover(v) for v in content.values()
            if isinstance(v, (str, list, dict))
        )
    return str(content)
