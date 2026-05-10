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

## Closure status (2026-05-10 update)

### Section A — code-side
- ✅ A1-A11 noqa annotations + F401 cleanup (commit f77958c)
- ✅ A12 in-flight TODO at swe_bench_verified.py:275 — Tier-2
  sandboxed grader landed (commits 8c0401a + 53faebb), TODO marker
  removed.
- ✅ A0 (pre-existing 17-fail in test_v2_secrets.py) — fixed via
  ``pytestmark = pytest.mark.real_secrets`` (commit f77958c).

### Section B — UI/UX-side
- ✅ B1-B5 loading + error states for Chat / Memory / ModelProfiles
  (commit 58e5fdc).
- ✅ B6 useSafeFetch hook + 7-page migration (commits 83cc0b3 + c4671cd).
  Docs.js was reverted because the BG agent only added the import
  without finishing the migration; Docs.js's existing
  ``cancelled = false`` cleanup is functionally equivalent.
- ✅ B7 try/catch on POST handlers — covered as part of useSafePost
  migration (the hook returns ``{ok, error}`` so callers don't need
  manual try/catch).
- ⏸ B8-B15 budget-near files — deferred per priority. Each file is
  on the explicit ``KNOWN_OVERSIZED`` grandfather list in
  ``test_v2_ui_scaffold.py``; they get a forced extraction pass on
  the next change.

### Section C — tests/coverage
- (acceptable per priority) C1 optional-dep skips in
  test_v2_automation_tools.py + test_v2_content_tools.py.
- ✅ C2 cognition WS push end-to-end test
  (``test_v2_cognition_ws_push.py``, commit 30906eb, 8 cases).
- ✅ C3 stuck-loop UI verification — front-back contract test
  (``test_v2_stuck_loop_front_back.py``, commit 2b40d48, 3 cases).

## Audit pass-3 follow-ups (post-original-list, 2026-05-10)

User then flagged 4 additional pain points via the find-skills
"Comprehensive capability audit" prompt. Closed all 4:

- ✅ (a) chat-reducer payload-key drift (call_id vs tool_call_id)
  ``test_v2_chat_reducer_tool_contract.py`` — 10 contract tests
  covering Python-side daemon emission + JS-side reducer ingestion,
  including B-267 race-order edge cases. Commits 3981f9a + 34cb15b.
- ✅ (b) max_tokens mid-stream truncation handling
  ``test_v2_b229_max_tokens_truncation_integration.py`` — 9 cross-
  provider tests pinning the drop-partial-tool-call + truncation-
  marker behaviour. Commit c30ff68.
- ✅ (d) async LLM retry semantics
  ``test_v2_b227_retry_semantics_integration.py`` — 8 tests
  covering rate_limit/overloaded retry-success, auth/format
  bail-immediately, schedule-length contract. Commit 3adee5a.
- ✅ (e) UI rendering edge cases — closed via the B-267 race-order
  cases inside the chat-reducer contract file (commit 34cb15b).

## Order-dependent suite-pollution fixes (2026-05-10)

While running the FULL ``pytest tests/unit/`` for the first time
post-fixes, two order-dependent failure clusters surfaced (passed
in isolation, failed when other tests ran first):

- ✅ ``test_v2_gepa_pareto.py`` (11 tests) — replaced
  ``asyncio.get_event_loop().run_until_complete()`` with
  ``asyncio.run()`` so the helper doesn't see a live event loop
  left by an earlier asyncio test. Commit d575868.
- ✅ ``test_v2_onboard.py::test_happy_path_writes_config_and_secret``
  — added ``@pytest.mark.real_secrets`` so the test opts out of the
  B-386-followup auto-patch that returns ``None`` from
  ``get_secret``. Commit 49a083c.

---

*Auto-generated 2026-05-09; closure status appended 2026-05-10 after
full audit-pass-3 sweep + 4 follow-up gaps + 2 suite-pollution fixes.*
