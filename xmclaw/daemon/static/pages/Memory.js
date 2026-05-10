// XMclaw — Memory page
//
// Three tabs:
//   1. 标识 — edit the 7 canonical persona files (SOUL/AGENTS/USER/MEMORY/
//      IDENTITY/TOOLS/BOOTSTRAP). Backed by GET/PUT /api/v2/profiles/active.
//      Saves rebuild app.state.agent's system prompt so edits land on the
//      next turn — no daemon restart needed.
//   2. 笔记 — the legacy memory notes browser (GET /api/v2/memory + per-file
//      GET/POST). Lets the user keep arbitrary topic notes alongside the
//      structured persona files. POST upserts on save, so notes are now
//      editable too (the prior version was read-only).
//   3. 日记 — daily journal (GET /api/v2/journal + per-date GET/PUT).
//      "Today" loads on tab open; the date list shows past entries with
//      previews. Empty save deletes the file.

const { h, Component } = window.__xmc.preact;
const { useState, useMemo, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { confirmDialog } from "../lib/dialog.js";
import { NotesTab, JournalTab } from "./_panels/memory_notes_journal.js";
import { IdentityTab } from "./_panels/memory_identity.js";
// B-323: ProvidersTab + MemoryActivitySparkline split into
// _panels/memory_providers.js so this page stays under the 500-line
// UI budget (FRONTEND_DESIGN.md §1.4). After B-323's split into the
// 5 sub-panels (memory_providers_{indexer,dream,pinned,picker,
// switcher}.js), memory_providers.js itself sits at ~415 LOC — back
// under the 500-line cap. KNOWN_OVERSIZED grandfather list is now
// empty per tests/unit/test_v2_ui_scaffold.py.
import { ProvidersTab } from "./_panels/memory_providers.js";
import { UnifiedQueryTab } from "./_panels/memory_unified_query.js";
// 2026-05-10 ("agent 自己用记忆" Phase C1): activity timeline showing
// what the AGENT has read/written via UnifiedMemorySystem since
// the daemon booted. This is the "view that's actually for the user" —
// the unified-query tab next to it is now a debug surface.
import { ActivityTab } from "./_panels/memory_activity.js";

// ── shared ────────────────────────────────────────────────────────────
// B-323: apiPut / apiPost / _diagnoseFetch / todayIso were used only
// by the now-extracted ProvidersTab — duplicated into
// _panels/memory_providers.js so this shell stays small. Kept the
// section header so future readers know where shared helpers go.

// 2026-05-10 redesign per user feedback "我的目的是给他自己用，不是
// 光给我用"：把"记忆活动"放在最显眼的位置（agent 自己读写记忆的实时
// 时间线），把"统一查询"降级成调试工具（顶部 banner 说明）。
const TAB_LABELS = [
  { id: "identity", label: "标识", hint: "SOUL / AGENTS / USER / MEMORY 等核心人格文件" },
  { id: "notes", label: "笔记", hint: "随手保存的主题笔记（~/.xmclaw/memory/*.md）" },
  { id: "journal", label: "日记", hint: "按日期归档的对话/事件记录" },
  { id: "activity", label: "记忆活动", hint: "Agent 自动读/写 UnifiedMemorySystem 的实时时间线" },
  { id: "unified", label: "统一查询 (调试)", hint: "手填多轴检索 — 仅供开发者验证 agent 内部存了什么；正常使用请看 \"记忆活动\"" },
  { id: "providers", label: "Providers", hint: "已挂载的记忆 provider（B-26 Hermes-style 抽象）" },
];


// IdentityTab + NotesTab + JournalTab live in ./_panels/ subtree —
// renamed from Memory-Identity.js / Memory-NotesJournal.js (B-308) to
// make it clear these are sub-panels of MemoryPage, not top-level
// routable pages. The original B-49 + B-52 splits keep individual
// files under the 500-line scaffold budget.

// ── shell error boundary (audit pass-3 B5) ────────────────────────────
//
// Each tab owns its own data-fetch + error state, but if a sub-panel
// throws synchronously during render the whole MemoryPage was blanking.
// This tiny class catches a tab-level throw and renders a recovery
// block while keeping the shell + tab nav alive — clicking another tab
// (or Retry) resets the boundary. Preact 10 supports componentDidCatch.

class TabErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { err: null };
  }
  componentDidCatch(err) {
    this.setState({ err });
  }
  componentDidUpdate(prev) {
    // Auto-reset when caller swaps the tab (resetKey changes) so a
    // good tab isn't masked by a stale error from another one.
    if (prev.resetKey !== this.props.resetKey && this.state.err) {
      // eslint-disable-next-line react/no-did-update-set-state
      this.setState({ err: null });
    }
  }
  render(props, state) {
    if (state.err) {
      return h(
        "div",
        { class: "xmc-h-error", role: "alert", style: "margin:.5rem 0" },
        [
          h("strong", null, "标签页加载失败"),
          h(
            "div",
            { style: "font-size:.78rem;opacity:.85;margin-top:4px;word-break:break-word" },
            String(state.err && state.err.message || state.err),
          ),
          h(
            "button",
            {
              type: "button",
              class: "xmc-h-btn",
              style: "margin-top:.5rem",
              onClick: () => this.setState({ err: null }),
            },
            "重试",
          ),
        ],
      );
    }
    return props.children;
  }
}

