<!-- 谢谢提 PR! XMclaw 的开发纪律见 docs/DEV_ROADMAP.md §3.6 — Epic 类 PR 必须引用 Epic 号. -->

## Summary

<!-- What changed and why. 1–3 bullets. Focus on the "why". -->

**Related issue:** Fixes #(num) / Relates to #(num)
**Related Epic:** <!-- e.g. "Epic #14: prompt-injection scan for memory recall" — required if the change touches a tracked Epic. -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change (callers, config schema, or wire format affected)
- [ ] Documentation only
- [ ] Refactor / internal cleanup
- [ ] CI / build / tooling

## Components affected

- [ ] Core (`xmclaw/core/` — bus, IR, grader, evolution, scheduler)
- [ ] Daemon (`xmclaw/daemon/` — FastAPI, WS, AgentLoop, static UI)
- [ ] Providers (`xmclaw/providers/` — LLM / tool / memory / runtime / channel)
- [ ] CLI (`xmclaw/cli/`)
- [ ] Skills (`xmclaw/skills/`)
- [ ] Security / Anti-Req (`xmclaw/security/`)
- [ ] Backup / utils
- [ ] Docs (`docs/*.md`, `README*.md`)
- [ ] Tests / CI

## Anti-Req checklist (skip if N/A)

- [ ] No new path imports upward (core ← providers etc) — `python scripts/check_import_direction.py`
- [ ] If a new tool/provider was added, `docs/TOOLS.md` and the relevant `AGENTS.md` are updated
- [ ] If event schema changed, bump `schema_version` and update `docs/EVENTS.md`
- [ ] If touching grader / scheduler, the LLM self-grade weight stays ≤ 0.2
- [ ] If touching auth, WS denies invalid tokens with `close(4401)`

## Local verification

```bash
# Required: smart-gate at minimum
python scripts/test_changed.py
ruff check xmclaw/

# Recommended for non-trivial changes:
python -m pytest tests/ -q
python scripts/lint_roadmap.py docs/DEV_ROADMAP.md
```

Paste the summary lines (don't paste full output — link to a CI run instead).

```
ruff check ............... 0 errors
pytest ................... XXX passed, X skipped
roadmap_lint ............. clean
```

## Roadmap discipline (Epic-touching PRs)

- [ ] Updated `docs/DEV_ROADMAP.md` §4 (status, owner, dates)
- [ ] Added a progress-log line: `YYYY-MM-DD: <summary> (commit <sha7>)`
- [ ] Commit message references Epic: `Epic #N: <action>` / `Epic #N partial:` / `Epic #N blocked:`

## Test plan

<!-- How a reviewer should verify this works. Include the commands or UI flow. -->

## Screenshots / transcripts (UI / behavioural changes)

<!-- For UI: before/after. For behaviour: a real conversation transcript or event excerpt. -->

## Additional notes

<!-- Migration steps, follow-ups, known limitations. -->
