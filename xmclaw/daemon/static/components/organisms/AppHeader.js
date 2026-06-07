// XMclaw — AppHeader (top bar with breadcrumb + page actions)
//
// Modified by Worker F (2026-06-05):
//   - Added comm status button with online indicator (.nb-header-btn)
//   - Added notification bell button (.nb-header-btn)
//   - Added focus mode toggle button (.nb-header-btn)
//   - Buttons use native title attribute for accessible tooltips
//   - Kept existing activePath + children props interface

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { NAV_GROUPS, Icon } from "./AppShell.js";

// ── 顶栏模型 / agent 快捷切换（2026-06-07）─────────────────────────────
// 解耦设计：本组件自己拉 /api/v2/llm/profiles + /api/v2/agents 填下拉；当前选中
// 值由 ChatPage 通过 window 事件 ``xmc:chat-state`` 广播过来；切换时反向派发
// ``xmc:switch-model`` / ``xmc:switch-agent``，ChatPage 监听并调用既有的
// onChangeModel / onSwitchAgent（都是会话级、热生效，无需重启）。这样无需把
// 会话 state 一路 thread 到 shell 顶栏。
const _selStyle = (
  "appearance:none;padding:3px 22px 3px 9px;font-size:.72rem;border-radius:6px;"
  + "background:var(--nb-bg-surface,#161B26);color:var(--nb-fg-primary,#F1F5F9);"
  + "border:1px solid var(--nb-border,rgba(148,163,184,.18));cursor:pointer;"
  + "font-family:var(--nb-font-mono,monospace);max-width:170px;"
  + "background-image:url('data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2712%27 height=%2712%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%2394A3B8%27 stroke-width=%272%27%3E%3Cpath d=%27M6 9l6 6 6-6%27/%3E%3C/svg%3E');"
  + "background-repeat:no-repeat;background-position:right 6px center;"
);

function HeaderSwitchers({ token }) {
  const [profiles, setProfiles] = useState([]);
  const [agents, setAgents] = useState([]);
  // 当前会话广播来的选中态（chat 未挂载时为 null → 不显示切换器）
  const [chatState, setChatState] = useState(window.__xmcChatState || null);

  const _q = (p) => p + (token ? `?token=${encodeURIComponent(token)}` : "");
  const _load = async () => {
    try {
      const [pr, ag] = await Promise.all([
        fetch(_q("/api/v2/llm/profiles")).then((r) => r.json()).catch(() => ({})),
        fetch(_q("/api/v2/agents")).then((r) => r.json()).catch(() => ({})),
      ]);
      // 运行时已加载的 profile 优先；为空则退回 on_disk（让刚加的也能选）
      const list = (pr.profiles && pr.profiles.length ? pr.profiles : pr.on_disk) || [];
      setProfiles(list);
      setAgents((ag.agents) || []);
    } catch (_) { /* 顶栏切换器失败不该影响其它 */ }
  };

  useEffect(() => { _load(); }, [token]);
  useEffect(() => {
    const onState = (e) => setChatState((e && e.detail) || window.__xmcChatState || null);
    window.addEventListener("xmc:chat-state", onState);
    // 迟挂载也能拿到最近一次状态
    if (window.__xmcChatState) setChatState(window.__xmcChatState);
    return () => window.removeEventListener("xmc:chat-state", onState);
  }, []);

  // 没有活跃会话（chat 没广播过）就不渲染切换器，避免在非对话页空显
  if (!chatState) return null;

  const curModel = chatState.llmProfileId || "default";
  const curAgent = chatState.agentId || "main";
  const fire = (name, detail) =>
    window.dispatchEvent(new CustomEvent(name, { detail }));

  return html`
    <div style="display:flex;gap:6px;align-items:center;margin-right:4px">
      <select
        style=${_selStyle}
        value=${curModel}
        title="切换模型（本会话，下一条消息生效）"
        onChange=${(e) => fire("xmc:switch-model", { profileId: e.target.value })}
      >
        <option value="default">⚙ default</option>
        ${profiles
          .filter((p) => p.id && p.id !== "default")
          .map((p) => html`<option value=${p.id}>🧠 ${p.label || p.id}${p.model ? ` · ${String(p.model).split("/").pop()}` : ""}</option>`)}
      </select>
      <select
        style=${_selStyle}
        value=${curAgent}
        title="切换对话 agent（本会话）"
        onChange=${(e) => fire("xmc:switch-agent", { agentId: e.target.value })}
      >
        <option value="main">🤖 main</option>
        ${(agents || [])
          .filter((a) => a.agent_id && a.agent_id !== "main")
          .map((a) => html`<option value=${a.agent_id}>🤝 ${a.agent_id}</option>`)}
      </select>
    </div>
  `;
}

function findPageMeta(path) {
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (item.path === path) {
        return { group: group.label, label: item.label, icon: item.icon };
      }
    }
  }
  return null;
}

export function AppHeader({ activePath, children, token, onToggleComm, onToggleNotif, onToggleFocus, focusMode, commOnline }) {
  const meta = findPageMeta(activePath);
  return html`
    <header class="xmc-h-appheader nb-header" aria-label="page header">
      <div class="xmc-h-appheader__left nb-header__left">
        ${meta
          ? html`
            <span style="opacity:.45;font-size:.7rem">${meta.group}</span>
            <span style="opacity:.3">/</span>
            <span class="xmc-h-appheader__title nb-header__title">${meta.label}</span>
          `
          : html`<span class="xmc-h-appheader__title nb-header__title">XMclaw</span>`}
      </div>
      <div class="xmc-h-appheader__right nb-header__right">
        <${HeaderSwitchers} token=${token} />
        <button
          type="button"
          class="nb-header-btn"
          onClick=${onToggleComm}
          title="通讯状态"
        >
          <span
            class="nb-status-dot"
            style=${commOnline
              ? ""
              : "background:var(--nb-error);box-shadow:0 0 8px rgba(239,68,68,0.4);animation:none;"}
          ></span>
        </button>
        <button
          type="button"
          class="nb-header-btn"
          onClick=${onToggleNotif}
          title="通知中心"
        >
          🔔
        </button>
        <button
          type="button"
          class="nb-header-btn"
          onClick=${onToggleFocus}
          title=${focusMode ? "退出专注模式" : "专注模式"}
        >
          🔦
        </button>
        ${children}
      </div>
    </header>
  `;
}
