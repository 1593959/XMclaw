"""Unit tests for ``scripts/lint_roadmap.py`` (Epic #10 phase 2).

The lint script is the mechanical backstop for §3.6.5 — it catches
Epic / Milestone drift that humans reliably introduce. These tests
pin the five rules the linter actually enforces (not a broader spec)
so a future rule extension is an intentional change, not an accident.

Covered:
  * Well-formed roadmap with one WIP Epic → clean.
  * Epic status DONE but 完成 date is ``-`` → violation.
  * Epic status WIP but 起始 date is ``-`` → violation.
  * Epic status DONE with one unchecked checklist item → violation.
  * Deferred checklist item ("留给 Epic #2") under a DONE Epic →
    tolerated, no violation.
  * Milestone exit criterion ``[ ]`` whose sole referenced Epic is
    DONE → violation (the cross-check).
  * Duplicate Epic numbers → parse-level error.
  * Shipped roadmap file is always clean (regression guard — every
    roadmap change has to keep this passing).
  * Rule #5: ``(commit pending)`` / ``(commit 待落)`` sentinels in
    Epic progress logs are violations; same strings elsewhere (or
    in the next Epic's checklist) must not false-positive.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "lint_roadmap.py"


def _load_linter():
    spec = importlib.util.spec_from_file_location("lint_roadmap", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lint_roadmap"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def linter():
    return _load_linter()


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ROADMAP.md"
    p.write_text(body, encoding="utf-8")
    return p


_CLEAN_BODY = """\
## 4. Epics

### Epic #1 · Thing

**状态**：✅ 完成 | **负责人**：me | **起始**：2026-04-01 | **完成**：2026-04-02

**检查清单**：

- [x] Did the thing

## 7. Milestones

### M1 · Done

**退出标准**：

- [x] Epic #1 shipped
"""


def test_clean_roadmap_has_no_violations(linter, tmp_path):
    path = _write(tmp_path, _CLEAN_BODY)
    assert linter.lint(path) == []


def test_done_epic_missing_end_date_flagged(linter, tmp_path):
    body = _CLEAN_BODY.replace(
        "**起始**：2026-04-01 | **完成**：2026-04-02",
        "**起始**：2026-04-01 | **完成**：-",
    )
    path = _write(tmp_path, body)
    violations = linter.lint(path)
    assert any("DONE but 完成 date is '-'" in v for v in violations)


def test_wip_epic_missing_start_date_flagged(linter, tmp_path):
    body = """\
## 4. Epics

### Epic #1 · WIP thing

**状态**：🟡 进行中 | **负责人**：me | **起始**：- | **完成**：-

**检查清单**：

- [ ] Todo

## 7. Milestones

### M1 · Open

**退出标准**：

- [ ] Epic #1 shipped
"""
    path = _write(tmp_path, body)
    violations = linter.lint(path)
    assert any("WIP but 起始 date is '-'" in v for v in violations)


def test_done_epic_with_unchecked_item_flagged(linter, tmp_path):
    body = _CLEAN_BODY.replace("- [x] Did the thing", "- [ ] Did the thing")
    path = _write(tmp_path, body)
    violations = linter.lint(path)
    assert any("unchecked" in v for v in violations)


def test_deferred_unchecked_item_is_tolerated(linter, tmp_path):
    body = _CLEAN_BODY.replace(
        "- [x] Did the thing",
        "- [x] Did the thing\n- [ ] Other thing (留给 Epic #2 — later)",
    )
    path = _write(tmp_path, body)
    assert linter.lint(path) == []


def test_english_deferred_syntax_also_tolerated(linter, tmp_path):
    body = _CLEAN_BODY.replace(
        "- [x] Did the thing",
        "- [x] Did the thing\n- [ ] Other (deferred to Epic #9)",
    )
    path = _write(tmp_path, body)
    assert linter.lint(path) == []


def test_milestone_criterion_unchecked_but_epic_done_flagged(linter, tmp_path):
    body = _CLEAN_BODY.replace("- [x] Epic #1 shipped", "- [ ] Epic #1 shipped")
    path = _write(tmp_path, body)
    violations = linter.lint(path)
    assert any("Milestone exit criterion" in v for v in violations)


def test_milestone_criterion_unchecked_with_wip_epic_not_flagged(linter, tmp_path):
    """If the referenced Epic isn't done, the criterion legitimately
    isn't done either — no violation."""
    body = """\
## 4. Epics

### Epic #1 · WIP

**状态**：🟡 进行中 | **负责人**：me | **起始**：2026-04-01 | **完成**：-

**检查清单**：

- [ ] Todo

## 7. Milestones

### M1 · Open

**退出标准**：

- [ ] Epic #1 shipped
"""
    path = _write(tmp_path, body)
    assert linter.lint(path) == []


def test_duplicate_epic_numbers_raise_parse_error(linter, tmp_path):
    body = """\
## 4. Epics

### Epic #1 · A

**状态**：✅ 完成 | **负责人**：me | **起始**：2026-04-01 | **完成**：2026-04-02

### Epic #1 · B

**状态**：✅ 完成 | **负责人**：me | **起始**：2026-04-01 | **完成**：2026-04-02
"""
    path = _write(tmp_path, body)
    violations = linter.lint(path)
    assert any("duplicate Epic #1" in v for v in violations)


def test_missing_roadmap_returns_exit_code_2(linter, tmp_path):
    rc = linter.main(["lint_roadmap.py", str(tmp_path / "nope.md")])
    assert rc == 2


