"""B-329: scaffolded channel adapters surface a clear, actionable
NotImplementedError instead of a cryptic ``ModuleNotFoundError``.

Pre-B-329 the four IM scaffolds (telegram / dingtalk / wecom /
weixin) declared ``adapter_factory_path="...adapter:Adapter"`` but
the ``adapter.py`` files didn't exist. The dispatcher's
``include_scaffolds=False`` filter kept production from hitting
this in normal config flow — but anyone using
``include_scaffolds=True`` (tests, debug tooling, future code) got a
``ModuleNotFoundError: xmclaw.providers.channel.telegram.adapter``
that didn't explain WHY the module is missing.

Now each scaffolded package ships a tiny ``adapter.py`` that
imports + raises ``NotImplementedError`` with the channel name +
port target. Manifest discovery is unaffected (manifest still lives
in ``__init__.py``).

Tests:
  * Each of the 4 manifests resolves to an importable module + class
  * Instantiating the class raises NotImplementedError with the
    channel name in the message
  * The discover() default still filters them out (no behaviour
    regression)
"""
from __future__ import annotations

import importlib

import pytest


_SCAFFOLD_CHANNELS = [
    # B-380: telegram graduated from scaffold to ready (real adapter at
    # telegram/adapter.py). B-383: dingtalk graduated (Stream Mode WS
    # adapter at dingtalk/adapter.py). B-384: wecom graduated to ready
    # (outbound-only webhook adapter at wecom/adapter.py — inbound
    # remains out-of-scope). The remaining IM scaffold keeps the same
    # pattern: manifest exists, adapter raises NotImplementedError on
    # construct so dispatcher's include_scaffolds=False filter hides
    # them from production but the registry still surfaces them in the
    # UI's "coming soon" list.
    ("weixin", "WeChatAdapter", "WeChat"),
]


# B-330: ACP is a scaffold of a slightly different shape (start()
# raises rather than __init__()) but should be hidden from production
# dispatch the same way. Tested separately below since the
# scaffold-pattern instantiation tests above don't apply.
_ACP_SCAFFOLD = ("acp", "ACPAdapter")


@pytest.mark.parametrize("channel_id, class_name, name_substring", _SCAFFOLD_CHANNELS)
def test_b329_scaffold_module_imports(
    channel_id: str, class_name: str, name_substring: str,
) -> None:
    """Pre-B-329 this would fail with ``ModuleNotFoundError`` because
    the adapter module simply didn't exist. Verifying we can resolve
    the manifest's ``adapter_factory_path`` to a real class is what
    the dispatcher's import path needs."""
    mod = importlib.import_module(
        f"xmclaw.providers.channel.{channel_id}.adapter"
    )
    cls = getattr(mod, class_name)
    assert isinstance(cls, type), (
        f"{channel_id}.{class_name} should be a class"
    )


@pytest.mark.parametrize("channel_id, class_name, name_substring", _SCAFFOLD_CHANNELS)
def test_b329_scaffold_instantiation_raises_with_clear_message(
    channel_id: str, class_name: str, name_substring: str,
) -> None:
    """The actionable part — when something does try to instantiate a
    scaffold adapter, the error must name the channel + tell the user
    what state it's in (scaffold) + how to look up the port target.
    """
    mod = importlib.import_module(
        f"xmclaw.providers.channel.{channel_id}.adapter"
    )
    cls = getattr(mod, class_name)
    with pytest.raises(NotImplementedError) as exc:
        cls({"some": "config"})

    msg = str(exc.value)
    # Channel name appears (so the operator knows WHICH channel).
    assert name_substring in msg, (
        f"error message for {channel_id} should mention {name_substring!r}; "
        f"got: {msg!r}"
    )
    # "scaffold" word appears so the state is unambiguous.
    assert "scaffold" in msg.lower()
    # Port target is mentioned so the operator can find the qwenpaw
    # reference adapter.
    assert "qwenpaw" in msg.lower(), (
        f"error message should reference the port target; got: {msg!r}"
    )


