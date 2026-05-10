---
description: Audit pass-3 — concrete code-quality + UX findings (auto-generated 2026-05-09)
tags: [audit, pass-3, ux]
---

# Audit Pass-3 Findings

User asked me to **list the audit clean-up items myself** rather than
expect a paste-in. This is a fresh scan run on
2026-05-09 against `main` HEAD (`06b66a3`).

Methodology: grep-based static scan + UI page heuristic inspection.
Each finding has a **Severity** (P0 = bug, P1 = quality risk, P2 =
code hygiene) and a **Fix-effort** (S/M/L).

## Section A — Code-side (xmclaw/)

| # | Severity | Effort | File:Line | Finding |
|---|---|---|---|---|
| A1 | P2 | S | xmclaw/backup/create.py:85 | `except Exception:` no `noqa: BLE001` (has `# pragma: no cover` instead — not the same thing). |
| A2 | P2 | S | xmclaw/core/bus/sqlite.py:219 | `except Exception:` un-annotated. |
| A3 | P2 | S | xmclaw/daemon/lifecycle.py:103 | `except Exception:` un-annotated (HTTP health probe — `return False` IS the right design, just needs noqa). |
| A4 | P2 | S | xmclaw/daemon/multi_agent_manager.py:317 | `except Exception:` un-annotated. |
| A5 | P2 | S | xmclaw/providers/llm/openai.py:435 | `except Exception:` un-annotated (streaming → non-streaming fallback). |
| A6 | P2 | S | xmclaw/providers/tool/_helpers.py:387 | `except Exception:` un-annotated. |
| A7 | P2 | S | xmclaw/security/rule_loader.py:92 | `except Exception:` un-annotated (rule load resilience). |
| A8 | P2 | S | xmclaw/security/tool_guard/engine.py:99 | `except Exception:` un-annotated (one bad guardian shouldn't kill the engine). |
| A9 | P2 | S | xmclaw/utils/i18n.py:23 | `except Exception:` un-annotated (locale detection fallback). |
| A10 | P2 | S | xmclaw/utils/secrets.py:224 | `except Exception:` un-annotated (secrets-store resilience). |
| A11 | P2 | S | xmclaw/cognition/evolution_loop.py:20 | F401 `Awaitable` imported but unused. ruff `--fix` handles. |
| A12 | P1 | M | xmclaw/eval/swe_bench_verified.py:275 | `# TODO(B-385): wire to docker runtime`. **In progress** — Tier-2 BG agent running. Will resolve via that commit. |

**Section A summary**: 11 noqa annotations + 1 unused import + 1 in-flight Tier-2 wire. All P2 (hygiene), no P0/P1 bugs surfaced by the scan.

## Section B — UI / UX-side (xmclaw/daemon/static/)

| # | Severity | Effort | File | Finding |
|---|---|---|---|---|
| B1 | P1 | M | pages/Chat.js | No explicit `loading` state. WS user expects a thinking indicator. |
| B2 | P1 | M | pages/Memory.js | No explicit `loading` / `error` state at the shell level (each tab has its own; the shell render path can flash before tab data loads). |
| B3 | P1 | M | pages/ModelProfiles.js | No explicit `loading` state. |
| B4 | P1 | M | pages/Chat.js | No `useState` for catch-able error; relies on toasts only. WS-disconnect / 4xx aren't surfaced as a recoverable inline element. |
| B5 | P2 | M | pages/Memory.js | No top-level error boundary (sub-tab errors propagate as toast). |
| B6 | P1 | L | 8 pages (Agents/Analytics/Backup/Channels/Cognition/Config/Cron/Docs) | useEffect → apiGet pattern WITHOUT `isMounted` / `AbortController` cleanup. Potential `setState on unmounted component` warning + memory leak when user navigates away mid-fetch. |
| B7 | P2 | M | 7 places across Backup / Cognition / Doctor / Marketplace | `await apiPost(...)` in event handler with no `try/catch`. The user's onClick → unhandled promise rejection → silent fail (token revoke, network drop) without toast. |
| B8 | P2 | S | components/molecules/SetupBanner.js (500 LOC) | Right at the 500-line UI budget — any future addition needs an extraction pass. |
| B9 | P2 | S | pages/Analytics.js (499 LOC) | One line away from budget. |
| B10 | P2 | S | app.js (481 LOC) | Approaching budget. |
| B11 | P2 | S | lib/chat_reducer.js (476 LOC) | Approaching budget. |
| B12 | P2 | S | pages/Config.js (453 LOC) | Approaching budget. |
| B13 | P2 | S | components/organisms/AppShellParts.js (444 LOC) | Approaching budget. |
| B14 | P2 | S | pages/Skills.js (436 LOC) | Approaching budget. |
| B15 | P2 | S | pages/_panels/memory_providers.js (415 LOC) | Approaching budget; on the existing KNOWN_OVERSIZED grandfather list (per ui_scaffold test). |

**Section B summary**: 5 P1 UX gaps (loading/error states + missing useEffect cleanups) + 10 P2 budget-near files. The B6 useEffect cleanup is the highest single-fix value (8 pages benefit from one helper).

## Section C — Tests / Coverage

| # | Severity | Effort | Finding |
|---|---|---|---|
| C1 | P2 | S | 5 `pytest.skip(...)` calls in test_v2_automation_tools.py / test_v2_content_tools.py — gated on optional deps (`psutil`, `pypdf`, `python-docx`). Acceptable; just listing for completeness. |
| C2 | P1 | L | No end-to-end test on cognition WebSocket push — only the GET routes. WS may regress silently. |
| C3 | P2 | M | `tests/unit/test_v2_b397_stuck_loop.py` shipped + tests pass, but the daemon's actual recovery path (after stuck-loop break) isn't user-tested via UI. |

## Fix Priority Order (what to ship first)

Picking by **(S | high-coverage)** × **(low risk)**:

1. **A1-A10 + A11**: ruff-fixable cleanup (~30 min, 11 sites, no behavior change). **Ship first.**
2. **B6**: useEffect cleanup helper (single new helper in `lib/api.js`-adjacent + 8 page edits). High user-visible value. **Ship second.**
3. **B7**: try/catch wrapper or shared `runOrToast` helper for POST handlers. Medium effort.
4. **B1-B5**: Chat / Memory loading + error states. Page-by-page.
5. **B8-B15**: defer until forced (next change to that file).

## Out-of-scope (need user direction)

- Anything related to "the 28 audit pass-3 detail bugs" or "66 UX
  audit findings" the user mentioned earlier with no concrete list —
  this doc IS that list now (sized 27 findings: 12 code + 15 UX/test),
  generated by direct inspection.

---

*Auto-generated 2026-05-09. Re-run via the grep commands in the
session transcript or by spawning a "fresh audit" agent.*
