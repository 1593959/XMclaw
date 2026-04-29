// XMclaw — first-time setup banner (B-81)
//
// Polls /api/v2/setup and surfaces missing onboarding steps as a
// dismissable banner at the top of the main content area. The endpoint
// returns ``{llm_configured, persona_ready, embedding_configured,
// indexer_running, dream_running, missing: [...], ready: bool}`` —
// we surface up to three actionable items with a quick-jump button
// each.
//
// Dismiss state: localStorage "xmc-setup-dismissed-set" stores a
// JSON-encoded set of missing-item names the user already chose to
// ignore. A new missing item NOT in that set re-shows the banner —
// so dismissing "embedding" once doesn't hide future "llm" reminders.
//
// "ready: true" hides the banner unconditionally and clears the
// dismiss set so a future regression (e.g. user wipes config) gets
// surfaced fresh.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

const DISMISS_KEY = "xmc-setup-dismissed-set";

// Per-missing-item descriptor: label + (Chinese) what's broken + a
// quick-jump callback. Ordered by priority — LLM > persona > embedding.
const STEP_INFO = {
  llm: {
    title: "未配置 LLM API key",
    body: "Agent 当前以 echo 模式运行（只回显消息），需要至少一个 provider 的 API key 才能真正对话。",
    action: "前往配置",
    href: "/ui/config",
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
    action: "前往配置",
    href: "/ui/memory",
  },
};

function _readDismissed() {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (_) {
    return new Set();
  }
}

function _writeDismissed(set) {
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify(Array.from(set)));
  } catch (_) {
    /* quota / private mode — silently no-op */
  }
}

export function SetupBanner({ token }) {
  const [setup, setSetup] = useState(null);
  const [dismissed, setDismissed] = useState(_readDismissed);

  const reload = useCallback(() => {
    apiGet("/api/v2/setup", token)
      .then(setSetup)
      .catch(() => setSetup(null));
  }, [token]);

  useEffect(() => {
    reload();
    const id = setInterval(reload, 60_000);
    return () => clearInterval(id);
  }, [reload]);

  // When the daemon reports ready=true, clear any sticky dismiss state
  // so a future regression (e.g. user wipes config.json) re-surfaces
  // the banner without requiring them to remember they dismissed it.
  useEffect(() => {
    if (setup && setup.ready && dismissed.size > 0) {
      _writeDismissed(new Set());
      setDismissed(new Set());
    }
  }, [setup, dismissed]);

  if (!setup || setup.ready) return null;

  // Filter out items the user has explicitly dismissed.
  const visible = (setup.missing || []).filter((m) => !dismissed.has(m));
  if (visible.length === 0) return null;

  const onDismiss = (key) => {
    const next = new Set(dismissed);
    next.add(key);
    setDismissed(next);
    _writeDismissed(next);
  };

  const onDismissAll = () => {
    const next = new Set([...(setup.missing || [])]);
    setDismissed(next);
    _writeDismissed(next);
  };

  const onCopy = async (cmd) => {
    try {
      await navigator.clipboard.writeText(cmd);
      toast.success(`已复制到剪贴板：${cmd}`);
    } catch (_) {
      toast.info(`手动复制：${cmd}`);
    }
  };

  return html`
    <div
      class="xmc-h-setupbanner"
      role="status"
      aria-label="首次设置进度"
      style="margin:0 0 .9rem 0;padding:.7rem .9rem;border:1px solid var(--color-warning, #c8a86a);border-radius:6px;background:rgba(200,168,106,.08);font-family:var(--xmc-font-mono);font-size:.82rem"
    >
      <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.4rem">
        <strong style="letter-spacing:.05em;text-transform:uppercase;color:var(--color-warning, #c8a86a)">
          首次设置进度（${visible.length} / ${(setup.missing || []).length} 待完成）
        </strong>
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--ghost"
          style="font-size:.7rem;padding:.1rem .5rem"
          onClick=${onDismissAll}
          title="全部忽略，恢复进度后会自动重新提示"
        >
          全部忽略
        </button>
      </div>
      ${visible.map((key) => {
        const info = STEP_INFO[key];
        if (!info) return null;
        return html`
          <div
            key=${key}
            style="display:flex;justify-content:space-between;align-items:center;gap:.6rem;padding:.4rem 0;border-top:1px dashed rgba(200,168,106,.25)"
          >
            <div style="flex:1;min-width:0">
              <div style="font-weight:600">⚠ ${info.title}</div>
              <div style="margin-top:.2rem;color:var(--xmc-fg-muted);font-size:.76rem;line-height:1.5">
                ${info.body}
              </div>
            </div>
            <div style="display:flex;gap:.3rem;flex-shrink:0">
              ${info.href ? html`
                <a
                  href=${info.href}
                  class="xmc-h-btn xmc-h-btn--primary"
                  style="font-size:.72rem;padding:.2rem .55rem;text-decoration:none"
                >
                  ${info.action} →
                </a>
              ` : html`
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--primary"
                  style="font-size:.72rem;padding:.2rem .55rem"
                  onClick=${() => onCopy(info.copyCmd)}
                >
                  ${info.action}
                </button>
              `}
              <button
                type="button"
                class="xmc-h-btn xmc-h-btn--ghost"
                style="font-size:.72rem;padding:.2rem .5rem"
                onClick=${() => onDismiss(key)}
                title="只忽略这一项"
              >
                忽略
              </button>
            </div>
          </div>
        `;
      })}
    </div>
  `;
}
