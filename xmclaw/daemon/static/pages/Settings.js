// XMclaw — Settings page (B-148 redesign)
//
// 旧版 (B-117 之前): 顶部 Provider 单选 + Key + Base URL + Default
// Model 一段，下面再有"多模型 profiles"另一段，两段语义重叠还要
// 用户填 7 个字段。新手懵圈。
//
// 新版: 统一"模型库"卡片视图。每张卡 = 一个模型。
//   - 卡片显示: 显示名 + provider/model + ⭐default + 编辑/删除/设为默认
//   - "+ 添加模型" 按钮 → 引导式对话框（选服务商 → 自动带 base_url
//     和模型候选 → 填 key 完成）
//   - legacy llm.{provider} 块在 UI 上呈现成 id="default" 的特殊卡片
//
// 后端没变（仍走 PUT /api/v2/config/llm 写 legacy；POST /api/v2/llm/profiles
// 写命名 profile；PUT /api/v2/llm/profiles/default 切默认）。

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Button } from "../components/atoms/button.js";
import { Badge } from "../components/atoms/badge.js";
import { apiGet, apiPut } from "../lib/api.js";
import { confirmDialog } from "../lib/dialog.js";
import { toast } from "../lib/toast.js";
import { AudioSection } from "./Settings-audio.js";
import { PROVIDER_PRESETS, findPreset, presetIdFromProfile } from "../lib/model_presets.js";

async function postJson(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error || d.ok === false) {
    throw new Error(d.error || `HTTP ${r.status}`);
  }
  return d;
}

async function deleteJson(path, token) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const r = await fetch(url, { method: "DELETE" });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error || d.ok === false) {
    throw new Error(d.error || `HTTP ${r.status}`);
  }
  return d;
}

async function putJson(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const r = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error || d.ok === false) {
    throw new Error(d.error || `HTTP ${r.status}`);
  }
  return d;
}

// ── Wizard modal: pick preset → fill key → save ─────────────────

