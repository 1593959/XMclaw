"""B-298: pin the recursive walker that finds the SkillToolProvider
inside the agent's tool stack.

The whole evolution chain (B-294 trigger, B-295 selector, B-296
per-skill HEAD lookup) was structurally wired but **functionally
inert** in production because ``app.create_app``'s lifespan looked
for the SkillToolProvider via ``getattr(_candidate, "_providers",
None)`` — a name CompositeToolProvider never exposed (it stores
children in ``_children`` / ``children``). The lookup silently
returned ``None`` → no registry injected, no VariantSelector
started, no proposals ever generated.

These tests lock down:

* attribute-name resolution: walker finds children via the public
  ``children`` accessor first, falling back to private ``_children``
  and the legacy ``_providers`` name (so a future subclass that
  intentionally goes back to ``_providers`` doesn't silently break);
* multi-level nesting: walker reaches a SkillToolProvider that's
  two levels deep inside nested CompositeToolProviders (the
  factory's actual layout when memory bridges are wired);
* termination on cycles: walker doesn't infinite-loop if a
  malformed provider tree contains a self-reference;
* graceful degradation: ``(None, None)`` for an echo-mode agent
  with no SkillToolProvider in the tree.
"""
from __future__ import annotations

from typing import Any


from xmclaw.daemon.app import _find_skill_provider


class _FakeRegistry:
    """Minimal duck-type for the walker's ``hasattr(reg, "list_skill_ids")``
    check. Real SkillRegistry has more, but the walker only inspects
    this one method to distinguish "skill registry" from "anything
    else attached as ``_registry``".
    """

    def list_skill_ids(self) -> list[str]:
        return []


class _FakeSkillProvider:
    """Stand-in for SkillToolProvider — only needs the ``_registry``
    attribute the walker looks for."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry


class _PublicChildrenComposite:
    """Provider that exposes children through the public ``children``
    property — the walker should prefer this over private attrs."""

    def __init__(self, *kids: Any) -> None:
        self._kids = list(kids)

    @property
    def children(self) -> list[Any]:
        return list(self._kids)


class _PrivateChildrenComposite:
    """Mirrors CompositeToolProvider's actual storage (``_children``)."""

    def __init__(self, *kids: Any) -> None:
        self._children = list(kids)


class _LegacyProvidersComposite:
    """Pre-B-298 attribute name; kept supported via the third
    fallback so an external custom ToolProvider that uses
    ``_providers`` still works."""

    def __init__(self, *kids: Any) -> None:
        self._providers = list(kids)


# ── attribute resolution ────────────────────────────────────────────


def test_b298_finds_provider_via_public_children() -> None:
    reg = _FakeRegistry()
    stp = _FakeSkillProvider(reg)
    root = _PublicChildrenComposite(stp)
    found_stp, found_reg = _find_skill_provider(root)
    assert found_stp is stp
    assert found_reg is reg


def test_b298_finds_provider_via_private_children() -> None:
    """The actual CompositeToolProvider name."""
    reg = _FakeRegistry()
    stp = _FakeSkillProvider(reg)
    root = _PrivateChildrenComposite(stp)
    found_stp, found_reg = _find_skill_provider(root)
    assert found_stp is stp
    assert found_reg is reg


def test_b298_finds_provider_via_legacy_providers() -> None:
    """Backwards-compat: ``_providers`` is still walked as last
    fallback so an external provider that uses the old name
    doesn't silently disappear from the lookup."""
    reg = _FakeRegistry()
    stp = _FakeSkillProvider(reg)
    root = _LegacyProvidersComposite(stp)
    found_stp, found_reg = _find_skill_provider(root)
    assert found_stp is stp
    assert found_reg is reg


# ── multi-level nesting (the production layout) ─────────────────────


def test_b298_finds_provider_two_levels_deep() -> None:
    """Mirrors factory.py:1141 — outer composite wraps an inner
    composite that holds the SkillToolProvider. The pre-B-298
    single-level lookup couldn't see this far."""
    reg = _FakeRegistry()
    stp = _FakeSkillProvider(reg)
    inner = _PrivateChildrenComposite(stp, "filler-tool")
    outer = _PrivateChildrenComposite(inner, "memory-bridge")
    found_stp, found_reg = _find_skill_provider(outer)
    assert found_stp is stp
    assert found_reg is reg


