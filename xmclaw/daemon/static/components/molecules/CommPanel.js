// XMclaw — CommPanel (communication status dropdown)
//
// Worker F (2026-06-05): Nebula prototype port.
// Fixed-position panel, top-right below header.
// WebSocket status, message stats, active sessions, third-party tools.

const { h } = window.__xmc.preact;
const { useState, useCallback, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";

const WS_CONNECTIONS = [
  { name: "主通道", url: "ws://127.0.0.1:8766/ws", status: "online", ping: 12 },
  { name: "事件流", url: "ws://127.0.0.1:8766/events", status: "online", ping: 8 },
  { name: "日志流", url: "ws://127.0.0.1:8766/logs", status: "offline", ping: null },
];

const STATS = [
  { label: "发送", value: "1,247" },
  { label: "接收", value: "1,189" },
  { label: "重连", value: "3" },
  { label: "丢失", value: "0" },
];

const ICON_MAP = {
  feishu: "📘",
  dingtalk: "💬",
  wecom: "💚",
  slack: "💜",
  discord: "💙",
  email: "📧",
  telegram: "✈️",
  teams: "🔷",
};

const COLOR_MAP = {
  feishu: "linear-gradient(135deg,#00B0FF,#2979FF)",
  dingtalk: "linear-gradient(135deg,#0089FF,#00C6FF)",
  wecom: "linear-gradient(135deg,#07C160,#10B981)",
  slack: "linear-gradient(135deg,#4A154B,#611f69)",
  discord: "linear-gradient(135deg,#5865F2,#7289DA)",
  email: "linear-gradient(135deg,#FF6B00,#FF9800)",
  telegram: "linear-gradient(135deg,#0088CC,#00A8E8)",
  teams: "linear-gradient(135deg,#6264A7,#7B7FC7)",
};

const SESSIONS = [
  { name: "批量压缩图片", meta: "42 消息 · 14:36", active: true },
  { name: "记忆系统调研", meta: "128 消息 · 2h 前", active: false },
  { name: "技能自主调用", meta: "56 消息 · 1d 前", active: false },
];

export function CommPanel({ onClose, token }) {
  const [channels, setChannels] = useState(null);
  const [toolStates, setToolStates] = useState(() => []);

  useEffect(() => {
    if (!token) return;
    apiGet("/api/v2/channels", token)
      .then((data) => {
        const chs = Array.isArray(data) ? data : data?.channels || [];
        setChannels(chs);
        setToolStates(chs.map((c) => !!c.running));
      })
      .catch(() => {
        setChannels([]);
        setToolStates([]);
      });
  }, [token]);

  const toggleTool = useCallback((idx) => {
    setToolStates((prev) => {
      const next = [...prev];
      next[idx] = !next[idx];
      return next;
    });
  }, []);

  const tools = (channels || []).map((ch) => ({
    icon: ICON_MAP[ch.id] || "📡",
    name: ch.label || ch.id,
    status: ch.running ? "connected" : ch.config?.enabled ? "configuring" : "disconnected",
    meta: `${ch.implementation_status || "unknown"}${ch.needs_tunnel ? " · 需隧道" : ""}`,
    color: COLOR_MAP[ch.id] || "linear-gradient(135deg,#64748B,#94A3B8)",
  }));

  return html`
    <div class="nb-comm-panel show" role="dialog" aria-label="通讯面板">
      <div class="nb-comm-header">
        <h3>📡 通讯状态</h3>
        <button type="button" onClick=${onClose} title="关闭">✕</button>
      </div>
      <div class="nb-comm-body">
        <div class="nb-comm-section">
          <h4>WebSocket 连接</h4>
          ${WS_CONNECTIONS.map(
            (conn) => html`
              <div class="nb-comm-row" key=${conn.name}>
                <span class=${"nb-comm-status " + conn.status}></span>
                <span>${conn.name} · ${conn.url}</span>
                <span class="nb-comm-ping"
                  >${conn.ping != null ? conn.ping + "ms" : "--"}</span
                >
              </div>
            `
          )}
        </div>

        <div class="nb-comm-section">
          <h4>消息统计</h4>
          <div class="nb-comm-stats">
            ${STATS.map(
              (s) => html`
                <div class="nb-comm-stat" key=${s.label}>
                  <div class="nb-comm-stat__value">${s.value}</div>
                  <div class="nb-comm-stat__label">${s.label}</div>
                </div>
              `
            )}
          </div>
        </div>

        <div class="nb-comm-section">
          <h4>第三方通信工具</h4>
          ${channels === null
            ? html`<div style="padding:8px 0;font-size:12px;opacity:.6;">加载中…</div>`
            : tools.length === 0
              ? html`<div style="padding:8px 0;font-size:12px;opacity:.6;">暂无通信通道配置</div>`
              : tools.map(
                  (tool, idx) => html`
                    <div
                      class="nb-comm-tool"
                      key=${tool.name}
                      onClick=${() => toggleTool(idx)}
                    >
                      <div
                        class="nb-comm-tool__icon"
                        style=${"background:" + tool.color}
                      >
                        ${tool.icon}
                      </div>
                      <div class="nb-comm-tool__info">
                        <div class="nb-comm-tool__name">
                          ${tool.name}
                          <span
                            class=${"nb-badge " +
                            (tool.status === "connected"
                              ? "nb-badge--green"
                              : tool.status === "configuring"
                                ? "nb-badge--amber"
                                : "nb-badge--purple")}
                          >
                            ${tool.status === "connected"
                              ? "已连接"
                              : tool.status === "configuring"
                                ? "配置中"
                                : "未连接"}
                          </span>
                        </div>
                        <div class="nb-comm-tool__meta">${tool.meta}</div>
                      </div>
                      <div
                        class=${"nb-comm-tool__status " +
                        (toolStates[idx] ? "connected" : "disconnected")}
                      >
                        ${toolStates[idx] ? "● 在线" : "○ 离线"}
                      </div>
                      <div
                        class=${"nb-comm-tool__toggle " +
                        (toolStates[idx] ? "on" : "")}
                      ></div>
                    </div>
                  `
                )}
        </div>

        <div class="nb-comm-section">
          <h4>活动会话</h4>
          ${SESSIONS.map(
            (sess) => html`
              <div class="nb-comm-session" key=${sess.name}>
                <div
                  class=${"nb-comm-session__dot " +
                  (sess.active ? "active" : "")}
                ></div>
                <div class="nb-comm-session__info">
                  <div class="nb-comm-session__name">${sess.name}</div>
                  <div class="nb-comm-session__meta">${sess.meta}</div>
                </div>
              </div>
            `
          )}
        </div>

        <div class="nb-comm-actions">
          <button class="nb-comm-btn" onClick=${() => {}}>
            📡 发送 Ping
          </button>
          <button class="nb-comm-btn" onClick=${() => {}}>
            🔄 强制重连
          </button>
          <button class="nb-comm-btn danger" onClick=${() => {}}>
            ⏹ 断开全部
          </button>
        </div>
      </div>
    </div>
  `;
}
