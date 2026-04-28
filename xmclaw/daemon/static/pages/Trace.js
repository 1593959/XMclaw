// XMclaw — Trace page
//
// Live event timeline. Polls /api/v2/events every 2s and renders
// the most recent BehavioralEvents as an expandable feed:
//
//   - USER_MESSAGE / LLM_REQUEST / LLM_RESPONSE
//   - TOOL_CALL_EMITTED / TOOL_INVOCATION_STARTED / TOOL_INVOCATION_FINISHED
//   - SESSION_LIFECYCLE / ANTI_REQ_VIOLATION
//   - MEMORY_PUT / MEMORY_EVICTED / TODO_UPDATED
//
// Addresses the user's "状态不可观测" complaint (B-15 list). Without
// this page the user can only watch chat output — invisible internal
// state (which tools fired, why a hop happened, what the agent saw
// from a tool result) is dark. Now they can replay/observe in real
// time.

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

// Tone per event type — keeps the eye trained.
const TONE = {
  user_message: "info",
  llm_request: "muted",
  llm_chunk: "muted",
  llm_response: "success",
  tool_call_emitted: "warn",
  tool_invocation_started: "warn",
  tool_invocation_finished: "success",
  session_lifecycle: "muted",
  anti_req_violation: "error",
  memory_put: "info",
  memory_evicted: "muted",
  memory_op: "info",
  todo_updated: "info",
};

// Friendly Chinese labels.
const LABEL = {
  user_message: "用户消息",
  llm_request: "调用 LLM",
  llm_chunk: "流式片段",
  llm_response: "LLM 回复",
  tool_call_emitted: "工具调用",
  tool_invocation_started: "工具开始",
  tool_invocation_finished: "工具完成",
  session_lifecycle: "会话生命周期",
  anti_req_violation: "反需求违规",
  memory_put: "记忆写入",
  memory_evicted: "记忆驱逐",
  memory_op: "记忆操作",
  todo_updated: "Todos 更新",
};

const EVENT_TYPES = [
  "user_message",
  "llm_request",
  "llm_response",
  "tool_call_emitted",
  "tool_invocation_finished",
  "session_lifecycle",
  "anti_req_violation",
  "memory_put",
  "memory_op",
  "todo_updated",
];

function fmtTs(ts) {
  if (!ts) return "";
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
  return d.toLocaleTimeString("zh-CN", { hour12: false }) + "." +
    String(d.getMilliseconds()).padStart(3, "0");
}

function shortPayload(ev) {
  const t = ev.type;
  const p = ev.payload || {};
  if (t === "user_message") return (p.content || "").slice(0, 120);
  if (t === "llm_response") return (p.content || p.text || "").slice(0, 120);
  if (t === "llm_request") {
    return `tools=${p.tools_count || 0} model=${p.model || "?"}`;
  }
  if (t === "tool_call_emitted") {
    const args = p.args ? Object.keys(p.args).join(", ") : "";
    return `${p.name || p.tool_name || "?"}(${args})`;
  }
  if (t === "tool_invocation_finished") {
    const ok = !p.error;
    if (ok) {
      const r = typeof p.result === "string"
        ? p.result.slice(0, 120)
        : JSON.stringify(p.result || {}).slice(0, 120);
      return `ok · ${r}`;
    }
    return `error · ${(p.error || "").slice(0, 120)}`;
  }
  if (t === "session_lifecycle") return p.phase || "";
  if (t === "anti_req_violation") return p.reason || p.kind || "";
  if (t === "memory_put") return `${p.tag || ""}: ${(p.content || "").slice(0, 80)}`;
  if (t === "memory_op") {
    const op = p.op || "?";
    const prov = p.provider || "?";
    const stats = [];
    if (p.k != null) stats.push(`k=${p.k}`);
    if (p.hits != null) stats.push(`hits=${p.hits}`);
    if (p.elapsed_ms != null) stats.push(`${Math.round(p.elapsed_ms)}ms`);
    return `${prov}.${op}` + (stats.length ? "  " + stats.join(" ") : "");
  }
  if (t === "todo_updated") return `${p.count || 0} items`;
  return JSON.stringify(p).slice(0, 120);
}

function EventRow({ ev, expanded, onToggle }) {
  const t = ev.type;
  const tone = TONE[t] || "muted";
  const label = LABEL[t] || t;
  const sid = ev.session_id || "";
  const tone_cls = `xmc-h-badge xmc-h-badge--${tone}`;
  return html`
    <li
      class="xmc-datapage__row xmc-trace__row"
      key=${ev.id || (ev.ts + ":" + t)}
      style="cursor:pointer"
      onClick=${onToggle}
    >
      <div style="display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap">
        <small style="font-family:var(--xmc-font-mono);color:var(--xmc-fg-muted);min-width:9ch">${fmtTs(ev.ts)}</small>
        <span class=${tone_cls}>${label}</span>
        <code style="font-size:.7rem;color:var(--xmc-fg-muted);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${sid.slice(-24) || "(daemon)"}</code>
        <span style="flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.78rem">${shortPayload(ev)}</span>
      </div>
      ${expanded
        ? html`
            <pre style="margin:.4rem 0 0;padding:.5rem;background:var(--color-bg);border-radius:4px;font-family:var(--xmc-font-mono);font-size:.7rem;line-height:1.4;max-height:18rem;overflow:auto;white-space:pre-wrap;word-break:break-word">${JSON.stringify(ev.payload || {}, null, 2)}</pre>
          `
        : null}
    </li>
  `;
}

