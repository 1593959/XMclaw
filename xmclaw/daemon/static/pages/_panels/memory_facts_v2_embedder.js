// Memory v2 — embedder config inspection panel.
//
// Wave 27 follow-up. User complaint: "向量数据库配置向量模型的接口
// 和端口不明晰，根本不知道配置的是什么向量模型". This panel surfaces
// the entire vector-embedding configuration in plain view + a Test
// button to round-trip a probe through the running embedder.
//
// What it shows:
//   * provider name (openai / dashscope / ollama / ...)
//   * model id (text-embedding-3-small / qwen3-embedding-0.6b / ...)
//   * dim (1024, 1536, 3072 — whatever's actually live)
//   * base_url (the actual HTTP endpoint requests fly to)
//   * api_key set + masked preview (first 4 + last 4 chars)
//   * max_batch_size / timeout_s
//   * Cache hit rate + size (the LRU layer added in Phase 1b)
//   * Failure count
//
// What it does NOT show:
//   * Raw api_key (security — masked only)
//   * Embedding vectors (privacy — probe sample is first 4 floats)
//
// Configuration changes still go through daemon/config.json — this
// panel is read-only for now. Editing in-UI lands in a future
// follow-up (would need to gate behind explicit confirm + restart).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost } from "../../lib/api.js";


// ── Row helper ───────────────────────────────────────────────────


function Row({ label, value, code = false, mono = false, dim = false }) {
  const valStyle = [
    "word-break:break-all",
    mono ? "font-family:var(--theme-font-mono, monospace)" : "",
    dim ? "color:var(--xmc-fg-muted)" : "",
  ].filter(Boolean).join(";");
  return html`
    <div style="display:flex;gap:1rem;padding:.35rem 0;border-bottom:1px solid color-mix(in srgb, var(--color-border) 50%, transparent)">
      <div style="min-width:140px;color:var(--xmc-fg-muted);font-size:.85rem">${label}</div>
      <div style=${"flex:1;font-size:.85rem;" + valStyle}>
        ${code ? html`<code>${value}</code>` : value}
      </div>
    </div>
  `;
}


// ── Test result block ────────────────────────────────────────────


function TestResult({ result }) {
  if (!result) return null;
  if (result.ok) {
    return html`
      <div style="margin-top:.6rem;padding:.6rem .8rem;border:1px solid var(--color-success);border-radius:6px;background:color-mix(in srgb, var(--color-success) 8%, transparent)">
        <strong>✓ embed 成功</strong>
        <div style="font-size:.78rem;margin-top:.3rem;color:var(--xmc-fg-muted)">
          返回维度: <code>${result.returned_dim}</code> · 耗时 <code>${result.elapsed_ms} ms</code>
        </div>
        <div style="font-size:.72rem;margin-top:.25rem">
          vec[0..4] = [${result.sample.join(", ")}…]
        </div>
      </div>
    `;
  }
  return html`
    <div style="margin-top:.6rem;padding:.6rem .8rem;border:1px solid var(--color-destructive);border-radius:6px;background:color-mix(in srgb, var(--color-destructive) 8%, transparent)">
      <strong>✗ embed 失败</strong>
      <div style="font-size:.78rem;margin-top:.3rem">${result.error}</div>
      <div style="font-size:.72rem;color:var(--xmc-fg-muted);margin-top:.2rem">
        耗时 ${result.elapsed_ms} ms
      </div>
    </div>
  `;
}


// ── Main ─────────────────────────────────────────────────────────


