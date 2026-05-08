// XMclaw — Memory page: vector-indexer status + embedding form
// (B-323 follow-up split out of memory_providers.js).
//
// Pure presentation. Parent owns ``indexer`` snapshot, ``embForm``
// state, ``showEmb`` toggle, ``embSaving`` flag, and the
// ``onSaveEmbedding`` async handler. We just render and call back.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);


export function VectorIndexerCard({
  indexer,
  showEmb, setShowEmb,
  embForm, setEmbForm,
  embSaving, onSaveEmbedding,
}) {
  if (!indexer) return null;
  return html`
    <div class="xmc-h-card" style="padding:.6rem .8rem;margin:.6rem 0;background:var(--color-bg);border-left:3px solid var(--color-primary, #6aa3f0)">
      <strong style="font-size:.85rem">向量索引（B-41/B-43）</strong>
      ${indexer.wired
        ? html`
            <div style="margin-top:.3rem;display:flex;gap:.6rem;flex-wrap:wrap;font-size:.8rem">
              <span class="xmc-h-badge xmc-h-badge--${indexer.running ? 'success' : 'warn'}">
                ${indexer.running ? '运行中' : '未运行'}
              </span>
              <span class="xmc-datapage__subtitle">监视文件: <strong>${indexer.watched_count}</strong></span>
              <span class="xmc-datapage__subtitle">已索引: <strong>${indexer.known_count}</strong></span>
              <span class="xmc-datapage__subtitle">轮询: <strong>${indexer.poll_interval_s}s</strong></span>
            </div>
          `
        : html`
            <div style="margin-top:.3rem;color:var(--xmc-fg-muted);font-size:.78rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
              <span>⚠ ${indexer.reason || '未启用'}</span>
              <button
                type="button"
                class="xmc-h-btn xmc-h-btn--ghost"
                style="font-size:.7rem;padding:.15rem .5rem"
                onClick=${() => setShowEmb((v) => !v)}
              >
                ${showEmb ? '收起' : '配置 embedding'}
              </button>
            </div>
            ${showEmb ? html`
              <div style="margin-top:.6rem;display:grid;grid-template-columns:auto 1fr;gap:.4rem .6rem;align-items:center;font-size:.78rem">
                <label>provider</label>
                <select
                  value=${embForm.provider}
                  onChange=${(e) => setEmbForm({ ...embForm, provider: e.target.value })}
                  class="xmc-h-input"
                >
                  <option value="openai">openai (covers Ollama / vLLM / DashScope)</option>
                </select>
                <label>base_url</label>
                <input
                  type="text"
                  class="xmc-h-input"
                  value=${embForm.base_url}
                  placeholder="http://127.0.0.1:11434/v1"
                  onInput=${(e) => setEmbForm({ ...embForm, base_url: e.target.value })}
                />
                <label>model</label>
                <input
                  type="text"
                  class="xmc-h-input"
                  value=${embForm.model}
                  placeholder="qwen3-embedding:0.6b"
                  onInput=${(e) => setEmbForm({ ...embForm, model: e.target.value })}
                />
                <label>dimensions</label>
                <input
                  type="number"
                  class="xmc-h-input"
                  value=${embForm.dimensions}
                  min="1"
                  onInput=${(e) => setEmbForm({ ...embForm, dimensions: Number(e.target.value) || 0 })}
                />
                <label>api_key</label>
                <input
                  type="password"
                  class="xmc-h-input"
                  value=${embForm.api_key}
                  placeholder="（Ollama 本地不需要）"
                  onInput=${(e) => setEmbForm({ ...embForm, api_key: e.target.value })}
                />
              </div>
              <div style="margin-top:.6rem;display:flex;gap:.4rem;justify-content:flex-end">
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--ghost"
                  style="font-size:.75rem"
                  onClick=${() => setShowEmb(false)}
                >取消</button>
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--primary"
                  style="font-size:.75rem"
                  disabled=${embSaving}
                  onClick=${onSaveEmbedding}
                >${embSaving ? '保存中…' : '保存（需重启 daemon）'}</button>
              </div>
              <div style="margin-top:.4rem;font-size:.7rem;color:var(--xmc-fg-muted)">
                提示：dimensions 必须和模型实际输出维度一致——qwen3-embedding:0.6b = 1024，text-embedding-3-small = 1536。
              </div>
            ` : null}
          `}
    </div>
  `;
}
