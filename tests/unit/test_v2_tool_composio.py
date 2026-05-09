"""B-389 (Sprint 2) — ComposioToolProvider unit tests.

Pins for the Composio bridge:

  * Module imports without ``composio_core`` installed (lazy SDK import
    contract — the daemon must boot on installs that didn't pull the
    optional extra).
  * Constructing ``ComposioToolProvider`` does NOT touch the SDK; the
    SDK is loaded on first ``list_tools()`` / ``invoke()`` call.
  * Missing api_key raises ``ValueError`` with an actionable hint.
  * ``list_tools()`` translates Composio's OpenAI-shape function specs
    into ``ToolSpec`` correctly (wrapped + flat shapes both accepted).
  * ``invoke()`` happy path: pulls action result out of the response
    envelope and surfaces it in ``ToolResult.content``.
  * ``invoke()`` failure cases — auth / rate limit / not-found / network
    / generic — each maps to a stable classified error prefix the LLM
    can pattern-match on.
  * Tool-list cache honours TTL; refetch happens after expiry; cache
    can be disabled (ttl=0).
  * Factory wiring: enabled+api_key produces a CompositeToolProvider
    that surfaces Composio actions; disabled / missing skips cleanly;
    enabled-but-empty-api_key raises ConfigError at startup.

All tests use a fake ``ComposioToolSet`` injected via a fake ``composio``
module in ``sys.modules`` — they never touch the real SDK or network.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall


# ── fake-SDK helpers ──────────────────────────────────────────────────


class FakeToolSet:
    """A stand-in for ``composio.ComposioToolSet`` used by tests.

    Records constructor args + every ``execute_action`` call so the
    asserts can inspect what the bridge actually sent, and lets the
    test choose the response shape per call (success / failure /
    legacy success-key / non-dict).
    """

    instances: list["FakeToolSet"] = []

    def __init__(self, api_key: str, entity_id: str) -> None:
        self.api_key = api_key
        self.entity_id = entity_id
        self.tool_responses: list[Any] = []
        self.action_responses: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        FakeToolSet.instances.append(self)

    def get_tools(self, apps: list[str]) -> list[Any]:
        # Pop next queued response, or default to a single Gmail action.
        if self.tool_responses:
            return self.tool_responses.pop(0)
        return _default_tools_for_apps(apps)

    def execute_action(
        self, action: str, params: dict[str, Any], entity_id: str,
    ) -> Any:
        self.calls.append({
            "action": action, "params": params, "entity_id": entity_id,
        })
        if self.action_responses:
            response = self.action_responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {"successful": True, "data": {"echo": params}}


def _default_tools_for_apps(apps: list[str]) -> list[Any]:
    """Synthetic OpenAI-shape function specs keyed off app slugs."""
    out: list[dict[str, Any]] = []
    for app in apps:
        out.append({
            "type": "function",
            "function": {
                "name": f"{app}_DO_THING",
                "description": f"Do the thing in {app}.",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
        })
    return out


@pytest.fixture
def fake_composio(monkeypatch: pytest.MonkeyPatch) -> type[FakeToolSet]:
    """Install a fake ``composio`` module exposing ``ComposioToolSet``.

    Tests that DON'T want the SDK present should use the
    ``composio_missing`` fixture instead. We always reset the
    ``FakeToolSet.instances`` list so cross-test pollution can't fake
    out an earlier install hint check.
    """
    fake_module = types.ModuleType("composio")
    fake_module.ComposioToolSet = FakeToolSet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "composio", fake_module)
    FakeToolSet.instances = []
    return FakeToolSet


@pytest.fixture
def composio_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import composio`` to raise ImportError.

    Setting the entry to ``None`` makes ``import composio`` raise
    ``ImportError: import of composio halted; None in sys.modules`` —
    Python's standard "block this module" idiom.
    """
    monkeypatch.setitem(sys.modules, "composio", None)


def _call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(name=name, args=args or {}, provenance="synthetic")


# ── lazy-import contract ──────────────────────────────────────────────


def test_module_imports_without_sdk(composio_missing: None) -> None:
    """The composio module must import on installs that haven't pulled
    the ``[tools-composio]`` extra. If we naively did ``import composio``
    at the top of the bridge, daemon boot would crash for every user
    who left the feature disabled — defeating the whole opt-in design."""
    # Force a fresh import after the monkeypatch.
    sys.modules.pop("xmclaw.providers.tool.composio", None)
    from xmclaw.providers.tool import composio as bridge_mod
    assert bridge_mod.ComposioToolProvider is not None