export function TracePage({ token }) {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);
  const [paused, setPaused] = useState(false);
  const [filterType, setFilterType] = useState("");
  const [filterSid, setFilterSid] = useState("");
  const [expanded, setExpanded] = useState(new Set());
  const [tail, setTail] = useState(80);
  const lastFetchRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const fetchOnce = async () => {
      if (paused) return;
      try {
        const types = filterType
          ? `&types=${encodeURIComponent(filterType)}`
          : `&types=${EVENT_TYPES.join(",")}`;
        const sid = filterSid ? `&session_id=${encodeURIComponent(filterSid)}` : "";
        const d = await apiGet(`/api/v2/events?limit=${tail}${types}${sid}`, token);
        if (!cancelled) {
          setEvents(d.events || []);
          setError(null);
          lastFetchRef.current = Date.now();
        }
      } catch (e) {
        if (!cancelled) setError(String(e.message || e));
      }
    };
    fetchOnce();
    const id = setInterval(fetchOnce, 2_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [token, paused, filterType, filterSid, tail]);

  const toggleRow = (key) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Most recent first.
  const ordered = useMemo(() => {
    if (!events) return [];
    return [...events].reverse();
  }, [events]);

  // Per-type counts for the legend.
  const counts = useMemo(() => {
    if (!events) return {};
    const m = {};
    for (const e of events) m[e.type] = (m[e.type] || 0) + 1;
    return m;
  }, [events]);

  return html`
    <section class="xmc-datapage" aria-labelledby="trace-title">
      <header class="xmc-datapage__header">
        <h2 id="trace-title">思考轨迹</h2>
        <p class="xmc-datapage__subtitle">
          实时事件流 — agent 的工具调用、LLM 调用、会话生命周期、记忆写入。
          每 2 秒自动刷新，点击任意行展开 payload。状态不再黑盒。
        </p>
      </header>

      <div class="xmc-datapage__row" style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem">
        <button
          class="xmc-h-btn ${paused ? 'xmc-h-btn--primary' : 'xmc-h-btn--ghost'}"
          onClick=${() => setPaused(p => !p)}
        >
          ${paused ? "▶ 恢复" : "⏸ 暂停"}
        </button>
        <button class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => setExpanded(new Set())}>折叠全部</button>
        <select
          value=${filterType}
          onChange=${(e) => setFilterType(e.target.value)}
          style="padding:.3rem .5rem;font-family:var(--xmc-font-mono);font-size:.8rem"
        >
          <option value="">所有类型</option>
          ${EVENT_TYPES.map((t) => html`<option value=${t} key=${t}>${LABEL[t] || t}</option>`)}
        </select>
        <input
          type="text"
          placeholder="按会话过滤 (session_id)"
          value=${filterSid}
          onInput=${(e) => setFilterSid(e.target.value)}
          style="flex:1 1 220px;min-width:0;padding:.3rem .5rem;font-family:var(--xmc-font-mono);font-size:.8rem"
        />
        <select
          value=${String(tail)}
          onChange=${(e) => setTail(parseInt(e.target.value, 10))}
          style="padding:.3rem .5rem"
          title="返回条数"
        >
          <option value="40">最近 40</option>
          <option value="80">最近 80</option>
          <option value="200">最近 200</option>
          <option value="500">最近 500</option>
        </select>
        <small class="xmc-datapage__subtitle" style="margin-left:auto;font-family:var(--xmc-font-mono)">
          ${(events || []).length} 条 · ${paused ? "已暂停" : "实时"}
        </small>
      </div>

      ${Object.keys(counts).length > 0
        ? html`
            <div class="xmc-datapage__row" style="display:flex;gap:.4rem;flex-wrap:wrap;font-size:.75rem;margin-bottom:.6rem">
              ${Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([t, n]) => html`
                <span class="xmc-h-badge xmc-h-badge--${TONE[t] || 'muted'}" key=${t}>
                  ${LABEL[t] || t}: ${n}
                </span>
              `)}
            </div>
          `
        : null}

      ${error
        ? html`<p class="xmc-datapage__error">${error}</p>`
        : events === null
          ? html`<p class="xmc-datapage__hint">加载中…</p>`
          : ordered.length === 0
            ? html`<p class="xmc-datapage__empty">暂无事件 — 试着发一条对话</p>`
            : html`
                <ul class="xmc-datapage__list xmc-trace__feed" style="max-height:calc(100vh - 22rem);overflow-y:auto">
                  ${ordered.map((ev) => {
                    const k = ev.id || (ev.ts + ":" + ev.type + ":" + (ev.session_id || ""));
                    return html`<${EventRow}
                      key=${k}
                      ev=${ev}
                      expanded=${expanded.has(k)}
                      onToggle=${() => toggleRow(k)}
                    />`;
                  })}
                </ul>
              `}
    </section>
  `;
}
