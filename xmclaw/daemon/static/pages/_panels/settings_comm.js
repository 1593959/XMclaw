// XMclaw — Settings / Communication Integration panel (Nebula prototype port)
//
// Surfaces third-party communication tool toggles and message routing rules.
// Added as part of the Nebula prototype integration (Worker E).
// Connected to backend /api/v2/channels (2026-06-05).

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../../components/atoms/badge.js";
import { apiGet, apiSend } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

const ICON_MAP = {
  feishu: "📘",
  dingtalk: "💬",
  wecom: "💚",
  slack: "💜",
  discord: "💙",
  email: "📧",
  telegram: "✈️",
  teams: "👥",
};

const GRADIENT_MAP = {
  feishu: "linear-gradient(135deg,#00B0FF,#2979FF)",
  dingtalk: "linear-gradient(135deg,#0089FF,#00C6FF)",
  wecom: "linear-gradient(135deg,#07C160,#10B981)",
  slack: "linear-gradient(135deg,#4A154B,#611f69)",
  discord: "linear-gradient(135deg,#5865F2,#7289DA)",
  email: "linear-gradient(135deg,#FF6B00,#FF9800)",
  telegram: "linear-gradient(135deg,#0088cc,#34b7f1)",
  teams: "linear-gradient(135deg,#6264A7,#7B83EB)",
};

const ROUTES = [
  { label: "系统告警", targets: "飞书 + 钉钉 + 邮件" },
  { label: "技能完成通知", targets: "Slack + Discord" },
  { label: "日常报告", targets: "邮件 + Telegram" },
  { label: "紧急错误", targets: "全部通道" },
];

function CommToolItem({ tool, onToggle }) {
  return html`
    <div class="nb-comm-tool" onClick=${() => onToggle(tool.id)}>
      <div class="nb-comm-tool__icon" style=${`background:${tool.gradient}`}>${tool.icon}</div>
      <div class="nb-comm-tool__info">
        <div class="nb-comm-tool__name">
          ${tool.name}
          <${Badge} tone=${tool.badgeTone} style="font-size:9px;padding:1px 6px;">${tool.badgeText}<//>
        </div>
        <div class="nb-comm-tool__meta">${tool.meta}</div>
      </div>
      <div class="nb-comm-tool__status ${tool.status}">${tool.statusText}</div>
      <div class="nb-comm-tool__toggle ${tool.enabled ? "on" : ""}"></div>
    </div>
  `;
}

export function CommunicationSettings({ token }) {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) return;
    apiGet("/api/v2/channels", token)
      .then((d) => {
        const chs = Array.isArray(d) ? d : d?.channels || [];
        const mapped = chs.map((ch) => {
          const running = !!ch.running;
          const enabled = !!ch.config?.enabled;
          return {
            id: ch.id,
            name: ch.label || ch.id,
            icon: ICON_MAP[ch.id] || "📡",
            gradient: GRADIENT_MAP[ch.id] || "linear-gradient(135deg,#64748B,#94A3B8)",
            meta: `${ch.implementation_status || "unknown"}${ch.needs_tunnel ? " · 需隧道" : ""}`,
            status: running ? "connected" : enabled ? "configuring" : "disconnected",
            statusText: running ? "● 在线" : enabled ? "⚠ 配置中" : "○ 离线",
            badgeTone: running ? "success" : enabled ? "warn" : "muted",
            badgeText: running ? "已连接" : enabled ? "配置中" : "未连接",
            enabled: enabled,
          };
        });
        setTools(mapped);
        setLoading(false);
      })
      .catch(() => {
        setTools([]);
        setLoading(false);
      });
  }, [token]);

  const handleToggle = async (id) => {
    const tool = tools.find((t) => t.id === id);
    if (!tool) return;
    const nextEnabled = !tool.enabled;
    const next = tools.map((t) =>
      t.id === id
        ? {
            ...t,
            enabled: nextEnabled,
            status: nextEnabled ? "configuring" : "disconnected",
            statusText: nextEnabled ? "⚠ 配置中" : "○ 离线",
            badgeTone: nextEnabled ? "warn" : "muted",
            badgeText: nextEnabled ? "配置中" : "未连接",
          }
        : t
    );
    setTools(next);

    try {
      await apiSend("PUT", `/api/v2/channels/${encodeURIComponent(id)}`, { enabled: nextEnabled }, token);
      toast.success(`${tool.name} ${nextEnabled ? "已启用" : "已禁用"}（需重启 daemon 生效）`);
    } catch (e) {
      toast.error("保存失败: " + String(e.message || e));
      setTools(tools);
    }
  };

  if (loading) return html`<div style="padding:1rem">加载中…</div>`;

  return html`
    <section class="xmc-settings__group">
      <h3>第三方通信工具</h3>
      <p class="xmc-settings__hint">管理外部通知通道。点击行可切换开关状态。</p>

      <div style="margin-top:.6rem">
        ${tools.map((t) => html`<${CommToolItem} key=${t.id} tool=${t} onToggle=${handleToggle} />`)}
      </div>

      <div style="margin-top:1.2rem;padding:.9rem 1rem;background:var(--xmc-bg-elevated, var(--midground));border-radius:var(--xmc-radius-md);border:1px solid var(--xmc-border)">
        <h4 style="font-size:.85rem;font-weight:600;color:var(--xmc-fg-primary);margin-bottom:.4rem">📋 消息路由规则</h4>
        <p style="font-size:.75rem;color:var(--xmc-fg-subtle);line-height:1.6;margin:0">
          ${ROUTES.map((r) => html`
            <span key=${r.label} style="display:block">${r.label} → ${r.targets}</span>
          `)}
        </p>
      </div>
    </section>
  `;
}