def test_constructor_does_not_load_sdk(composio_missing: None) -> None:
    """Constructing the provider must NOT call ``import composio`` —
    construction happens at daemon-factory time on every boot, and
    making it require the SDK would force every user to install
    composio-core just because the config schema knows about it."""
    sys.modules.pop("xmclaw.providers.tool.composio", None)
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test_anything", apps=["GMAIL"])
    # Internal handle stays None until first list_tools / invoke.
    assert provider._toolset is None  # type: ignore[attr-defined]


def test_list_tools_without_sdk_returns_empty_list(
    composio_missing: None,
) -> None:
    """``list_tools()`` is called from the system-prompt builder + every
    turn's tool advertisement. If the SDK is missing it must NOT raise:
    the daemon would crash on the first conversation. We swallow the
    ImportError and return [], deferring the loud surface to ``invoke``
    where the user actually tried to call a tool that doesn't work."""
    sys.modules.pop("xmclaw.providers.tool.composio", None)
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    assert provider.list_tools() == []


@pytest.mark.asyncio
async def test_invoke_without_sdk_classifies_install_hint(
    composio_missing: None,
) -> None:
    """When the user actually fires a Composio action without the SDK
    installed, ``invoke()`` returns a structured failure pointing at
    the missing pip extra so they know what to install."""
    sys.modules.pop("xmclaw.providers.tool.composio", None)
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    result = await provider.invoke(_call("GMAIL_SEND_EMAIL", {"to": "a@b"}))
    assert result.ok is False
    assert "composio_unavailable" in (result.error or "")
    assert "pip install" in (result.error or "")


# ── construction validation ───────────────────────────────────────────


def test_constructor_rejects_empty_api_key() -> None:
    """Empty api_key is a config bug we want to surface at construction
    time, not at first invoke. ``ConfigError`` -> startup banner; silent
    no-op -> mysterious "tool not found" later."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    with pytest.raises(ValueError, match="api_key"):
        ComposioToolProvider(api_key="", apps=["GMAIL"])
    with pytest.raises(ValueError, match="api_key"):
        ComposioToolProvider(api_key="   ", apps=["GMAIL"])


def test_constructor_normalises_apps_to_uppercase() -> None:
    """Composio's app slugs are uppercase by convention. Accepting
    lowercase / mixed-case input keeps the user-facing config forgiving
    while still feeding the SDK its expected shape."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(
        api_key="ck_test", apps=["gmail", "  Slack  ", "GITHUB"],
    )
    assert provider._apps == ("GMAIL", "SLACK", "GITHUB")  # type: ignore[attr-defined]


def test_constructor_filters_non_string_apps() -> None:
    """A user editing config.json by hand might leave a stray null /
    number in apps[]. Drop those silently rather than crashing the
    bridge — same forgiveness posture as voice / lsp blocks."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(
        api_key="ck_test",
        apps=["GMAIL", "", None, 42, "SLACK"],  # type: ignore[list-item]
    )
    assert provider._apps == ("GMAIL", "SLACK")  # type: ignore[attr-defined]


def test_constructor_default_entity_id_when_blank() -> None:
    """Empty entity_id falls back to ``"default"`` — matching the
    Composio convention for single-tenant installs."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    p1 = ComposioToolProvider(api_key="ck_test", entity_id="", apps=["GMAIL"])
    p2 = ComposioToolProvider(api_key="ck_test", entity_id="   ", apps=["GMAIL"])
    p3 = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    assert p1._entity_id == "default"  # type: ignore[attr-defined]
    assert p2._entity_id == "default"  # type: ignore[attr-defined]
    assert p3._entity_id == "default"  # type: ignore[attr-defined]


# ── list_tools ────────────────────────────────────────────────────────