function AddModelWizard({ token, existingIds, onClose, onCreated }) {
  // Step 1: pick preset; Step 2: fill creds.
  const [step, setStep] = useState(1);
  const [presetId, setPresetId] = useState("anthropic");
  const [providerKind, setProviderKind] = useState("anthropic");
  const [modelId, setModelId] = useState("");
  const [model, setModel] = useState("");
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const pickPreset = (p) => {
    setPresetId(p.id);
    setProviderKind(p.provider_kind);
    setBaseUrl(p.base_url || "");
    setModel(p.models[0] || "");
    if (!modelId) {
      // Auto-suggest a sensible profile id like "anthropic-haiku" or "minimax"
      const slug = p.id.toLowerCase().replace(/[^a-z0-9-]/g, "_");
      let candidate = slug;
      let i = 1;
      while (existingIds.has(candidate)) {
        candidate = `${slug}_${++i}`;
      }
      setModelId(candidate);
    }
    if (!label) setLabel(p.label);
    setStep(2);
  };

  const onSubmit = async (e) => {
    e && e.preventDefault();
    if (!modelId.trim() || !model.trim()) {
      toast.error("ID 和模型名都必填");
      return;
    }
    if (existingIds.has(modelId.trim())) {
      toast.error(`ID "${modelId}" 已存在，换一个`);
      return;
    }
    if (!apiKey.trim() && presetId !== "ollama") {
      // Ollama 本地不需要 key；其他 provider 必须填或继承
      const ok = await confirmDialog({
        title: "未填 API Key",
        body: "继续保存？保存后 daemon 会尝试从同 provider 的旧配置继承 key（B-146）。如果没有可继承的，启动时会跳过这个 profile。",
        confirmLabel: "继续",
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      const body = {
        id: modelId.trim(),
        label: label.trim() || modelId.trim(),
        provider: providerKind,
        model: model.trim(),
      };
      if (baseUrl.trim()) body.base_url = baseUrl.trim();
      if (apiKey.trim()) body.api_key = apiKey.trim();
      await postJson("/api/v2/llm/profiles", token, body);
      toast.success(`模型 "${label || modelId}" 已保存 — 重启 daemon 后生效`);
      onCreated();
    } catch (err) {
      toast.error(`保存失败: ${err.message || err}`);
    } finally {
      setBusy(false);
    }
  };

  const preset = findPreset(presetId);

  return html`
    <div style="position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:200" onClick=${onClose}>
      <div style="background:var(--color-bg);border:1px solid var(--color-border);border-radius:8px;padding:1rem 1.2rem;max-width:640px;width:92%;max-height:85vh;overflow-y:auto" onClick=${(e) => e.stopPropagation()}>
        <header style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;margin-bottom:.6rem">
          <h3 style="margin:0">新增模型 <small style="opacity:.6;font-weight:normal">· 步骤 ${step}/2</small></h3>
          <button class="xmc-h-btn xmc-h-btn--ghost" style="padding:.1rem .4rem" onClick=${onClose}>×</button>
        </header>

        ${step === 1 ? html`
          <p style="margin:.2rem 0 .8rem;font-size:.85rem">先选服务商，下一步会自动填好 base_url 并给一组常用模型供选。</p>
          <div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(160px, 1fr));gap:.5rem">
            ${PROVIDER_PRESETS.map((p) => html`
              <button
                type="button"
                class="xmc-h-btn xmc-h-btn--ghost"
                key=${p.id}
                onClick=${() => pickPreset(p)}
                style="padding:.6rem;display:flex;flex-direction:column;align-items:flex-start;gap:.2rem;text-align:left;height:auto"
                title=${p.note}
              >
                <span style="font-size:1.2rem">${p.icon} <strong style="font-size:.9rem">${p.label}</strong></span>
                <small style="font-size:.65rem;opacity:.6;line-height:1.3;text-overflow:ellipsis;overflow:hidden;width:100%;white-space:nowrap">${p.note}</small>
              </button>
            `)}
          </div>
        ` : html`
          <form onSubmit=${onSubmit}>
            <div style="margin-bottom:.5rem;padding:.4rem .55rem;border-radius:4px;background:color-mix(in srgb, var(--color-primary) 6%, transparent);font-size:.78rem">
              <strong>${preset?.icon} ${preset?.label}</strong>
              ${preset?.note ? html`<div style="font-size:.7rem;opacity:.8;margin-top:.15rem">${preset.note}</div>` : null}
              <button type="button" class="xmc-h-btn xmc-h-btn--ghost" style="margin-top:.3rem;padding:.1rem .4rem;font-size:.7rem" onClick=${() => setStep(1)}>← 换一个</button>
            </div>

            <label style="display:flex;flex-direction:column;gap:.15rem;font-size:.78rem;margin-bottom:.4rem">
              <span>显示名 (chat 顶部下拉显示)</span>
              <input type="text" value=${label} onInput=${(e) => setLabel(e.target.value)} placeholder=${preset?.label} style="padding:.3rem .45rem" />
            </label>

            <label style="display:flex;flex-direction:column;gap:.15rem;font-size:.78rem;margin-bottom:.4rem">
              <span>ID <span style="color:var(--color-error,#c66)">*</span> <small style="opacity:.6">小写字母 / 数字 / -_, 全局唯一</small></span>
              <input type="text" value=${modelId} onInput=${(e) => setModelId(e.target.value)} required style="padding:.3rem .45rem;font-family:var(--xmc-font-mono);font-size:.78rem" />
            </label>

            <label style="display:flex;flex-direction:column;gap:.15rem;font-size:.78rem;margin-bottom:.4rem">
              <span>模型名 <span style="color:var(--color-error,#c66)">*</span></span>
              <input type="text" value=${model} onInput=${(e) => setModel(e.target.value)} list="xmc-model-suggest" required style="padding:.3rem .45rem;font-family:var(--xmc-font-mono);font-size:.78rem" />
              <datalist id="xmc-model-suggest">
                ${(preset?.models || []).map((m) => html`<option key=${m} value=${m} />`)}
              </datalist>
            </label>

            <label style="display:flex;flex-direction:column;gap:.15rem;font-size:.78rem;margin-bottom:.4rem">
              <span>Base URL <small style="opacity:.6">(留空使用预设)</small></span>
              <input type="text" value=${baseUrl} onInput=${(e) => setBaseUrl(e.target.value)} placeholder=${preset?.base_url} style="padding:.3rem .45rem;font-size:.78rem" />
            </label>

            <label style="display:flex;flex-direction:column;gap:.15rem;font-size:.78rem;margin-bottom:.5rem">
              <span>API Key ${presetId === "ollama" ? html`<${Badge} tone="muted">Ollama 不需要</${Badge}>` : null}</span>
              <input type="password" value=${apiKey} onInput=${(e) => setApiKey(e.target.value)} placeholder=${presetId === "ollama" ? "随便填" : "粘贴 api_key"} autocomplete="off" style="padding:.3rem .45rem" />
              <small style="opacity:.6;font-size:.65rem">不填 → daemon 会试着继承同 provider 的旧 key (B-146)</small>
            </label>

            <div style="display:flex;gap:.4rem;justify-content:flex-end">
              <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onClose} disabled=${busy}>取消</button>
              <button type="submit" class="xmc-h-btn" disabled=${busy}>${busy ? "保存中…" : "保存模型"}</button>
            </div>
          </form>
        `}
      </div>
    </div>
  `;
}

// ── Per-model card ──────────────────────────────────────────────

