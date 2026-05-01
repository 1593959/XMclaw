// XMclaw — SlashPopover 1:1 port of hermes-agent SlashPopover.tsx
//
// Slash-command autocomplete that floats above the chat composer.
// Type '/' → see matching commands → ↑/↓/Click to highlight, Tab to
// apply, Esc to dismiss, Enter falls through to the composer.
//
// Hermes pulls items from gw.request("complete.slash", ...). We don't
// have a backend completion route yet (Phase B-9.1 hookup), so this
// version uses a static SLASH_COMMANDS table for the canonical agent-
// runtime verbs. The keyboard surface and visual layout match Hermes
// SlashPopover.tsx:131-172.

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

// Default command set — covers the verbs the Ink TUI ships with plus
// XMclaw additions. Each entry mirrors Hermes CompletionItem shape +
// adds an `action` so applying actually does something instead of
// just stuffing text into the composer.
//
// Action types:
//   * { kind: "navigate", to: "/path" } — navigate via SPA router
//   * { kind: "store",    do: (store) => void } — mutate the store
//   * { kind: "text" }   — insert literal `text` into composer (default)
const SLASH_COMMANDS = [
  { display: "/new",      text: "/new",       meta: "新建一个会话",
    action: { kind: "store", do: (store) => store.startNewSession?.() } },
  { display: "/reset",    text: "/reset",     meta: "重置当前会话历史",
    action: { kind: "send", text: "/reset" } },
  { display: "/clear",    text: "/clear",     meta: "清空 chat 面板（保留 daemon 历史）",
    action: { kind: "store", do: (store) => store.clearChat?.() } },
  { display: "/retry",    text: "/retry",     meta: "把上一条用户消息复制到输入框，等你确认后回车重发",
    action: { kind: "store", do: (store) => store.retryLast?.() } },
  { display: "/undo",     text: "/undo",      meta: "删掉上一回合（user + assistant）— UI + daemon 历史都清",
    action: { kind: "store", do: (store) => store.undoLast?.() } },
  { display: "/plan",     text: "/plan",      meta: "切到 Plan 模式（先批准再执行）",
    action: { kind: "store", do: (store) => store.togglePlan?.(true) } },
  { display: "/act",      text: "/act",       meta: "切到 Act 模式",
    action: { kind: "store", do: (store) => store.togglePlan?.(false) } },
  { display: "/model",    text: "/model ",    meta: "切换 LLM profile" },
  { display: "/agent",    text: "/agent ",    meta: "切换运行的 agent profile" },
  { display: "/sessions", text: "/sessions",  meta: "跳到会话列表",
    action: { kind: "navigate", to: "/sessions" } },
  { display: "/skills",   text: "/skills",    meta: "跳到技能页",
    action: { kind: "navigate", to: "/skills" } },
  { display: "/cron",     text: "/cron",      meta: "跳到定时任务页",
    action: { kind: "navigate", to: "/cron" } },
  { display: "/logs",     text: "/logs",      meta: "跳到日志页",
    action: { kind: "navigate", to: "/logs" } },
  { display: "/config",   text: "/config",    meta: "跳到配置页",
    action: { kind: "navigate", to: "/config" } },
  { display: "/analytics",text: "/analytics", meta: "跳到分析页",
    action: { kind: "navigate", to: "/analytics" } },
  { display: "/docs",     text: "/docs",      meta: "跳到文档页",
    action: { kind: "navigate", to: "/docs" } },
  { display: "/help",     text: "/help",      meta: "命令清单 + 快捷键",
    action: { kind: "navigate", to: "/docs" } },
  { display: "/debug",    text: "/debug",     meta: "切换 debug toast",
    action: { kind: "store", do: (store) => store.toggleDebug?.() } },
];