def test_list_tools_translates_openai_function_shape(
    fake_composio: type[FakeToolSet],
) -> None:
    """Composio returns OpenAI Chat Completions function specs:
    ``{"type": "function", "function": {name, description, parameters}}``.
    Each maps to a XMclaw ToolSpec verbatim."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL", "SLACK"])
    specs = provider.list_tools()
    names = sorted(s.name for s in specs)
    assert names == ["GMAIL_DO_THING", "SLACK_DO_THING"]
    gmail = next(s for s in specs if s.name == "GMAIL_DO_THING")
    assert "Do the thing in GMAIL" in gmail.description
    assert gmail.parameters_schema["properties"]["x"]["type"] == "string"


def test_list_tools_accepts_flat_function_shape(
    fake_composio: type[FakeToolSet],
) -> None:
    """Some SDK versions return the function block flat (no outer
    ``{"type": "function", "function": ...}`` wrapper). The converter
    must handle both shapes — Composio's API has shipped both at
    different times and we don't want a minor SDK upgrade to nuke the
    catalogue."""
    fake_composio.instances.clear()
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    # Force the toolset, then queue a flat-shape response.
    provider.list_tools()  # builds toolset
    fake_composio.instances[-1].tool_responses.append([
        {"name": "FLAT_ACTION", "description": "flat shape",
         "parameters": {"type": "object", "properties": {}}},
    ])
    # Bypass cache.
    provider._cached_specs = None  # type: ignore[attr-defined]
    specs = provider.list_tools()
    assert [s.name for s in specs] == ["FLAT_ACTION"]


def test_list_tools_skips_malformed_entries(
    fake_composio: type[FakeToolSet],
) -> None:
    """Composio occasionally ships a tool with a missing name / non-dict
    parameters. Drop those individually instead of crashing the whole
    list — losing 99 working tools because tool 100 was malformed is
    the wrong tradeoff."""
    fake_composio.instances.clear()
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[-1].tool_responses.append([
        {"function": {"name": "OK_TOOL", "description": "good",
                      "parameters": {"type": "object"}}},
        "not a dict",
        {"function": {"name": "", "description": "blank name"}},
        {"function": {"name": "NEXT_OK", "description": "also good",
                      "parameters": {"type": "object"}}},
        {"function": {"description": "no name"}},
    ])
    provider._cached_specs = None  # type: ignore[attr-defined]
    specs = provider.list_tools()
    assert [s.name for s in specs] == ["OK_TOOL", "NEXT_OK"]


def test_list_tools_empty_apps_returns_empty(
    fake_composio: type[FakeToolSet],
) -> None:
    """No apps configured == no Composio tools surface. Catches the
    ``enabled=true, apps=[]`` foot-gun without raising."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=[])
    assert provider.list_tools() == []


def test_list_tools_caches_within_ttl(
    fake_composio: type[FakeToolSet],
) -> None:
    """Hit Composio once, serve subsequent calls from the cache. The
    agent loop calls list_tools per turn; without this cache every
    turn would burn an HTTP round-trip + a quota slot."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(
        api_key="ck_test", apps=["GMAIL"], cache_ttl_s=300.0,
    )
    a = provider.list_tools()
    b = provider.list_tools()
    c = provider.list_tools()
    assert a == b == c
    # Only one toolset instance ever — and it was hit once.
    assert len(fake_composio.instances) == 1


def test_list_tools_refetches_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
    fake_composio: type[FakeToolSet],
) -> None:
    """When TTL elapses we refetch so a freshly-authorised app
    appears in the agent's toolset on the next list_tools call."""
    import xmclaw.providers.tool.composio as bridge_mod
    fake_composio.instances.clear()
    # ``now`` is mutable so the test can advance the clock between
    # list_tools calls deterministically.
    now = {"v": 100.0}
    monkeypatch.setattr(
        bridge_mod.time, "monotonic", lambda: now["v"],
    )
    provider = bridge_mod.ComposioToolProvider(
        api_key="ck_test", apps=["GMAIL"], cache_ttl_s=10.0,
    )
    # First call at t=100: primes cache, _cached_at = 100.
    first = provider.list_tools()
    assert provider._cached_at == 100.0  # type: ignore[attr-defined]
    assert {s.name for s in first} == {"GMAIL_DO_THING"}
    # Second call still inside TTL — cache hit, no refetch.
    now["v"] = 105.0
    provider.list_tools()
    assert provider._cached_at == 100.0  # type: ignore[attr-defined]
    # Queue a new (empty) response, advance past TTL — refetch.
    fake_composio.instances[0].tool_responses.append([])
    now["v"] = 999.0
    second = provider.list_tools()
    assert provider._cached_at == 999.0  # type: ignore[attr-defined]
    # Refetched the queued empty response, so the catalogue now reflects
    # the live state at t=999 — proves get_tools was actually re-invoked.
    assert second == []


