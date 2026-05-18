"""OutputStyles — Wave-32+ (2026-05-18)."""
from __future__ import annotations

import pytest

from xmclaw.core.agent_context import use_current_session_id
from xmclaw.core.ir import ToolCall
from xmclaw.core.output_styles import (
    DEFAULT_OUTPUT_STYLE,
    clear_all_session_styles,
    get_style,
    list_styles,
    session_style,
    set_session_style,
)
from xmclaw.providers.tool.builtin import BuiltinTools


@pytest.fixture(autouse=True)
def _clean():
    clear_all_session_styles()
    yield
    clear_all_session_styles()


def _call(tool: str, **args: object) -> ToolCall:
    return ToolCall(name=tool, args=args, provenance="synthetic")


def test_builtin_styles_advertised() -> None:
    names = {s.name for s in list_styles()}
    assert {"default", "Explanatory", "Learning"} <= names


def test_get_style_unknown_falls_back_to_default() -> None:
    """Lenient lookup: unknown names should return the default style
    rather than raising — mirrors plan_mode's lenient semantics."""
    s = get_style("not-a-real-style")
    assert s.name == DEFAULT_OUTPUT_STYLE
    assert s.prompt == ""


def test_get_style_none_returns_default() -> None:
    s = get_style(None)
    assert s.name == DEFAULT_OUTPUT_STYLE


def test_session_style_defaults_to_default() -> None:
    """Sessions with no explicit style set return the default."""
    s = session_style("never-touched")
    assert s.name == DEFAULT_OUTPUT_STYLE


def test_set_session_style_and_clear() -> None:
    set_session_style("sess-A", "Explanatory")
    assert session_style("sess-A").name == "Explanatory"
    assert "Insight" in session_style("sess-A").prompt
    # Setting None clears.
    set_session_style("sess-A", None)
    assert session_style("sess-A").name == DEFAULT_OUTPUT_STYLE
    # Setting "default" also clears.
    set_session_style("sess-A", "Explanatory")
    set_session_style("sess-A", DEFAULT_OUTPUT_STYLE)
    assert session_style("sess-A").name == DEFAULT_OUTPUT_STYLE


def test_explanatory_and_learning_have_distinct_prompts() -> None:
    """Pin the two built-ins so a regression that accidentally
    collapses them to the same prompt is caught."""
    e = get_style("Explanatory")
    learn = get_style("Learning")
    assert e.prompt != learn.prompt
    assert "Learn by Doing" in learn.prompt
    assert "Insight" in e.prompt


def test_set_session_style_isolation_between_sessions() -> None:
    """A style set on one session must not leak to another."""
    set_session_style("sess-A", "Explanatory")
    assert session_style("sess-A").name == "Explanatory"
    assert session_style("sess-B").name == DEFAULT_OUTPUT_STYLE


# ── set_output_style tool ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_output_style_tool_advertised() -> None:
    tools = BuiltinTools()
    names = {s.name for s in tools.list_tools()}
    assert "set_output_style" in names


@pytest.mark.asyncio
async def test_set_output_style_tool_happy_path() -> None:
    tools = BuiltinTools()
    with use_current_session_id("sess-tool"):
        res = await tools.invoke(_call("set_output_style", name="Explanatory"))
        assert res.ok, res.error
        assert "Explanatory" in res.content
        assert session_style("sess-tool").name == "Explanatory"


@pytest.mark.asyncio
async def test_set_output_style_unknown_errors_with_choices() -> None:
    tools = BuiltinTools()
    with use_current_session_id("sess-tool2"):
        res = await tools.invoke(_call("set_output_style", name="ToyMode"))
        assert res.ok is False
        assert "unknown" in (res.error or "").lower()
        # The valid set is listed so the LLM can self-correct.
        assert "Explanatory" in (res.error or "")


@pytest.mark.asyncio
async def test_set_output_style_outside_session_errors() -> None:
    tools = BuiltinTools()
    # No use_current_session_id wrapping.
    res = await tools.invoke(_call("set_output_style", name="Learning"))
    assert res.ok is False
    assert "session" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_set_output_style_missing_name_errors() -> None:
    tools = BuiltinTools()
    with use_current_session_id("sess-tool3"):
        res = await tools.invoke(_call("set_output_style"))
        assert res.ok is False
        # Empty / whitespace also rejected.
        res2 = await tools.invoke(_call("set_output_style", name="   "))
        assert res2.ok is False


# ── REST endpoint ────────────────────────────────────────────────────────


def test_output_styles_router_list() -> None:
    from fastapi.testclient import TestClient
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app import create_app

    client = TestClient(create_app(bus=InProcessEventBus()))
    resp = client.get("/api/v2/output_styles")
    assert resp.status_code == 200
    body = resp.json()
    names = {s["name"] for s in body["styles"]}
    assert {"default", "Explanatory", "Learning"} <= names
    # Prompt body must NOT leak in list output (it can contain
    # operator customizations).
    assert all("prompt" not in s for s in body["styles"])


def test_output_styles_router_session_default() -> None:
    from fastapi.testclient import TestClient
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app import create_app

    client = TestClient(create_app(bus=InProcessEventBus()))
    resp = client.get("/api/v2/output_styles/session/never-set")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "never-set"
    assert body["style"]["name"] == DEFAULT_OUTPUT_STYLE


def test_output_styles_router_session_after_set() -> None:
    from fastapi.testclient import TestClient
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.app import create_app

    set_session_style("sess-rest", "Explanatory")
    client = TestClient(create_app(bus=InProcessEventBus()))
    resp = client.get("/api/v2/output_styles/session/sess-rest")
    body = resp.json()
    assert body["style"]["name"] == "Explanatory"