function _navigate(to) {
  window.history.pushState({}, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

// Epic #24 Phase 1: dynamically-loaded "learned skills" suggestions
// (B-24, was reading from xm-auto-evo's `/api/v2/auto_evo/learned_skills`)
// removed along with system B. Phase 2 will reintroduce skill-as-slash
// suggestions backed by `SkillRegistry` (only HEAD versions of skills
// that passed evidence-gated promote). Static `SLASH_COMMANDS` keep
// working as before.

function filterCommands(input) {
  if (!input.startsWith("/")) return [];
  const q = input.slice(1).toLowerCase().trim();
  if (!q) return SLASH_COMMANDS;
  return SLASH_COMMANDS.filter((c) =>
    c.display.toLowerCase().includes(q) ||
    (c.meta || "").toLowerCase().includes(q)
  );
}

const I_CHEV = "m9 18 6-6-6-6";

function ChevIcon({ active }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-h-slash__chev " + (active ? "is-active" : "")}
         aria-hidden="true">
      <path d=${I_CHEV} />
    </svg>
  `;
}

/**
 * SlashPopover component.
 *
 * Props:
 *   input    — current composer text (string)
 *   onApply  — (nextInputText) => void
 *   onKeyHandled — optional: invoked when popover consumed a keystroke
 *
 * Use:
 *   - Mount above the composer (parent gives it absolute positioning).
 *   - Forward the composer's keydown to `popoverApi.handleKey(e)`. The
 *     hook returns true if the popover consumed the key (caller skips
 *     its own send/newline handling).
 *
 * The Preact equivalent of Hermes's forwardRef + useImperativeHandle
 * is a `usePopoverApi` hook returning `{ render, handleKey }` so the
 * Composer can call handleKey from its onKeyDown without lifting state.
 */
export function usePopoverApi({ input, onApply, store, token: _token }) {
  const [selected, setSelected] = useState(0);
  const items = useMemo(
    () => filterCommands(input || ""),
    [input]
  );
  const visible = items.length > 0 && (input || "").startsWith("/");

  // Reset selection when the items list shrinks past the active index.
  useEffect(() => {
    if (selected >= items.length) setSelected(0);
  }, [items.length, selected]);

  const apply = (item) => {
    if (!item) return;
    const a = item.action;
    if (a && a.kind === "navigate" && a.to) {
      onApply("");
      _navigate(a.to);
      return;
    }
    if (a && a.kind === "store" && typeof a.do === "function") {
      try { a.do(store || {}); } catch (_) {}
      onApply("");
      return;
    }
    if (a && a.kind === "send" && a.text) {
      // Stuff the text and let composer's Enter send it.
      onApply(a.text);
      return;
    }
    // Default: insert text and let user keep typing.
    onApply(item.text);
  };

  const handleKey = (e) => {
    if (!visible) return false;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => (s + 1) % items.length);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => (s - 1 + items.length) % items.length);
      return true;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      apply(items[selected]);
      return true;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      // Clear the slash by replacing input with empty — Hermes hides
      // via state but we don't own the input, so signalling via
      // onApply("") matches the visible behaviour (popover disappears
      // on next render because items=[]).
      onApply("");
      return true;
    }
    return false;
  };

  const render = () => {
    if (!visible) return null;
    return html`
      <div
        class="xmc-h-slash"
        role="listbox"
        aria-label="slash commands"
      >
        ${items.map((it, i) => {
          const active = i === selected;
          return html`
            <button
              key=${it.text + "-" + i}
              type="button"
              role="option"
              aria-selected=${active ? "true" : "false"}
              class=${"xmc-h-slash__item " + (active ? "is-active" : "")}
              onMouseEnter=${() => setSelected(i)}
              onMouseDown=${(e) => { e.preventDefault(); apply(it); }}
            >
              <${ChevIcon} active=${active} />
              <span class="xmc-h-slash__display">${it.display}</span>
              ${it.meta
                ? html`<span class="xmc-h-slash__meta">${it.meta}</span>`
                : null}
            </button>
          `;
        })}
      </div>
    `;
  };

  return { render, handleKey, visible };
}