export function EmbedderInfoPanel({ token }) {
  const [info, setInfo] = useState(null);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await apiGet("/api/v2/memory/v2/embedder", token);
      setInfo(r);
    } catch (e) {
      setInfo({ configured: false, reason: String(e?.message || e) });
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await apiPost(
        "/api/v2/memory/v2/embedder/test",
        { text: "test probe — 验证 embedder 可达性" },
        token,
      );
      setTestResult(r);
      // Refresh stats (cache stats moved after the probe).
      refresh();
    } catch (e) {
      setTestResult({ ok: false, error: String(e?.message || e), elapsed_ms: 0 });
    } finally {
      setTesting(false);
    }
  };

  if (loading) {
    return html`<div style="opacity:.7;padding:1rem 0">加载 embedder 配置…</div>`;
  }

  if (!info || !info.configured) {
    return html`
      <div class="xmc-h-warn" role="alert" style="padding:.7rem .9rem;border:1px dashed var(--color-warning,#c98a3a);border-radius:6px;margin:.6rem 0">
        <strong>向量 embedder 未配置</strong>
        <div style="font-size:.85rem;margin-top:.3rem">
          ${info?.reason || "在 daemon/config.json 的 evolution.memory.embedding 节配置 provider/model/api_key/base_url 后重启 daemon。"}
        </div>
      </div>
    `;
  }

  return html`
    <section style="margin-top:.6rem">
      <header style="display:flex;align-items:center;gap:.6rem;margin-bottom:.4rem">
        <strong>向量 embedder 配置</strong>
        <button
          type="button"
          class="xmc-h-btn"
          onClick=${refresh}
          style="font-size:.78rem;padding:.3rem .55rem"
        >🔄 刷新</button>
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--primary"
          onClick=${runTest}
          disabled=${testing}
          style="font-size:.78rem;padding:.3rem .55rem"
        >${testing ? "测试中…" : "🧪 round-trip 测试"}</button>
      </header>

      <div style="border:1px solid var(--color-border);border-radius:6px;padding:.5rem .9rem;background:var(--xmc-bg-elev)">
        <${Row} label="provider" value=${info.provider} code=${true} />
        <${Row} label="model" value=${info.model || "(default)"} code=${true} mono=${true} />
        <${Row} label="dim (维度)" value=${info.dim} code=${true} />
        <${Row} label="base_url (HTTP 端点)" value=${info.base_url || "(default OpenAI)"} mono=${true} />
        <${Row}
          label="api_key"
          value=${info.api_key_set ? html`<span><code>${info.api_key_masked}</code> <span style="opacity:.6">· (已设置, 屏蔽显示)</span></span>` : html`<span style="color:var(--color-warning,#c98a3a)">⚠ 未设置 (本地 endpoint 可豁免)</span>`}
        />
        <${Row} label="max_batch_size" value=${info.max_batch_size} code=${true} dim=${true} />
        <${Row} label="timeout_s" value=${info.timeout_s} code=${true} dim=${true} />
      </div>

      <div style="margin-top:.6rem;padding:.5rem .9rem;border:1px solid var(--color-border);border-radius:6px;background:var(--xmc-bg-elev)">
        <div style="font-size:.78rem;color:var(--xmc-fg-muted);margin-bottom:.3rem">
          <strong>EmbeddingService LRU 缓存</strong> (Phase 1b)
        </div>
        <div style="display:flex;gap:1.5rem;flex-wrap:wrap;font-size:.85rem">
          <span>hit rate: <strong>${(info.cache.hit_rate * 100).toFixed(1)}%</strong></span>
          <span>hits / misses: <strong>${info.cache.hits} / ${info.cache.misses}</strong></span>
          <span>size / cap: <strong>${info.cache.size} / ${info.cache.capacity}</strong></span>
          ${info.failures > 0
            ? html`<span style="color:var(--color-destructive)">failures: <strong>${info.failures}</strong></span>`
            : null}
        </div>
      </div>

      <${TestResult} result=${testResult} />

      <details style="margin-top:.6rem;font-size:.85rem">
        <summary style="cursor:pointer;color:var(--xmc-fg-muted)">如何修改配置</summary>
        <div style="margin-top:.4rem;padding:.5rem .8rem;border-left:3px solid var(--color-primary);background:color-mix(in srgb, var(--color-primary) 4%, transparent)">
          编辑 <code>daemon/config.json</code> 的
          <code>evolution.memory.embedding</code> 节：
          <pre style="margin:.4rem 0;font-size:.78rem;overflow:auto"><code>${`{
  "evolution": {
    "memory": {
      "embedding": {
        "provider": "openai",
        "api_key": "sk-...",
        "base_url": "https://api.openai.com/v1",
        "model": "text-embedding-3-small",
        "dimensions": 1536
      }
    }
  }
}`}</code></pre>
          常用替代：
          <ul style="margin:.3rem 0;padding-left:1.2rem">
            <li><strong>DashScope</strong> (Alibaba): base_url <code>https://dashscope.aliyuncs.com/compatible-mode/v1</code>，model <code>text-embedding-v3</code>，dim 1024</li>
            <li><strong>Ollama 本地</strong>: base_url <code>http://localhost:11434/v1</code>，model <code>nomic-embed-text</code>，dim 768，无需 api_key</li>
            <li><strong>BGE / Qwen 本地</strong>: 用 vllm/Xinference 拉起 OpenAI-compat 端点指过去即可</li>
          </ul>
          改完重启 daemon 生效。
        </div>
      </details>
    </section>
  `;
}
