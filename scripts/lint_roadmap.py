"""Lint ``docs/DEV_ROADMAP.md`` for status consistency (Epic #10 §7).

The roadmap is the authoritative plan — every Epic's status triples up
across three places (Epic header, §4 checklist, §7 Milestone exit
criteria). In practice humans (and AI pair partners) update two of the
three and forget the third, leaving the doc claiming that an Epic is
both "✅ 完成" and "- [ ]" at the same time.

This script is the diff-catcher. It parses the markdown with a small
state machine (no third-party deps — it has to run in any environment
including a fresh checkout before ``pip install``) and applies these
rules:

1. **Status/date consistency**:
   - If Epic 状态 is ``✅ 完成``, the **完成** date must not be ``-``.
   - If Epic 状态 is ``🟡 进行中``, the **起始** date must not be ``-``.
2. **Checklist consistency**:
   - An Epic marked ``✅ 完成`` must have all its ``- [ ]`` / ``- [x]``
     checklist items checked. Any remaining ``- [ ]`` is a lint error.
3. **Milestone ↔ Epic cross-check** (§7):
   - When a Milestone exit-criterion line references ``Epic #N`` and
     Epic #N's status is ``✅``, the criterion itself must also be
     ``[x]``. An unchecked criterion pointing at a done Epic means
     either the Epic isn't really done, or the Milestone forgot to
     sync — either way, surface it.
4. **No duplicate Epic numbers**.
5. **No sha TODO markers in progress logs** (Epic §4 only):
   - Progress-log entries must not end the run with a ``(commit pending)``
     or ``(commit 待落)`` sentinel. These mark "come back and fill in the
     sha after the next commit" — leaving them indefinitely turns the
     log into `git blame` but worse. A manual scan found six of these
     lurking once; the lint rule replaces the scan.
   - Scoped to ``**进度日志**`` blocks under Section 4 Epic headers so
     the literal strings appearing in other prose (design discussion,
     commit messages) stay untouched.
   - Enforcement is mechanical: we look for the two literal substrings,
     not whether a 7-char hex actually exists. Verifying real shas would
     bitrot on branch renames / force-pushes — see anti-goals.

Exit code ``0`` = clean, ``1`` = violations found (printed as
``file:line: message``), ``2`` = roadmap missing / unparseable.

Invocation:
    python scripts/lint_roadmap.py [path/to/DEV_ROADMAP.md]

Anti-goals: this is a *drift detector*, not a project manager. It does
not enforce that every Epic has a progress log, nor that commit SHAs
in the log actually exist. Those heuristics lead to bitrot. Rule #5
asserts only the *absence* of two literal placeholder strings.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

STATUS_DONE = "✅"
STATUS_WIP = "🟡"
STATUS_BLOCKED = "🔴"
STATUS_TODO = "⬜"

_EPIC_HEADER = re.compile(r"^###\s+Epic\s+#(\d+)\b")
_MILESTONE_HEADER = re.compile(r"^###\s+(M\d+)\b")
_STATUS_LINE = re.compile(
    r"\*\*状态\*\*：([" + STATUS_DONE + STATUS_WIP + STATUS_BLOCKED + STATUS_TODO + r"])"
)
_START_LINE = re.compile(r"\*\*起始\*\*：([^\s|]+)")
_END_LINE = re.compile(r"\*\*完成\*\*：([^\s|]+)")
_CHECKBOX = re.compile(r"^\s*-\s*\[([ xX])\]")
_EPIC_REF = re.compile(r"Epic\s+#(\d+)")
# An unchecked checklist item is tolerated when it explicitly hands off
# ownership to another Epic. Keeping the item visible (rather than
# deleting it) tracks the cross-Epic dependency; the annotation is the
# signal that it is deferred, not forgotten.
_DEFERRED = re.compile(r"(留给\s*Epic\s*#\d+|挂单\s*Epic\s*#\d+|deferred\s+to\s+Epic\s*#\d+)", re.IGNORECASE)
_SECTION = re.compile(r"^##\s+(\d+)\.")
# Rule #5: sentinel strings left in progress-log entries when the author
# meant to come back and fill in the sha. Checked only inside the
# **进度日志** block of each Epic; see parser for scoping.
_SHA_TODO_MARKERS = ("(commit pending)", "(commit 待落)")


@dataclass
class EpicBlock:
    number: int
    header_line: int
    status: str | None = None
    start: str | None = None
    end: str | None = None
    # (line, checked, deferred). ``deferred`` marks items whose text
    # explicitly hands off to another Epic (via the _DEFERRED pattern) —
    # they stay in the list so the dependency is visible but don't count
    # as violations when the Epic is marked done.
    checklist: list[tuple[int, bool, bool]] = field(default_factory=list)
    in_checklist: bool = False
    # Line numbers inside the **进度日志** block that still carry a
    # "(commit pending)" or "(commit 待落)" sentinel. Rule #5 emits a
    # violation per line so each drifted entry shows up independently.
    progress_log_sha_todos: list[int] = field(default_factory=list)
    in_progress_log: bool = False


@dataclass
class MilestoneCriterion:
    line: int
    checked: bool
    text: str
    refs: list[int]  # epic numbers referenced on this line


def _parse(path: Path) -> tuple[list[EpicBlock], list[MilestoneCriterion]]:
    """Parse the roadmap into (epics, milestone criteria) lists.

    The state machine is deliberately small: we flip a bit when we
    enter section 4 vs 7, and track the "currently open" Epic /
    Milestone by remembering the last header we crossed.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    epics: dict[int, EpicBlock] = {}
    current_epic: EpicBlock | None = None
    criteria: list[MilestoneCriterion] = []
    in_milestone = False
    in_checklist_block = False
    current_section = ""

    for i, line in enumerate(lines, 1):
        sec_m = _SECTION.match(line)
        if sec_m:
            current_section = sec_m.group(1)
            current_epic = None
            in_milestone = False
            in_checklist_block = False
            continue

        if current_section == "4":
            m = _EPIC_HEADER.match(line)
            if m:
                n = int(m.group(1))
                if n in epics:
                    raise ValueError(
                        f"{path}:{i}: duplicate Epic #{n} header "
                        f"(first at line {epics[n].header_line})"
                    )
                current_epic = EpicBlock(number=n, header_line=i)
                epics[n] = current_epic
                in_checklist_block = False
                continue

            if current_epic is None:
                continue

            if _STATUS_LINE.search(line):
                current_epic.status = _STATUS_LINE.search(line).group(1)
            if _START_LINE.search(line):
                current_epic.start = _START_LINE.search(line).group(1)
            if _END_LINE.search(line):
                current_epic.end = _END_LINE.search(line).group(1)

            if line.strip().startswith("**检查清单**"):
                in_checklist_block = True
                current_epic.in_progress_log = False
                continue
            if line.strip().startswith("**退出标准**"):
                in_checklist_block = False
                current_epic.in_progress_log = False
            if line.strip().startswith("**进度日志**"):
                in_checklist_block = False
                current_epic.in_progress_log = True
                continue
            if in_checklist_block:
                cb = _CHECKBOX.match(line)
                if cb:
                    current_epic.checklist.append(
                        (i, cb.group(1) in "xX", bool(_DEFERRED.search(line)))
                    )
            # Rule #5: look for sha TODO sentinels only inside the
            # progress-log block. Scanning whole lines is fine — the
            # markers are distinctive enough to not false-positive.
            if current_epic.in_progress_log:
                for marker in _SHA_TODO_MARKERS:
                    if marker in line:
                        current_epic.progress_log_sha_todos.append(i)
                        break

        elif current_section == "7":
            m = _MILESTONE_HEADER.match(line)
            if m:
                in_milestone = True
                in_checklist_block = False
                continue

            if not in_milestone:
                continue

            if line.strip().startswith("**退出标准**"):
                in_checklist_block = True
                continue
            if line.strip().startswith("**进度日志**") or line.startswith("---"):
                in_checklist_block = False
                if line.startswith("---"):
                    in_milestone = False

            if in_checklist_block:
                cb = _CHECKBOX.match(line)
                if cb:
                    refs = [int(x) for x in _EPIC_REF.findall(line)]
                    criteria.append(
                        MilestoneCriterion(
                            line=i,
                            checked=cb.group(1) in "xX",
                            text=line.strip(),
                            refs=refs,
                        )
                    )

    return list(epics.values()), criteria