function ModelCard({ profile, isDefault, token, onChanged }) {
  const onSetDefault = async () => {
    try {
      await putJson("/api/v2/llm/profiles/default", token, { id: profile.id });
      toast.success(`默认模型已设为 "${profile.label || profile.id}" — 重启 daemon 生效`);
      onChanged();
    } catch (err) {
      toast.error(`切换失败: ${err.message || err}`);
    }
  };
  const onDelete = async () => {
    if (profile.id === "default") {
      toast.error("legacy 'default' 不能在这里删 — 去高级配置清空 llm.{provider} 节");
      return;
    }
    const ok = await confirmDialog({
      title: `删除模型 "${profile.label || profile.id}"？`,
      body: "操作不可撤销。删除后正在用此模型的会话会回退到 default。",
      confirmLabel: "删除",
    });
    if (!ok) return;
    try {
      await deleteJson(`/api/v2/llm/profiles/${encodeURIComponent(profile.id)}`, token);
      toast.success("已删除");
      onChanged();
    } catch (err) {
      toast.error(`删除失败: ${err.message || err}`);
    }
  };
  return html`
    <article style="margin:.5rem 0;padding:.6rem .8rem;border:1px solid ${isDefault ? "color-mix(in srgb, gold 50%, var(--color-border))" : "var(--color-border)"};border-radius:6px;background:color-mix(in srgb, var(--midground) ${isDefault ? "8%" : "3%"}, transparent)">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap">
        <div style="display:flex;align-items:baseline;gap:.4rem;flex-wrap:wrap">
          <strong style="font-size:1rem">${profile.label || profile.id}</strong>
          ${isDefault ? html`<${Badge} tone="success" title="此模型当前是 daemon 启动时的默认">⭐ 默认</${Badge}>` : null}
          <code style="font-size:.7rem;color:var(--xmc-fg-muted)">${profile.id}</code>
        </div>
        <div style="display:flex;gap:.3rem">
          ${!isDefault ? html`<button class="xmc-h-btn xmc-h-btn--ghost" style="padding:.15rem .5rem;font-size:.72rem" onClick=${onSetDefault} title="保存后重启 daemon 生效">⭐ 设为默认</button>` : null}
          <button class="xmc-h-btn xmc-h-btn--ghost" style="padding:.15rem .5rem;font-size:.72rem" onClick=${onDelete}>删除</button>
        </div>
      </div>
      <small style="display:block;margin-top:.2rem;color:var(--xmc-fg-muted);font-size:.7rem">
        Provider: <code>${profile.provider}</code> · 模型: <code>${profile.model}</code>
      </small>
    </article>
  `;
}

// ── Main page ────────────────────────────────────────────────────

export function SettingsPage({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [showWizard, setShowWizard] = useState(false);

  const load = useCallback(() => {
    apiGet("/api/v2/llm/profiles", token)
      .then(setData)
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(load, [load]);

  if (error) return html`<section class="xmc-settings"><h2>设置</h2><p style="color:var(--color-error)">${error}</p></section>`;
  if (!data) return html`<section class="xmc-settings"><p>加载中…</p></section>`;

  const profiles = data.profiles || [];
  const defaultId = data.default_id;
  const existingIds = new Set(profiles.map((p) => p.id));

  return html`
    <section class="xmc-settings" aria-labelledby="settings-title">
      <header class="xmc-settings__header">
        <h2 id="settings-title">设置</h2>
        <p class="xmc-settings__subtitle">
          模型配置（B-148 重设计）。每张卡 = 一个模型，"+ 添加模型" 走引导对话框，
          一键带好 base_url 和模型候选。可配 N 个，chat 顶部下拉切换。改完
          <strong>需重启 daemon</strong>。
        </p>
      </header>

      <section style="margin-top:1rem">
        <header style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:.5rem">
          <h3 style="margin:0">已配置模型 (${profiles.length})</h3>
          <button class="xmc-h-btn" onClick=${() => setShowWizard(true)}>+ 添加模型</button>
        </header>
        ${profiles.length === 0
          ? html`<p style="opacity:.7;padding:1rem;border:1px dashed var(--color-border);border-radius:6px;text-align:center">
              还没有模型。点 <strong>+ 添加模型</strong> 开始（Anthropic / OpenAI / MiniMax / DeepSeek / Kimi / 智谱 / Qwen / 本地 Ollama 都有预设）。
            </p>`
          : profiles.map((p) => html`
              <${ModelCard} key=${p.id} profile=${p} isDefault=${p.id === defaultId} token=${token} onChanged=${load} />
            `)}
      </section>

      ${showWizard ? html`<${AddModelWizard}
        token=${token}
        existingIds=${existingIds}
        onClose=${() => setShowWizard(false)}
        onCreated=${() => { setShowWizard(false); load(); }}
      />` : null}

      <${AudioSection} />
    </section>
  `;
}