// ── shell ─────────────────────────────────────────────────────────────

export function MemoryPage({ token }) {
  const [tab, setTab] = useState("identity");
  const activeMeta = useMemo(() => TAB_LABELS.find((t) => t.id === tab), [tab]);

  // B2: small mounting indicator — flicked on each tab switch and cleared
  // on the next animation frame after mount. Sub-panels do their own
  // network-loading UI; this is purely the "shell ack" so the user gets
  // immediate feedback that the click registered before the tab paints.
  const [mounting, setMounting] = useState(false);
  useEffect(() => {
    setMounting(true);
    const id = window.requestAnimationFrame(() => setMounting(false));
    return () => window.cancelAnimationFrame(id);
  }, [tab]);

  return html`
    <section class="xmc-datapage" aria-labelledby="memory-title">
      <header class="xmc-datapage__header">
        <h2 id="memory-title">记忆</h2>
        <p class="xmc-datapage__subtitle">
          ${activeMeta ? activeMeta.hint : ""}
          ${mounting ? html`<span style="margin-left:.5rem;font-size:.72rem;opacity:.6">· 加载中…</span>` : null}
        </p>
      </header>
      <nav class="xmc-mem-tabs" role="tablist" aria-label="记忆类别" style="display:flex;gap:.4rem;border-bottom:1px solid var(--color-border);margin-bottom:.8rem;flex-wrap:wrap">
        ${TAB_LABELS.map((t) => {
          const isActive = t.id === tab;
          return html`
            <button
              type="button"
              role="tab"
              aria-selected=${isActive}
              onClick=${() => setTab(t.id)}
              key=${t.id}
              style=${`appearance:none;background:none;border:none;padding:.5rem .9rem;font:inherit;cursor:pointer;color:${isActive ? "var(--color-primary)" : "var(--xmc-fg-muted)"};border-bottom:2px solid ${isActive ? "var(--color-primary)" : "transparent"};font-weight:${isActive ? "600" : "500"}`}
            >
              ${t.label}
            </button>
          `;
        })}
      </nav>
      <${TabErrorBoundary} resetKey=${tab}>
        ${tab === "identity" ? html`<${IdentityTab} token=${token} />` : null}
        ${tab === "notes" ? html`<${NotesTab} token=${token} />` : null}
        ${tab === "journal" ? html`<${JournalTab} token=${token} />` : null}
        ${tab === "activity" ? html`<${ActivityTab} token=${token} />` : null}
        ${tab === "unified" ? html`<${UnifiedQueryTab} token=${token} />` : null}
        ${tab === "providers" ? html`<${ProvidersTab} token=${token} />` : null}
      <//>
    </section>
  `;
}
