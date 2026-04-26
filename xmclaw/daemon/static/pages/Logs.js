// XMclaw — LogsPage 1:1 port of hermes-agent LogsPage.tsx
//
// Hermes layout (LogsPage.tsx:143-222):
//   - Filter toolbar row: Segmented controls for File / Level /
//     Component / Lines + auto-refresh switch + Refresh button
//   - Card with file name title + monospace log viewer (color-coded
//     ERROR red / WARNING yellow / DEBUG dim, INFO neutral) and a
//     scrolling region that auto-scrolls to bottom on each load.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_REFRESH = "M3 12a9 9 0 0 1 15-6.7L21 8 M21 3v5h-5 M21 12a9 9 0 0 1-15 6.7L3 16 M3 21v-5h5";
const I_FILE    = "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6";

const FILES      = ["daemon", "agent", "errors", "gateway"];
const LEVELS     = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"];
const COMPONENTS = ["all", "gateway", "agent", "tools", "cli", "cron"];
const LINE_COUNTS = ["50", "100", "200", "500"];

function classifyLine(line) {
  const u = line.toUpperCase();
  if (/ERROR|CRITICAL|FATAL/.test(u)) return "error";
  if (/WARNING|WARN/.test(u))         return "warning";
  if (/DEBUG/.test(u))                return "debug";
  return "info";
}

// ── Segmented (port of components/ui/segmented.tsx) ──────────────

function Segmented({ value, options, onChange, label }) {
  return html`
    <div class="xmc-h-segmented" role="group" aria-label=${label}>
      ${options.map((opt) => {
        const v = typeof opt === "string" ? opt : opt.value;
        const lbl = typeof opt === "string" ? opt : (opt.label ?? opt.value);
        return html`
          <button
            key=${v}
            type="button"
            class=${"xmc-h-segmented__btn " + (String(value) === String(v) ? "is-active" : "")}
            onClick=${() => onChange(v)}
          >${lbl}</button>
        `;
      })}
    </div>
  `;
}

function FilterGroup({ label, children }) {
  return html`
    <div class="xmc-h-filtergrp">
      <span class="xmc-h-filtergrp__label">${label}</span>
      ${children}
    </div>
  `;
}

// ── Page ────────────────────────────────────────────────────────

export function LogsPage({ token }) {
  const [file, setFile] = useState("daemon");
  const [level, setLevel] = useState("ALL");
  const [component, setComponent] = useState("all");
  const [lineCount, setLineCount] = useState("100");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [lines, setLines] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const scrollRef = useRef(null);

  const fetchLogs = useCallback(() => {
    setLoading(true);
    setError(null);
    const qs = new URLSearchParams({
      file, lines: String(lineCount), level, component,
    });
    apiGet("/api/v2/logs?" + qs.toString(), token)
      .then((d) => {
        setLines(d.lines || []);
        setTimeout(() => {
          if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
          }
        }, 50);
      })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [file, level, component, lineCount, token]);

  useEffect(() => { fetchLogs(); }, [fetchLogs]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(fetchLogs, 5000);
    return () => clearInterval(id);
  }, [autoRefresh, fetchLogs]);

  return html`
    <section class="xmc-h-page" aria-labelledby="logs-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="logs-title" class="xmc-h-page__title">日志</h2>
          <p class="xmc-h-page__subtitle">
            尾随 daemon 日志文件。INFO/DEBUG/WARNING/ERROR 颜色区分；
            自动刷新每 5 秒拉新行。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <label class="xmc-h-cfg__switch">
            <input
              type="checkbox"
              checked=${autoRefresh}
              onChange=${(e) => setAutoRefresh(e.target.checked)}
            />
            <span class="xmc-h-cfg__switch-track"></span>
            <span class="xmc-h-cfg__switch-state">auto</span>
          </label>
          ${autoRefresh
            ? html`<span class="xmc-h-badge xmc-h-badge--success">live</span>`
            : null}
          <button
            type="button"
            class="xmc-h-btn"
            onClick=${fetchLogs}
            disabled=${loading}
          >
            <${Icon} d=${I_REFRESH} />
            刷新
          </button>
        </div>
      </header>

      <div class="xmc-h-page__body xmc-h-logs__body">
        <div class="xmc-h-logs__toolbar" role="toolbar" aria-label="过滤">
          <${FilterGroup} label="文件">
            <${Segmented}
              value=${file}
              options=${FILES}
              onChange=${setFile}
              label="文件"
            />
          </${FilterGroup}>
          <${FilterGroup} label="等级">
            <${Segmented}
              value=${level}
              options=${LEVELS}
              onChange=${setLevel}
              label="等级"
            />
          </${FilterGroup}>
          <${FilterGroup} label="组件">
            <${Segmented}
              value=${component}
              options=${COMPONENTS}
              onChange=${setComponent}
              label="组件"
            />
          </${FilterGroup}>
          <${FilterGroup} label="行数">
            <${Segmented}
              value=${lineCount}
              options=${LINE_COUNTS}
              onChange=${setLineCount}
              label="行数"
            />
          </${FilterGroup}>
        </div>

        <div class="xmc-h-card xmc-h-logs__viewer-card">
          <h3 class="xmc-h-card__title xmc-h-logs__viewer-title">
            <${Icon} d=${I_FILE} />
            ${file}.log
          </h3>
          ${error
            ? html`<div class="xmc-h-error">${error}</div>`
            : null}
          <div class="xmc-h-logs__viewer" ref=${scrollRef}>
            ${lines.length === 0 && !loading
              ? html`<p class="xmc-h-logs__empty">没有匹配的日志行。</p>`
              : lines.map((line, i) => {
                const cls = classifyLine(line);
                return html`
                  <div
                    key=${i}
                    class=${"xmc-h-logs__line xmc-h-logs__line--" + cls}
                  >${line}</div>
                `;
              })}
          </div>
        </div>
      </div>
    </section>
  `;
}
