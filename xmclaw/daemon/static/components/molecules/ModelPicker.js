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

export function ModelPicker({ token, value, onChange }) {
  const [profiles, setProfiles] = useState([]);
  const [defaultId, setDefaultId] = useState(null);
  const [loaded, setLoaded] = useState(false);

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

  // Hide entirely until we know what the daemon offers — saves a flash
  // of an empty dropdown for users on the legacy single-block setup
  // who would never use this widget.
  if (!loaded || profiles.length <= 1) return null;

  return html`
    <label class="xmc-chat__model" title="本会话使用的模型 profile">
      <span class="xmc-chat__model-label">模型</span>
      <select
        class="xmc-chat__model-select"
        value=${value || ""}
        onChange=${(e) => onChange(e.target.value || null)}
      >
        <option value="">默认 (${defaultId || "未配置"})</option>
        ${profiles.map((p) => html`
          <option key=${p.id} value=${p.id}>
            ${p.label || p.id} · ${p.model}
          </option>
        `)}
      </select>
    </label>
  `;
}
