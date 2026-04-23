# AGENTS.md — template (Epic #12)

Every meaningful subdirectory of `xmclaw/` should carry a file named
`AGENTS.md` that any future contributor — human or AI — can read cold
and understand (a) what the directory is for, (b) what it's allowed to
depend on, and (c) what would be a violation. Top-level `CLAUDE.md`
handles repo-wide navigation; `AGENTS.md` handles the local contract.

Copy this file into `<subdir>/AGENTS.md` and fill in all five sections.
Keep it terse — a long AGENTS.md nobody reads is worse than a short
one that sticks. Target: 40-120 lines.

---

## 1. 职责（Responsibility）

> One paragraph. What is this directory *for*? What does code here
> own, and what does it deliberately NOT own? If you can't finish
> "this directory is the single source of truth for X" in one
> sentence, the boundary isn't sharp enough — fix that first.

## 2. 依赖规则（Dependency rules）

> What *this* directory is allowed to import FROM. Spell the rule out
> so CI can mechanize it later. Example:
>
> - ✅ MAY import: `xmclaw.core.*`, `xmclaw.utils.*`, Python stdlib.
> - ❌ MUST NOT import: `xmclaw.daemon.*`, `xmclaw.providers.*`.
>
> State the *reason* — "core is the causal upstream; letting it
> depend on providers inverts the DAG and creates import cycles."

## 3. 测试入口（How to test changes here）

> Point to the pytest lanes that exercise this directory, the
> relevant smart-gate lane in `scripts/test_lanes.yaml`, and any
> fixtures or manual-smoke commands specific to the module.
>
> - Unit: `tests/unit/test_v2_<module>.py`
> - Integration: `tests/integration/test_v2_<module>_<scenario>.py`
> - Smart-gate lane: `<lane_name>` in `scripts/test_lanes.yaml`
> - Manual smoke: `python -m xmclaw.<module> --help` (if applicable)

## 4. 禁止事项（Hard no's）

> Rules a change here must never break. Each item should be
> observable — "don't import X", "don't add network I/O at module
> scope", "don't silence exceptions without logging". If you can't
> state it as a falsifiable check, weaken it into a 建议 (suggestion)
> section elsewhere or drop it.
>
> Examples:
>
> - ❌ Don't add sync HTTP calls at module import time (deadlocks
>   the daemon factory).
> - ❌ Don't catch `Exception:` without a targeted re-raise or
>   structured log — silent swallows hid the #42 bug for two weeks.
> - ❌ Don't rename public event types without bumping the schema
>   version in `docs/EVENTS.md`.

## 5. 关键文件（Key files / entry points）

> The 3-6 files a new contributor should read first. One-line each,
> pointing to path:line where the public surface lives.
>
> - `foo.py:12` — `FooManager` class, the public entry point.
> - `types.py` — dataclasses consumed by the rest of the package.
> - `tests/unit/test_v2_foo.py` — contract tests; read before
>   editing `foo.py`.

---

**Style notes for this template**:

- Prefer concrete examples over abstract principles. "Don't import
  providers" is actionable; "maintain good separation of concerns"
  is not.
- When a rule has an incident history, cite it. "We learned this
  from #42" is more persuasive than "best practice says …".
- If a rule changes, update `AGENTS.md` in the same PR that makes
  the change stick in the code. Drift kills the file's authority.
- Keep CLAUDE.md as the repo-wide navigator; keep per-dir AGENTS.md
  for local contracts. Don't duplicate the same rule in both places.
