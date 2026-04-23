# AGENTS.md — `xmclaw/providers/runtime/`

## 1. 职责

Skill-execution runtimes. `base.py` defines `SkillRuntime` ABC;
`local.py` runs skills in-process (test default), `process.py` runs
them as isolated subprocesses with time + resource budgets.

A skill is "code proposed by the `EvolutionController` that we want to
run without letting it compromise the host." Runtime choice is the
main safety lever.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*`, `xmclaw.utils.*`, stdlib,
  `multiprocessing`, `asyncio`, `psutil`.
- ❌ MUST NOT import: sibling `providers/*` packages,
  `xmclaw.daemon.*`, `xmclaw.cli.*`.

## 3. 测试入口

- Unit: `tests/unit/test_v2_local_runtime.py`,
  `tests/unit/test_v2_process_runtime.py`.
- Smart-gate lane: `runtime`.

## 4. 禁止事项

- ❌ Don't skip the budget timeout when running untrusted skill
  code — `process.py` must enforce wall-clock + CPU caps. A
  skill that never returns is an availability attack.
- ❌ Don't use `subprocess.run(shell=True)`. Pass an argv list;
  never let skill source control the invocation string.
- ❌ Don't share state between skill runs via module-level
  globals. Each run is a fresh sandbox; statefulness hides in
  bugs that pass tests but break autonomous evolution.

## 5. 关键文件

- `base.py` — `SkillRuntime` ABC: `run(skill, inputs) ->
  SkillResult`.
- `local.py` — in-process runner (fast, unsafe; tests/dev only).
- `process.py` — subprocess runner (slow, isolated; production).
