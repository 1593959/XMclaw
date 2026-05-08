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