def test_shipped_roadmap_passes(linter):
    """Regression guard: the real docs/DEV_ROADMAP.md must stay clean."""
    real = _ROOT / "docs" / "DEV_ROADMAP.md"
    assert linter.lint(real) == []


def test_criterion_referencing_multiple_epics_partial_done(linter, tmp_path):
    """A criterion listing two Epics where only one is done should NOT be
    flagged — the criterion can't be marked complete while the other Epic
    is still open."""
    body = """\
## 4. Epics

### Epic #1 · A

**状态**：✅ 完成 | **负责人**：me | **起始**：2026-04-01 | **完成**：2026-04-02

**检查清单**:

- [x] done

### Epic #2 · B

**状态**：🟡 进行中 | **负责人**：me | **起始**：2026-04-01 | **完成**：-

**检查清单**:

- [ ] pending

## 7. Milestones

### M1 · Partial

**退出标准**：

- [ ] Epic #1 + Epic #2 together
"""
    path = _write(tmp_path, body)
    assert linter.lint(path) == []


# ---------------------------------------------------------------------------
# Rule #5: `(commit pending)` / `(commit 待落)` sentinels in progress logs
# ---------------------------------------------------------------------------
# 背景：手工排查时发现有 6 条进度日志遗留了 `(commit 待落)` 占位符从未回填真实
# sha。这些占位符的本意是"下次 commit 后回来补 sha"，但很容易在多轮切换中忘
# 记，让日志变成比 `git blame` 更糟的历史记录。Rule #5 把这个扫描自动化。
#
# 作用域：仅在 Section 4 Epic 的 **进度日志** 块内检测——其他位置（设计讨论、
# commit message 回显）即使字面出现该字符串也不应误报。

_SENTINEL_BODY_TEMPLATE = """\
## 4. Epics

### Epic #1 · Thing

**状态**：🟡 进行中 | **负责人**：me | **起始**：2026-04-01 | **完成**：-

**检查清单**：

- [ ] Work item

**进度日志**：

{log_lines}
"""


def test_commit_pending_sentinel_in_progress_log_flagged(linter, tmp_path):
    body = _SENTINEL_BODY_TEMPLATE.format(
        log_lines="- 2026-04-20: did a thing (commit pending)"
    )
    path = _write(tmp_path, body)
    violations = linter.lint(path)
    assert any("sha TODO sentinel" in v for v in violations)
    assert any("commit pending" in v for v in violations)


def test_commit_dailuo_sentinel_in_progress_log_flagged(linter, tmp_path):
    """Chinese variant `(commit 待落)` must also be caught — the 6 drifted
    entries found in practice all used this form."""
    body = _SENTINEL_BODY_TEMPLATE.format(
        log_lines="- 2026-04-20: 做了个东西 (commit 待落)"
    )
    path = _write(tmp_path, body)
    violations = linter.lint(path)
    assert any("sha TODO sentinel" in v for v in violations)


def test_multiple_sentinels_each_reported_separately(linter, tmp_path):
    """Per-line reporting: authors should be able to grep the lint output
    into an edit list, not hunt the markers themselves."""
    body = _SENTINEL_BODY_TEMPLATE.format(
        log_lines=(
            "- 2026-04-20: thing one (commit pending)\n"
            "- 2026-04-21: thing two (commit 待落)\n"
            "- 2026-04-22: thing three (commit abc1234)\n"
            "- 2026-04-23: thing four (commit pending)"
        )
    )
    path = _write(tmp_path, body)
    violations = [v for v in linter.lint(path) if "sha TODO sentinel" in v]
    assert len(violations) == 3  # three pending/待落 lines, one backfilled


def test_sentinel_outside_progress_log_not_flagged(linter, tmp_path):
    """Scoping guard: the literal string appearing in a checklist item or
    description must NOT trigger — only progress-log drift matters."""
    body = """\
## 4. Epics

### Epic #1 · Thing

**状态**：🟡 进行中 | **负责人**：me | **起始**：2026-04-01 | **完成**：-

**检查清单**：

- [ ] Work item mentioning (commit pending) in passing discussion

**退出标准**：

- Something about (commit 待落) as a design note
"""
    path = _write(tmp_path, body)
    violations = [v for v in linter.lint(path) if "sha TODO sentinel" in v]
    assert violations == []


def test_backfilled_sha_does_not_trigger(linter, tmp_path):
    """Regression guard: a properly backfilled log entry is clean."""
    body = _SENTINEL_BODY_TEMPLATE.format(
        log_lines="- 2026-04-20: did a thing (commit abc1234)"
    )
    path = _write(tmp_path, body)
    violations = [v for v in linter.lint(path) if "sha TODO sentinel" in v]
    assert violations == []


def test_progress_log_scope_resets_on_next_epic(linter, tmp_path):
    """Scope hygiene: `in_progress_log` must reset when a new Epic header
    starts. Otherwise a sentinel in Epic #2's checklist would be
    mis-attributed to Epic #1's (still-open) progress-log state."""
    body = """\
## 4. Epics

### Epic #1 · First

**状态**：🟡 进行中 | **负责人**：me | **起始**：2026-04-01 | **完成**：-

**检查清单**：

- [ ] Work

**进度日志**：

- 2026-04-20: initial (commit abc1234)

### Epic #2 · Second

**状态**：🟡 进行中 | **负责人**：me | **起始**：2026-04-01 | **完成**：-

**检查清单**：

- [ ] Item mentioning (commit pending) only in the description

**退出标准**：

- ship it
"""
    path = _write(tmp_path, body)
    violations = [v for v in linter.lint(path) if "sha TODO sentinel" in v]
    assert violations == []