def lint(path: Path) -> list[str]:
    """Return a list of violation strings; empty list means clean."""
    try:
        epics, criteria = _parse(path)
    except ValueError as e:
        return [str(e)]

    violations: list[str] = []

    for e in epics:
        prefix = f"{path}:{e.header_line}: Epic #{e.number}"
        if e.status is None:
            violations.append(f"{prefix}: missing 状态 line")
            continue

        if e.status == STATUS_DONE:
            if not e.end or e.end == "-":
                violations.append(f"{prefix}: status DONE but 完成 date is '-'")
            unchecked = [ln for ln, ok, deferred in e.checklist if not ok and not deferred]
            if unchecked:
                violations.append(
                    f"{prefix}: status DONE but {len(unchecked)} checklist items "
                    f"unchecked (lines {unchecked})"
                )

        if e.status == STATUS_WIP and (not e.start or e.start == "-"):
            violations.append(f"{prefix}: status WIP but 起始 date is '-'")

        # Rule #5: every progress-log line with a sha TODO sentinel is a
        # violation. Reported per-line so authors can grep the output
        # into an edit list instead of hunting the markers themselves.
        for ln in e.progress_log_sha_todos:
            violations.append(
                f"{path}:{ln}: Epic #{e.number}: progress log has a sha TODO "
                f"sentinel (`(commit pending)` or `(commit 待落)`) — "
                f"backfill the real sha once the commit lands"
            )

    status_by_num = {e.number: e.status for e in epics}
    for c in criteria:
        if c.checked:
            continue
        # Unchecked criterion; if every referenced Epic is done, that's drift.
        if not c.refs:
            continue
        done_refs = [n for n in c.refs if status_by_num.get(n) == STATUS_DONE]
        if done_refs and len(done_refs) == len(c.refs):
            violations.append(
                f"{path}:{c.line}: Milestone exit criterion is '[ ]' but all "
                f"referenced Epics {done_refs} are DONE"
            )

    return violations


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print("usage: lint_roadmap.py [path/to/DEV_ROADMAP.md]", file=sys.stderr)
        return 2
    path = Path(argv[1]) if len(argv) == 2 else Path("docs/DEV_ROADMAP.md")
    if not path.exists():
        print(f"lint_roadmap: roadmap not found at {path}", file=sys.stderr)
        return 2

    violations = lint(path)
    for v in violations:
        print(v)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
