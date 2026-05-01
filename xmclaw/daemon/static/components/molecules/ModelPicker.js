// XMclaw — ModelPicker (chat header dropdown)
//
// Lists every LLM profile the daemon's registry has online and lets
// the user route the current chat session through one of them. The
// dropdown's value is mirrored into store.chat.llmProfileId; sendComposer
// then includes it on every WS user frame.
//
// "默认" maps to llm_profile_id=null on the wire — the daemon's
// AgentLoop falls through to its registry default in that case.

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { ModelPickerDialog } from "./ModelPickerDialog.js";

export function ModelPicker({ token, value, onChange }) {
  const [profiles, setProfiles] = useState([]);
  const [defaultId, setDefaultId] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiGet("/api/v2/llm/profiles", token);
        if (cancelled) return;
        setProfiles(data.profiles || []);
        setDefaultId(data.default_id || null);
      } catch (_exc) {
        // Don't break the chat header on a 404 / 401. Just hide the picker.
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  if (!loaded) return null;
  // B-137: even with zero / one profile, show the picker so the user
  // can SEE the current model and click "..." to open the 2-stage
  // dialog (which also catalogs every model the daemon advertises).
  // Pre-B-137 this returned null for ≤1 profile — the picker was
  // invisible to anyone who hadn't set up named profiles yet, which
  // is exactly the user who needs the affordance most.
  const empty = profiles.length === 0;
  // B-146: 之前默认选项硬写 "默认 (default)" — 用户看不出实际是
  // 哪个 model。改成显示 default profile 真实的 label/model。
  const defaultProfile = profiles.find((p) => p.id === defaultId) || null;
  const defaultLabel = defaultProfile
    ? `默认 · ${defaultProfile.label || defaultProfile.id} · ${defaultProfile.model}`
    : (empty ? "(未配置)" : "默认");

  return html`
    <label class="xmc-chat__model" title="本会话使用的模型 profile">
      <span class="xmc-chat__model-label">模型</span>
      <select
        class="xmc-chat__model-select"
        value=${value || ""}
        onChange=${(e) => onChange(e.target.value || null)}
        disabled=${empty}
        title=${empty ? "没有命名 profile — 点 ⚙ 进设置创建" : null}
      >
        <option value="">${defaultLabel}</option>
        ${profiles.map((p) => html`
          <option key=${p.id} value=${p.id}>
            ${p.id === defaultId ? "★ " : ""}${p.label || p.id} · ${p.model}
          </option>
        `)}
      </select>
      <button
        type="button"
        class="xmc-chat__model-browse"
        onClick=${() => setDialogOpen(true)}
        title=${empty ? "打开模型目录 / 创建第一个 profile" : "浏览所有可用 model"}
      >…</button>
      <a
        href="#/settings"
        class="xmc-chat__model-browse"
        style="text-decoration:none"
        title="去设置加 profile"
      >⚙</a>
      ${dialogOpen
        ? html`<${ModelPickerDialog}
            token=${token}
            currentProfileId=${value || defaultId}
            onClose=${() => setDialogOpen(false)}
            onApply=${(sel) => onChange(sel.profile_id)}
          />`
        : null}
    </label>
  `;
}
