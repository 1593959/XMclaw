// XMclaw — Settings / Cognition tab (Iteration 4)
//
// Surfaces cognitive-daemon tunables: autonomy_level, heartbeat_hz,
// slow_subsystem_threshold_ms. Reads from GET /api/v2/config, writes
// via PUT /api/v2/config (merges into cognition section).

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPut } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

export function CognitionSettings({ token }) {
  const [cfg, setCfg] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet("/api/v2/config", token)
      .then((d) => { setCfg(d.config || {}); setLoading(false); })
      .catch(() => setLoading(false));
  }, [token]);

  async function save(patch) {
    try {
      await apiPut("/api/v2/config", { cognition: patch }, token);
      toast.success("认知参数已保存 — daemon 实时生效");
      setCfg((prev) => ({ ...prev, cognition: { ...(prev.cognition || {}), ...patch } }));
    } catch (e) {
      toast.error("保存失败: " + String(e.message || e));
    }
  }

  if (loading) return html`<div style="padding:1rem">加载中…</div>`;

  const cog = cfg?.cognition || {};
  return html`
    <fieldset class="xmc-settings__group">
      <legend>认知参数</legend>
      <p class="xmc-settings__hint">调整 daemon 的自主度和心跳频率。保存后立即生效。</p>

      <label class="xmc-settings__field">
        <span>自主度 (autonomy_level)</span>
        <input type="range" min="0" max="1" step="0.05" value=${cog.autonomy_level ?? 0.5}
          onChange=${(e) => save({ autonomy_level: parseFloat(e.target.value) })} />
        <small class="xmc-settings__hint">当前: ${(cog.autonomy_level ?? 0.5).toFixed(2)} — 越高越主动</small>
      </label>

      <label class="xmc-settings__field">
        <span>心跳频率 (heartbeat_hz)</span>
        <input type="number" min="0.1" max="10" step="0.1" value=${cog.heartbeat_hz ?? 1.0}
          onChange=${(e) => save({ heartbeat_hz: parseFloat(e.target.value) })} />
        <small class="xmc-settings__hint">ticks / 秒</small>
      </label>

      <label class="xmc-settings__field">
        <span>慢子系统阈值 (ms)</span>
        <input type="number" min="50" max="5000" step="50" value=${cog.slow_subsystem_threshold_ms ?? 500}
          onChange=${(e) => save({ slow_subsystem_threshold_ms: parseInt(e.target.value, 10) })} />
        <small class="xmc-settings__hint">超过此值的子系统标记为慢</small>
      </label>
    </fieldset>
  `;
}
