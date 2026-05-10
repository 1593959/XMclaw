// Unified memory query panel — xmclaw-architecture-redesign.md §3.3.3.
//
// Multi-axis query UI: semantic (text) + relation (anchor) + temporal
// (date range) + layer + limit. POSTs /api/v2/memory/unified_query;
// renders the merged result list with per-entry "matched_axes" badges
// so the user can see why each hit was returned.

const { h } = window.__xmc.preact;
const { useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiSend } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

const LAYERS = [
  { value: "", label: "(任意层)" },
  { value: "working", label: "工作记忆" },
  { value: "short_term", label: "短期" },
  { value: "long_term", label: "长期" },
  { value: "procedural", label: "程序" },
];

function _toUnixTs(dateInput) {
  if (!dateInput) return null;
  const t = new Date(dateInput).getTime();
  if (Number.isNaN(t)) return null;
  return t / 1000;
}

function _fmtTs(ts) {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19);
  } catch (_) {
    return String(ts);
  }
}

export function UnifiedQueryTab({ token }) {
  const [semantic, setSemantic] = useState("");
  const [relation, setRelation] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [layer, setLayer] = useState("");
  const [limit, setLimit] = useState(10);
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  async function runQuery() {
    setError(null);
    if (!semantic && !relation && !since && !until) {
      toast.warn("至少填一个轴：语义 / 关系 / 时间");
      return;
    }
    const body = { limit: Number(limit) || 10 };
    if (semantic) body.semantic = semantic;
    if (relation) body.relation = relation;
    if (since || until) {
      const tr = {};
      if (since) tr.since = _toUnixTs(since);
      if (until) tr.until = _toUnixTs(until);
      body.temporal = tr;
    }
    if (layer) body.layer = layer;

    setLoading(true);
    try {
      const r = await apiSend(
        "POST", "/api/v2/memory/unified_query", body, token,
      );
      setResults(r.results || []);
      if ((r.results || []).length === 0) {
        toast.info("没有匹配条目");
      }
    } catch (e) {
      const body = e && e.body;
      const msg = (body && (body.error || body.detail)) || String(e);
      setError(msg);
      setResults([]);
    } finally {
      setLoading(false);
    }
  }

  function clearForm() {
    setSemantic(""); setRelation("");
    setSince(""); setUntil("");
    setLayer(""); setLimit(10);
    setResults([]); setError(null);
  }

  const inputStyle = `width:100%;padding:.45rem .6rem;
    border:1px solid var(--color-border);border-radius:6px;
    background:var(--xmc-bg-soft, rgba(255,255,255,.03));
    color:var(--xmc-fg);font:inherit`;

  return html`
    <div style="display:grid;gap:1rem;max-width:820px">
      <div style="background:var(--xmc-bg-soft, rgba(255,255,255,.03));
                  border:1px solid var(--color-border);
                  border-radius:8px;padding:1rem;line-height:1.55">
        <div style="font-weight:600;margin-bottom:.4rem">
          🔎 多维度统一查询
        </div>
        <div style="font-size:.85rem;opacity:.78">
          按 <strong>语义 × 关系 × 时间 × 层级</strong> 任意组合查询记忆。
          至少给一个轴；多个轴用 dedup + score 求和合并。
          见 <code>docs/xmclaw-architecture-redesign.md §3.3.3</code>。
        </div>
      </div>

      <div style="display:grid;grid-template-columns:max-content 1fr;
                  gap:.6rem .8rem;align-items:center">
        <label style="font-weight:500">语义（向量索引）</label>
        <input type="text" style=${inputStyle}
               placeholder="e.g. 数据库优化"
               value=${semantic}
               onInput=${(e) => setSemantic(e.target.value)} />

        <label style="font-weight:500">关系锚点（图索引）</label>
        <input type="text" style=${inputStyle}
               placeholder="e.g. 项目X / 用户偏好"
               value=${relation}
               onInput=${(e) => setRelation(e.target.value)} />

        <label style="font-weight:500">时间起（时序索引）</label>
        <input type="datetime-local" style=${inputStyle}
               value=${since}
               onInput=${(e) => setSince(e.target.value)} />

        <label style="font-weight:500">时间止</label>
        <input type="datetime-local" style=${inputStyle}
               value=${until}
               onInput=${(e) => setUntil(e.target.value)} />

        <label style="font-weight:500">存储层</label>
        <select style=${inputStyle}
                value=${layer}
                onChange=${(e) => setLayer(e.target.value)}>
          ${LAYERS.map((l) => html`
            <option key=${l.value} value=${l.value}>${l.label}</option>
          `)}
        </select>

        <label style="font-weight:500">条数上限</label>
        <input type="number" min="1" max="100" style=${inputStyle}
               value=${limit}
               onInput=${(e) => setLimit(e.target.value)} />
      </div>

      <div style="display:flex;gap:.6rem">
        <button type="button"
                disabled=${loading}
                onClick=${runQuery}
                style="padding:.55rem 1.4rem;
                       background:var(--color-primary);color:#000;
                       border:none;border-radius:6px;cursor:pointer;
                       font-weight:600;
                       opacity:${loading ? 0.5 : 1}">
          ${loading ? "查询中…" : "🔎 查询"}
        </button>
        <button type="button" onClick=${clearForm}
                style="padding:.55rem 1.1rem;
                       background:transparent;
                       border:1px solid var(--color-border);
                       border-radius:6px;cursor:pointer;
                       color:var(--xmc-fg)">
          清空
        </button>
      </div>

      ${error ? html`
        <div style="padding:.6rem .8rem;
                    border:1px solid var(--xmc-danger);
                    border-radius:6px;color:var(--xmc-danger);
                    font-size:.88rem">
          错误: ${error}
        </div>
      ` : null}

      <${ResultList} results=${results} />
    </div>
  `;
}

