// XMclaw — SetupBanner data + localStorage helpers
//
// Split out of components/molecules/SetupBanner.js so the banner
// component stays under the 500-line UI budget (FRONTEND_DESIGN.md
// §1.4). The banner imports STEP_INFO + readDismissed / writeDismissed
// from here. No render concerns leak out.

export const DISMISS_KEY = "xmc-setup-dismissed-set";

// Per-missing-item descriptor: label + (Chinese) what's broken + a
// quick-jump callback. Ordered by priority — LLM > persona > embedding.
// ``form`` field opts the row into B-83's inline-expand UX: rather
// than navigating away, clicking the action toggles a form right in
// the banner. ``href`` is the fallback for items without an inline
// form (persona requires a CLI command, embedding lives on its own
// dedicated page that already has an inline form).
export const STEP_INFO = {
  llm: {
    title: "未配置 LLM API key",
    body: "Agent 当前以 echo 模式运行（只回显消息），需要至少一个 provider 的 API key 才能真正对话。",
    action: "立即配置",
    form: "llm",  // B-83: inline form
  },
  persona: {
    title: "Persona 文件未初始化",
    body: "首次安装需要运行 xmclaw onboard 创建 SOUL.md / IDENTITY.md。Agent 没有这些文件就缺少身份和工作目标。",
    action: "复制命令",
    href: null,  // copy-to-clipboard handler
    copyCmd: "xmclaw onboard",
  },
  embedding: {
    title: "向量索引未启用",
    body: "memory_search 当前只能做关键词匹配。配置一个 embedding provider 后会获得真正的语义检索。",
    action: "立即配置",
    form: "embedding",  // B-84: inline form (twin of B-83 LLM form)
  },
};

export function readDismissed() {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (_) {
    return new Set();
  }
}

export function writeDismissed(set) {
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify(Array.from(set)));
  } catch (_) {
    /* quota / private mode — silently no-op */
  }
}


// B-361 (Sprint 1): indexer error → diagnosis dispatcher.
//
// Pre-B-361 SetupBanner had ONE hard-coded fix-list (Ollama / 维度 /
// sqlite_vec) for every indexer failure mode. Real production
// failure was ``OperationalError('database is locked')`` (memory.db
// shared by PersonaStore + indexer + agent tools). User followed
// the suggested fix (delete memory.db) and got the same lock
// contention seconds later — the banner was actively misleading.
//
// Now: ``daemon`` exposes a structured ``indexer_health`` from the
// MemoryFileIndexer's ``health_status()`` method, AND the
// ``indexer_start_error`` string adapts to actual root cause. This
// helper picks the right title + fix-list based on either.
//
// Returns ``{ title: string, fixes: VNode (Preact) }`` — caller
// renders both. Centralized here so future failure modes land in
// one place, not scattered across the banner JSX.
//
// Imports html lazily so this module stays render-engine-agnostic
// for unit testing — html resolves to the Preact-bound version
// the banner already uses.

let _htm_html = null;
function _html() {
  if (_htm_html) return _htm_html;
  const { h } = window.__xmc.preact;
  _htm_html = window.__xmc.htm.bind(h);
  return _htm_html;
}

export function diagnoseIndexerError(setup) {
  const html = _html();
  const err = String((setup && setup.indexer_start_error) || "");
  const reason = (setup && setup.indexer_health || {}).unhealthy_reason || "";
  const lockHit = reason === "db_locked"
    || /database is locked/i.test(err)
    || /多 task 写竞争/.test(err);
  const embedHit = reason === "embed_failing"
    || (/embedding/i.test(err) && /(connect|timeout|http)/i.test(err));
  const startupHit = /embedder 未构造|sqlite_vec 未挂载|启动抛异常/.test(err);

  if (lockHit) {
    return {
      title: "⚠ 向量索引在跑但每次 tick 都失败 — memory.db 多 task 写竞争",
      fixes: html`<ul style="margin:.2rem 0 0 1.1rem;padding:0">
        <li>这<strong>不是</strong> sqlite_vec 未挂载、<strong>不是</strong> Ollama 没起来、<strong>不是</strong>维度冲突</li>
        <li>根因：PersonaStore + indexer + agent 工具共享一个 sqlite connection 抢写锁（B-362/B-363 永久修）</li>
        <li>临时缓解：<code>xmclaw stop &amp;&amp; xmclaw start</code>，第一次开 UI 前等 30 秒让 PersonaStore.migrate 跑完</li>
        <li>删 memory.db 没用 — 几秒后又会锁回去</li>
      </ul>`,
    };
  }
  if (embedHit) {
    return {
      title: "⚠ embedding 服务连续失败 — indexer tick 拿不到向量",
      fixes: html`<ul style="margin:.2rem 0 0 1.1rem;padding:0">
        <li>Ollama 没起来 → 终端跑 <code>ollama serve</code>（或检查 <code>memory.embedding.base_url</code>）</li>
        <li>模型本地没拉 → <code>ollama pull qwen3-embedding:0.6b</code></li>
        <li>云 endpoint 鉴权错 → 检查 api_key</li>
        <li>排除以上后看 <code>~/.xmclaw/v2/logs/xmclaw.log</code> 里的 <code>embedding.request_failed</code> 详情</li>
      </ul>`,
    };
  }
  if (startupHit) {
    return {
      title: "⚠ 向量索引启动失败（daemon 已重启过，但 indexer 起不来）",
      fixes: html`<ul style="margin:.2rem 0 0 1.1rem;padding:0">
        <li>embedder 未构造 → 检查 <code>evolution.memory.embedding</code> 节（api_key / base_url / model）</li>
        <li>sqlite_vec 未挂载 → 检查 <code>memory.enabled</code> 和 <code>sqlite-vec</code> 扩展可用性</li>
        <li>构造抛异常 → 维度跟历史数据冲突时，删 <code>~/.xmclaw/v2/memory.db</code> 重启</li>
      </ul>`,
    };
  }
  return {
    title: "⚠ 向量索引报错（未识别的失败模式）",
    fixes: html`<ul style="margin:.2rem 0 0 1.1rem;padding:0">
      <li>看下面"原始 error"找具体类型</li>
      <li>打开 Doctor 跑全套诊断</li>
      <li>仍不解 → 把 <code>~/.xmclaw/v2/logs/xmclaw.log</code> 最近 100 行贴 issue</li>
    </ul>`,
  };
}