@pytest.mark.parametrize("channel_id, class_name, name_substring", _SCAFFOLD_CHANNELS)
def test_b329_manifest_factory_path_matches_module(
    channel_id: str, class_name: str, name_substring: str,
) -> None:
    """B-329 invariant: the manifest's ``adapter_factory_path`` must
    point at a real importable module + class. Pre-B-329 this was
    a dangling reference for all four scaffolds; the test pins that
    every scaffold in the package now satisfies the contract.
    """
    pkg = importlib.import_module(f"xmclaw.providers.channel.{channel_id}")
    manifest = pkg.MANIFEST
    expected_modpath = f"xmclaw.providers.channel.{channel_id}.adapter"
    expected = f"{expected_modpath}:{class_name}"
    assert manifest.adapter_factory_path == expected, (
        f"{channel_id} manifest factory path mismatch — "
        f"manifest says {manifest.adapter_factory_path!r}, "
        f"expected {expected!r}"
    )
    # And resolve it the same way dispatcher does:
    modpath, clsname = manifest.adapter_factory_path.split(":")
    mod = importlib.import_module(modpath)
    assert getattr(mod, clsname) is not None


def test_b329_discover_default_still_filters_scaffolds() -> None:
    """No behaviour regression: the registry's default
    ``include_scaffolds=False`` continues to hide scaffolds from
    production dispatch. The B-329 fix is about making the modules
    exist + the error explain itself — NOT about exposing the
    scaffolds where they shouldn't be."""
    from xmclaw.providers.channel.registry import discover

    ready = discover(include_scaffolds=False)
    for ch_id, _, _ in _SCAFFOLD_CHANNELS:
        assert ch_id not in ready, (
            f"{ch_id} is a scaffold — must NOT appear in the default "
            f"include_scaffolds=False discovery; got: {list(ready.keys())}"
        )

    # And include_scaffolds=True DOES surface them (for the UI's
    # "grayed out / coming soon" rendering).
    all_chs = discover(include_scaffolds=True)
    for ch_id, _, _ in _SCAFFOLD_CHANNELS:
        assert ch_id in all_chs, (
            f"{ch_id} should appear when include_scaffolds=True so "
            f"the Channels page can render it as scaffold"
        )


# ── B-330: ACP joins the scaffold list ─────────────────────────────


def test_b330_acp_manifest_marked_scaffold() -> None:
    """B-330: ACPAdapter.start() raises NotImplementedError because
    the hermes acp_adapter/server.py port hasn't landed yet, but
    pre-B-330 the manifest's default ``implementation_status="ready"``
    let the dispatcher try to instantiate + start ACP whenever a user
    enabled it in config — boot then crashed. The manifest now
    declares ``scaffold`` to match the actual state, joining the
    other 4 IM scaffolds in the dispatcher's default-hidden set."""
    from xmclaw.providers.channel.acp import MANIFEST
    assert MANIFEST.implementation_status == "scaffold", (
        "ACP MANIFEST must be scaffold-flagged until the Phase 6.1 "
        "hermes port lands; otherwise dispatcher will try to start() "
        "the adapter and crash on the NotImplementedError"
    )


def test_b330_acp_filtered_by_default_discover() -> None:
    """B-330: with the manifest scaffold-flagged, the registry's
    default include_scaffolds=False discovery hides ACP — same way
    the 4 IM scaffolds are hidden. Production config that enables
    ACP no longer crashes daemon boot."""
    from xmclaw.providers.channel.registry import discover

    ready = discover(include_scaffolds=False)
    assert "acp" not in ready, (
        f"ACP scaffold must not appear in default discovery; got: "
        f"{list(ready.keys())}"
    )

    all_chs = discover(include_scaffolds=True)
    assert "acp" in all_chs, (
        "ACP should still surface when include_scaffolds=True so the "
        "Channels page can render it as 'coming soon'"
    )


@pytest.mark.asyncio
async def test_b330_acp_start_still_raises_with_explanatory_message() -> None:
    """If something explicitly bypasses the scaffold filter and
    instantiates + starts ACP, the error must point at the scaffold
    state + the port reference + how to flip the status back when
    the port lands."""
    from xmclaw.providers.channel.acp import ACPAdapter

    adapter = ACPAdapter(agent_id="main")
    with pytest.raises(NotImplementedError) as exc:
        await adapter.start()
    msg = str(exc.value)
    assert "scaffold" in msg.lower()
    assert "hermes" in msg.lower()
    assert "implementation_status" in msg or "manifest" in msg.lower()