function ResultList({ results }) {
  if (!results || !results.length) {
    return html`
      <div style="opacity:.55;font-size:.9rem;padding:1rem 0">
        填条件后点查询。结果会显示每条命中是从哪些轴匹配的。
      </div>
    `;
  }
  return html`
    <div style="display:grid;gap:.7rem">
      <div style="font-size:.82rem;opacity:.7">
        ${results.length} 条结果（按 score 降序）
      </div>
      ${results.map((r) => html`
        <div key=${r.id}
             style="padding:.7rem .9rem;
                    border:1px solid var(--color-border);
                    border-radius:7px;
                    background:var(--xmc-bg-soft, rgba(255,255,255,.02))">
          <div style="display:flex;justify-content:space-between;
                      align-items:flex-start;gap:.6rem;margin-bottom:.3rem">
            <div style="display:flex;gap:.4rem;flex-wrap:wrap">
              ${(r.matched_axes || []).map((a) => html`
                <span key=${a}
                      style="font-size:.7rem;font-weight:600;
                             padding:.1rem .4rem;border-radius:3px;
                             background:var(--color-primary);
                             color:#000">
                  ${a === "semantic" ? "语义" :
                    a === "relation" ? "关系" :
                    a === "temporal" ? "时间" : a}
                </span>
              `)}
              <span style="font-size:.7rem;opacity:.6">
                ${r.layer || "?"}
              </span>
            </div>
            <span style="font-size:.74rem;opacity:.7;font-family:var(--xmc-font-mono, monospace)">
              ${(r.score || 0).toFixed(3)}
            </span>
          </div>
          <div style="font-size:.88rem;line-height:1.5;
                      white-space:pre-wrap;word-break:break-word">
            ${r.text || "(empty)"}
          </div>
          <div style="margin-top:.3rem;font-size:.72rem;opacity:.55;
                      font-family:var(--xmc-font-mono, monospace)">
            id=${r.id} · ${_fmtTs(r.created_at)}
          </div>
        </div>
      `)}
    </div>
  `;
}