def test_list_tools_zero_ttl_disables_cache(
    fake_composio: type[FakeToolSet],
) -> None:
    """TTL=0 means refetch every time — useful in tests."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(
        api_key="ck_test", apps=["GMAIL"], cache_ttl_s=0.0,
    )
    # First call constructs toolset; we observe via execute_action which
    # we don't care about here, so just call list_tools twice with
    # different queued results to confirm the second result wins.
    provider.list_tools()
    fake_composio.instances[0].tool_responses.append([])
    assert provider.list_tools() == []


def test_list_tools_swallows_sdk_init_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``ComposioToolSet(...)`` raises (bad credentials, network
    blip), list_tools must NOT propagate — daemon boot would crash for
    a user who typoed their api_key. Returns []; the loud surface is
    in invoke()."""
    fake_module = types.ModuleType("composio")

    class BoomToolSet:
        def __init__(self, **_: Any) -> None:
            raise RuntimeError("invalid credentials")

    fake_module.ComposioToolSet = BoomToolSet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "composio", fake_module)
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    assert provider.list_tools() == []


# ── invoke ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_happy_path(
    fake_composio: type[FakeToolSet],
) -> None:
    """A successful Composio action returns ``{successful: True,
    data: <payload>}``; we surface the payload as ToolResult.content
    and pass the action name + params + entity_id to execute_action."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(
        api_key="ck_test", entity_id="alice", apps=["GMAIL"],
    )
    # Force toolset construction.
    provider.list_tools()
    inst = fake_composio.instances[0]
    inst.action_responses.append({
        "successful": True,
        "data": {"id": "msg-123", "thread_id": "t-1"},
    })
    result = await provider.invoke(_call("GMAIL_SEND_EMAIL", {
        "to": "a@b.com", "subject": "hi", "body": "hello",
    }))
    assert result.ok is True
    assert result.content == {"id": "msg-123", "thread_id": "t-1"}
    assert inst.calls[-1]["action"] == "GMAIL_SEND_EMAIL"
    assert inst.calls[-1]["params"]["to"] == "a@b.com"
    assert inst.calls[-1]["entity_id"] == "alice"


@pytest.mark.asyncio
async def test_invoke_legacy_success_key(
    fake_composio: type[FakeToolSet],
) -> None:
    """Older Composio SDK versions used ``"success"`` instead of
    ``"successful"``. Accept both so a user on a pinned older version
    isn't broken."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append({
        "success": True, "data": "ok",
    })
    result = await provider.invoke(_call("GMAIL_DO_THING", {"x": "y"}))
    assert result.ok is True
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_invoke_action_failure_surfaces_classified_error(
    fake_composio: type[FakeToolSet],
) -> None:
    """A non-success response with a Composio error_code becomes a
    ToolResult(ok=False) with a stable error prefix the LLM can match."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append({
        "successful": False,
        "error": "no integration configured for entity 'default'",
        "error_code": "INTEGRATION_NOT_FOUND",
    })
    result = await provider.invoke(_call("GMAIL_SEND_EMAIL", {}))
    assert result.ok is False
    assert "composio_action_failed[INTEGRATION_NOT_FOUND]" in (result.error or "")
    assert "no integration" in (result.error or "")


@pytest.mark.asyncio
async def test_invoke_classifies_auth_exception(
    fake_composio: type[FakeToolSet],
) -> None:
    """SDK raising ``Exception("401 Unauthorized")`` -> error string
    starts with ``composio_auth_error``. The LLM prompt can include
    "if you see composio_auth_error, ask the user to refresh keys"
    once these prefixes are stable."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append(
        RuntimeError("401 Unauthorized — invalid api key"),
    )
    result = await provider.invoke(_call("GMAIL_DO_THING", {}))
    assert result.ok is False
    assert (result.error or "").startswith("composio_auth_error:")


@pytest.mark.asyncio
async def test_invoke_classifies_rate_limit(
    fake_composio: type[FakeToolSet],
) -> None:
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append(
        RuntimeError("429 Too Many Requests — rate limit hit"),
    )
    result = await provider.invoke(_call("GMAIL_DO_THING", {}))
    assert result.ok is False
    assert (result.error or "").startswith("composio_rate_limited:")


@pytest.mark.asyncio
async def test_invoke_classifies_action_not_found(
    fake_composio: type[FakeToolSet],
) -> None:
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append(
        RuntimeError("No such action: TOTALLY_FAKE_ACTION"),
    )
    result = await provider.invoke(_call("TOTALLY_FAKE_ACTION", {}))
    assert result.ok is False
    assert (result.error or "").startswith("composio_action_not_found:")


@pytest.mark.asyncio
async def test_invoke_classifies_network_error(
    fake_composio: type[FakeToolSet],
) -> None:
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append(
        ConnectionError("Connection reset by peer"),
    )
    result = await provider.invoke(_call("GMAIL_DO_THING", {}))
    assert result.ok is False
    assert (result.error or "").startswith("composio_network_error:")


