# AGENTS.md — `xmclaw/skills/`

## 1. 职责

Skill-authoring machinery: the `SkillBase` ABC (`base.py`), manifest
schema (`manifest.py`), registry (`registry.py`), and versioning
rules (`versioning.py`). `demo/` holds the curated skills that ship
with the repo and get exercised by integration tests.

Skills are the unit the `EvolutionController` proposes, the
`HonestGrader` judges by evidence, and the `SkillScheduler` promotes
or rolls back. Anything here must stay explicit enough for automated
mutation not to produce silent nonsense.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*` (events, grader, IR),
  `xmclaw.utils.*`, stdlib.
- ❌ MUST NOT import: `xmclaw.daemon.*`, `xmclaw.providers.*`
  (except when explicitly documented — skills are usually pure
  Python operating on IR types), `xmclaw.cli.*`.

## 3. 测试入口

- Unit: `tests/unit/test_v2_skill_registry.py`.
- Integration: `tests/integration/test_v2_autonomous_evolution.py`,
  `tests/integration/test_v2_tool_aware_skill.py`.
- Smart-gate lane: `evolution`.

## 4. 禁止事项

- ❌ Don't bump a skill's `version` without updating the manifest
  `changelog` — the registry uses that list to decide rollback
  eligibility.
- ❌ Don't rely on module-level state inside a skill. Runs happen
  in subprocesses (`providers/runtime/process.py`); globals won't
  survive serialization.
- ❌ Don't add heavyweight dependencies to `demo/` skills. Demo
  skills ship with the installer; each kB of extra dep hits every
  user.

## 5. 关键文件

- `base.py` — `SkillBase` ABC: `run(inputs) -> SkillResult`.
- `manifest.py` — pydantic model for `skill.yaml` / `skill.json`.
- `registry.py` — lookup, filter, list. Used by the scheduler.
- `versioning.py` — bump rules + changelog semantics.
- `tool_bridge.py` — `SkillToolProvider`: bridges registry HEAD
  entries into LLM-callable tools as `skill_<id>`. Hosts the
  always-exposed `skill_browse` meta-tool (B-299) — synthesised at
  index 0 in `list_tools()`, takes `query: str` + optional
  `top_k`, returns top matches via combined token+substring
  scoring. The prefilter has a hard whitelist for
  `META_BROWSE_TOOL_NAME` so it ALWAYS reaches the LLM, even on
  zero-token-overlap queries (CJK against English skill descs).
  Epic #27 G-04 (2026-05-19): adds `disclosure_mode` (`inline` /
  `unified` / `auto`, default `auto`) + `unified_threshold`
  (default 20) constructor args; in `unified` mode (or `auto` once
  registered skill count > threshold) per-skill `skill_<id>` tools
  are dropped from `list_tools()` entirely — the agent invokes
  via the new `skill_run(skill_id, args)` meta-tool after
  discovering through `skill_browse` + `skill_view` (Hermes-style
  3-step). All 6 meta-tools (browse / view / status / install /
  uninstall / run) are always-on and whitelisted in the prefilter.
  Wire from `daemon/app.py` reads `config.skills.{disclosure_mode,
  unified_threshold}`.
- `prefilter.py` — B-238 token-overlap top-K filter that narrows
  ~404 installed skills to ~12 per turn. Non-skill tools and
  `skill_browse` always pass through; skills below
  `min_skills_to_filter=30` skip filtering entirely.
- `variant_selector.py` — B-295 UCB1 over `(skill_id, version)`
  arms. Subscribes to `GRADER_VERDICT`, exposes
  `pick_version(skill_id) -> int | None` consulted by
  `SkillToolProvider.invoke` when the daemon wires it in.
- `user_loader.py` — boot-time scan of `~/.xmclaw/skills_user/`
  + `~/.agents/skills/` (+ extras from
  `evolution.skill_paths.extra`). Loaded once at startup;
  runtime updates handled by `xmclaw/daemon/skills_watcher.py`.
- `demo/` — example skills that double as integration fixtures.
