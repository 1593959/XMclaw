# AGENTS.md — `xmclaw/skills/`

## 1. 职责

Skill-authoring machinery: the `SkillBase` ABC (`base.py`), manifest
schema (`manifest.py`), registry (`registry.py`), and versioning
rules (`versioning.py`). `demo/` holds the curated skills that ship
with the repo and get exercised by integration tests.

Skills are the unit the EvolutionEngine produces, mutates, and
grades. Anything here must stay explicit enough for automated
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
- `demo/` — example skills that double as integration fixtures.
