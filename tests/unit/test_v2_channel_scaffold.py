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
    ("telegram", "TelegramAdapter", "Telegram"),
    ("dingtalk", "DingTalkAdapter", "DingTalk"),
    ("wecom", "WeComAdapter", "WeCom"),
    ("weixin", "WeChatAdapter", "WeChat"),
]


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