def test_b298_finds_provider_through_mixed_attribute_names() -> None:
    """A future hybrid where the outer uses ``children`` (public)
    and the inner uses ``_children`` (private) must still resolve."""
    reg = _FakeRegistry()
    stp = _FakeSkillProvider(reg)
    inner = _PrivateChildrenComposite(stp)
    outer = _PublicChildrenComposite(inner)
    found_stp, found_reg = _find_skill_provider(outer)
    assert found_stp is stp
    assert found_reg is reg


# ── safety / edge cases ─────────────────────────────────────────────


def test_b298_returns_none_for_no_skill_provider() -> None:
    """Echo-mode daemon: agent has tools but none own a SkillRegistry.
    Walker must return (None, None), not raise — the lifespan blocks
    rely on that to skip wiring without exception handling."""
    root = _PrivateChildrenComposite("just-a-string", "another-tool")
    found_stp, found_reg = _find_skill_provider(root)
    assert found_stp is None
    assert found_reg is None


def test_b298_returns_none_for_none_root() -> None:
    """``getattr(agent, "_tools", None)`` on an echo-mode AgentLoop
    can be None — walker must accept that."""
    found_stp, found_reg = _find_skill_provider(None)
    assert found_stp is None
    assert found_reg is None


def test_b298_terminates_on_cycle() -> None:
    """Pathological provider tree with a self-reference — walker
    uses ``id()`` set to detect already-visited nodes, so this
    must complete (not hang the daemon startup)."""
    reg = _FakeRegistry()
    stp = _FakeSkillProvider(reg)
    cyclic = _PrivateChildrenComposite(stp)
    # Self-reference: cyclic._children appends itself.
    cyclic._children.append(cyclic)
    found_stp, found_reg = _find_skill_provider(cyclic)
    assert found_stp is stp
    assert found_reg is reg


def test_b298_skips_attribute_named_registry_but_not_skill_registry() -> None:
    """Some non-SkillRegistry providers might also expose a
    ``_registry`` attribute (e.g. a tool that holds a config
    registry). The walker filters by ``hasattr(reg, "list_skill_ids")``
    so those don't get picked up by accident."""

    class _NotASkillRegistry:
        # No list_skill_ids method.
        pass

    class _DecoyProvider:
        def __init__(self) -> None:
            self._registry = _NotASkillRegistry()

    real_reg = _FakeRegistry()
    real_stp = _FakeSkillProvider(real_reg)
    decoy = _DecoyProvider()
    # Decoy comes first in the children list — the walker should
    # walk past it (no list_skill_ids) and find the real one.
    root = _PrivateChildrenComposite(decoy, real_stp)
    found_stp, found_reg = _find_skill_provider(root)
    assert found_stp is real_stp
    assert found_reg is real_reg


# ── integration: real CompositeToolProvider + SkillToolProvider ────


def test_b298_works_with_real_composite_and_skill_provider() -> None:
    """Smoke test against the actual production classes — guards
    against the contract drifting (e.g. CompositeToolProvider
    renaming ``_children`` again in a future refactor)."""
    from xmclaw.providers.tool.composite import CompositeToolProvider
    from xmclaw.providers.tool.builtin import BuiltinTools
    from xmclaw.skills.tool_bridge import SkillToolProvider
    from xmclaw.skills.registry import SkillRegistry

    reg = SkillRegistry()
    stp = SkillToolProvider(registry=reg)
    bt = BuiltinTools(allowed_dirs=[])
    inner = CompositeToolProvider(bt, stp)

    # Mirror factory.py's outer wrapping. The "bridge" can be any
    # provider that exposes list_tools(); we use a stub.
    class _StubBridge:
        def list_tools(self) -> list:  # noqa: D401
            return []

    outer = CompositeToolProvider(inner, _StubBridge())
    found_stp, found_reg = _find_skill_provider(outer)
    assert found_stp is stp
    assert found_reg is reg