@pytest.mark.asyncio
async def test_invoke_generic_exception_falls_back_to_composio_error(
    fake_composio: type[FakeToolSet],
) -> None:
    """Anything we can't classify lands under the catch-all prefix.
    Stable so prompt instructions can rely on the prefix existing."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append(
        ValueError("something weird"),
    )
    result = await provider.invoke(_call("GMAIL_DO_THING", {}))
    assert result.ok is False
    assert (result.error or "").startswith("composio_error:")


@pytest.mark.asyncio
async def test_invoke_non_dict_response_wraps_as_success(
    fake_composio: type[FakeToolSet],
) -> None:
    """Some Composio actions return raw scalars (a count, a status
    string). The bridge accepts those — anything that isn't ``dict``
    gets wrapped as a successful result with the value as content."""
    from xmclaw.providers.tool.composio import ComposioToolProvider
    provider = ComposioToolProvider(api_key="ck_test", apps=["GMAIL"])
    provider.list_tools()
    fake_composio.instances[0].action_responses.append("plain string")
    result = await provider.invoke(_call("GMAIL_DO_THING", {}))
    assert result.ok is True
    assert result.content == "plain string"


# ── factory wiring ────────────────────────────────────────────────────


def test_factory_skips_when_disabled(fake_composio: type[FakeToolSet]) -> None:
    """``enabled: false`` (the default) skips Composio entirely. The
    provider stays cleanly out of the chain so the user never sees
    Composio tools they didn't ask for."""
    from xmclaw.daemon.factory import build_tools_from_config
    tools = build_tools_from_config({
        "tools": {
            "composio": {
                "enabled": False, "api_key": "ck_test",
                "apps": ["GMAIL"],
            },
        },
    })
    assert tools is not None
    names = {s.name for s in tools.list_tools()}
    assert not any(n.startswith("GMAIL_") for n in names)


def test_factory_skips_when_section_missing(
    fake_composio: type[FakeToolSet],
) -> None:
    """No ``composio`` block at all is also disabled. Same as above
    but covers the explicit-absence case (most users will never write
    the block)."""
    from xmclaw.daemon.factory import build_tools_from_config
    tools = build_tools_from_config({"tools": {"enable_bash": True}})
    assert tools is not None
    names = {s.name for s in tools.list_tools()}
    assert not any(n.startswith("GMAIL_") for n in names)


def test_factory_raises_config_error_when_enabled_without_api_key() -> None:
    """``enabled=true`` + empty api_key is a half-filled config — fail
    fast at startup rather than letting the daemon boot in a state
    where Composio tools are advertised but every call fails."""
    from xmclaw.daemon.factory import ConfigError, build_tools_from_config
    with pytest.raises(ConfigError, match="composio"):
        build_tools_from_config({
            "tools": {
                "composio": {"enabled": True, "api_key": "", "apps": ["GMAIL"]},
            },
        })


def test_factory_raises_config_error_when_apps_not_list() -> None:
    """``apps`` must be a list — a stray string ("GMAIL") would
    otherwise iterate as characters and break the SDK call. Caught at
    config-validation time."""
    from xmclaw.daemon.factory import ConfigError, build_tools_from_config
    with pytest.raises(ConfigError, match="composio.apps"):
        build_tools_from_config({
            "tools": {
                "composio": {
                    "enabled": True, "api_key": "ck_test",
                    "apps": "GMAIL",
                },
            },
        })


def test_factory_wires_composio_provider(
    fake_composio: type[FakeToolSet],
) -> None:
    """Happy path: enabled=true + valid api_key produces a tool chain
    whose list_tools() includes the Composio actions alongside builtin
    tools (file_read / bash / web_fetch / …)."""
    from xmclaw.daemon.factory import build_tools_from_config
    tools = build_tools_from_config({
        "tools": {
            "composio": {
                "enabled": True,
                "api_key": "ck_test",
                "entity_id": "alice",
                "apps": ["GMAIL", "SLACK"],
                "cache_ttl_s": 60,
            },
        },
    })
    assert tools is not None
    names = {s.name for s in tools.list_tools()}
    assert "file_read" in names  # builtin tools still present
    assert "GMAIL_DO_THING" in names
    assert "SLACK_DO_THING" in names
    # Toolset constructed with the right credentials.
    inst = fake_composio.instances[-1]
    assert inst.api_key == "ck_test"
    assert inst.entity_id == "alice"
