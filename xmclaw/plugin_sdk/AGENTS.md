# xmclaw/plugin_sdk/AGENTS.md — Plugin SDK 契约 (Epic #2)

This directory is the **only** thing a third-party plugin is allowed to
import from. Everything else under `xmclaw.*` is internal and may
change without notice. The SDK surface lives in `__init__.py` and is
guarded by `scripts/check_plugin_isolation.py`.

---

## 1. 职责（Responsibility）

The single source of truth for the public API contract shown to
third-party plugins. Re-exports the ABCs a plugin must subclass
(`Skill`, `ToolProvider`, `LLMProvider`, `MemoryProvider`,
`ChannelAdapter`, `SkillRuntime`), the IR dataclasses it exchanges
with the runtime (`ToolCall`, `ToolResult`, `MemoryItem`, …), and the
read-only bus primitives (`EventType`, `BehavioralEvent`). This
directory owns **only re-exports**; it holds no logic, no state, no
helpers. Keeping it thin is what makes the compatibility promise
cheap.

## 2. 依赖规则（Dependency rules）

This module sits **above** the rest of `xmclaw/` in the DAG — it
depends on internal packages so plugins don't have to.

- ✅ MAY import: `xmclaw.core.*`, `xmclaw.providers.*`, `xmclaw.skills.*`,
  Python stdlib.
- ❌ MUST NOT import: `xmclaw.daemon.*`, `xmclaw.cli.*`. Daemon and
  CLI sit *on top of* the plugin layer; letting the SDK see them
  would make plugins implicitly depend on a daemon being present.

What **plugins** (`xmclaw/plugins/**` and third-party packages) may
import is the dual rule, enforced by
`scripts/check_plugin_isolation.py`:

- ✅ MAY import: `xmclaw.plugin_sdk.*`, Python stdlib, their own deps.
- ❌ MUST NOT import: any other `xmclaw.*` subpackage. The whole point
  of this SDK is that plugins only touch the frozen surface.

## 3. 测试入口（How to test changes here）

- Unit: `tests/unit/test_v2_plugin_sdk.py` (surface-freeze + export
  parity).
- CI guard: `python scripts/check_plugin_isolation.py` walks
  `xmclaw/plugins/**` and fails on any import outside `xmclaw.plugin_sdk`
  or stdlib.
- Smart-gate lane: `plugin_sdk` in `scripts/test_lanes.yaml` — triggers
  when `xmclaw/plugin_sdk/**`, `xmclaw/plugins/**`, or the isolation
  script changes.
- Manual: `python -c "from xmclaw.plugin_sdk import Skill, ToolProvider"`
  — a clean import should print nothing.

## 4. 禁止事项（Hard no's）

- ❌ **Never add logic here.** No classes, no functions, no module-level
  side effects. Only `from X import Y` + `__all__`. A helper belongs in
  the subpackage it helps, not here.
- ❌ **Never expose internals by accident.** Adding `from xmclaw.core.bus
  import InProcessEventBus` to `__init__.py` would hand plugins the
  ability to publish events, which Anti-req #14 forbids (plugins must
  not forge events on behalf of the daemon). Every new export needs
  explicit justification.
- ❌ **Never remove a name without a major bump.** `FROZEN_SURFACE` is
  compared against `__all__` in the test suite. If you drop a name, the
  test fails; that failure is asking for a CHANGELOG entry and a
  major-version bump, not a workaround.
- ❌ **Never shadow the canonical definition.** Re-define `ToolCall` here
  and now there are two `ToolCall` types in the codebase, which breaks
  `isinstance` checks silently. Always `from X import Y` the one true
  symbol.

## 5. 关键文件（Key files / entry points）

- `__init__.py:1` — the frozen re-export list (and `FROZEN_SURFACE`
  tuple that the test harness pins to `__all__`).
- `../../scripts/check_plugin_isolation.py` — AST scan enforcing rule
  §2 on `xmclaw/plugins/**`.
- `../../tests/unit/test_v2_plugin_sdk.py` — surface-freeze + parity
  tests. Read first before adding to `__all__`.
- `../plugins/loader.py` — the daemon-side discovery stub
  (`importlib.metadata.entry_points("xmclaw.plugins.*")`). Actual
  wiring is Phase 2.
