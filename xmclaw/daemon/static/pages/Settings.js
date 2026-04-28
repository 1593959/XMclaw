// XMclaw — Settings page
//
// First page beyond Chat that gets a real implementation. Lets the user
// pick provider / api_key / base_url / default_model and write that back
// to daemon/config.json via PUT /api/v2/config/llm. The daemon answers
// with restart_required:true; we surface that prominently because the
// in-memory AgentLoop captures the LLM at construction time — a config
// change does not hot-swap into the running agent.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Button } from "../components/atoms/button.js";
import { Badge } from "../components/atoms/badge.js";
import { apiGet, apiPut } from "../lib/api.js";
import { ModelProfilesSection } from "./ModelProfiles.js";
import {
  sttSupported,
  ttsSupported,
  listVoices,
  onVoicesReady,
  getAudioPrefs,
  setAudioPrefs,
  speak,
} from "../lib/audio.js";

function AudioSection() {
  const [voices, setVoices] = useState(() => listVoices());
  const [prefs, setPrefs] = useState(() => getAudioPrefs());

  useEffect(() => onVoicesReady(setVoices), []);

  const update = (patch) => {
    const next = setAudioPrefs(patch);
    setPrefs(next);
  };

  const sample = () => {
    speak("这是 XMclaw 的语音样例。Hello from XMclaw.", {
      onError: () => {},
    });
  };

  // Group voices by language for the dropdown so a user looking for
  // a Chinese voice doesn't have to scroll past 50 English ones.
  const grouped = {};
  for (const v of voices) {
    const key = v.lang || "?";
    (grouped[key] ||= []).push(v);
  }
  const langKeys = Object.keys(grouped).sort();

  return html`
    <section class="xmc-settings__group" style="margin-top:1.5rem">
      <h3>音频（语音输入 + 朗读）</h3>
      <p class="xmc-settings__hint" style="margin-bottom:.6rem">
        基于浏览器原生 Web Speech API。
        语音输入：${sttSupported ? html`<${Badge} tone="success">支持</${Badge}>` : html`<${Badge} tone="warn">不支持</${Badge}>（建议 Chrome / Edge）`}。
        语音输出：${ttsSupported ? html`<${Badge} tone="success">支持</${Badge}>` : html`<${Badge} tone="warn">不支持</${Badge}>`}。
        所有设置存浏览器 localStorage，不上传服务端。
      </p>

      <label class="xmc-settings__field">
        <span>语言 / 区域 (BCP-47)</span>
        <input
          type="text"
          value=${prefs.lang}
          onInput=${(e) => update({ lang: e.target.value })}
          placeholder="zh-CN"
        />
        <small class="xmc-settings__hint">
          影响识别+朗读语言。例：<code>zh-CN</code> / <code>en-US</code> / <code>ja-JP</code>。
        </small>
      </label>

      <label class="xmc-settings__field">
        <span>朗读声音</span>
        <select
          value=${prefs.voiceUri}
          onChange=${(e) => update({ voiceUri: e.target.value })}
          disabled=${!ttsSupported || voices.length === 0}
        >
          <option value="">系统默认（按上方语言匹配）</option>
          ${langKeys.map((lang) => html`
            <optgroup label=${lang} key=${lang}>
              ${grouped[lang].map((v) => html`
                <option value=${v.voiceURI} key=${v.voiceURI}>
                  ${v.name}${v.localService ? " · 本地" : " · 云端"}
                </option>
              `)}
            </optgroup>
          `)}
        </select>
        <small class="xmc-settings__hint">
          ${voices.length} 个声音可选。Edge 上 zh-CN 通常有 Xiaoxiao / Yunyang 等高质量声音。
        </small>
      </label>

      <label class="xmc-settings__field">
        <span>语速 (${prefs.rate.toFixed(2)}×)</span>
        <input
          type="range"
          min="0.5"
          max="2"
          step="0.05"
          value=${String(prefs.rate)}
          onInput=${(e) => update({ rate: parseFloat(e.target.value) })}
        />
      </label>

      <label class="xmc-settings__field">
        <span>音量 (${Math.round(prefs.volume * 100)}%)</span>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value=${String(prefs.volume)}
          onInput=${(e) => update({ volume: parseFloat(e.target.value) })}
        />
      </label>

      <label class="xmc-settings__field" style="display:flex;align-items:center;gap:.6rem">
        <input
          type="checkbox"
          checked=${prefs.autoSpeak}
          onChange=${(e) => update({ autoSpeak: e.target.checked })}
        />
        <span>自动朗读 agent 回复</span>
      </label>

      <div class="xmc-settings__actions" style="margin-top:.6rem">
        <button type="button" class="xmc-h-btn" onClick=${sample} disabled=${!ttsSupported}>试听</button>
      </div>
    </section>
  `;
}

const PROVIDER_PRESETS = {
  anthropic: {
    label: "Anthropic / Claude",
    base_url_default: "https://api.anthropic.com",
    model_examples: [
      "claude-opus-4-7",
      "claude-sonnet-4-6",
      "claude-haiku-4-5-20251001",
    ],
  },
  openai: {
    label: "OpenAI / 兼容 API",
    base_url_default: "https://api.openai.com/v1",
    model_examples: [
      "gpt-4.1",
      "gpt-4o",
      "gpt-4o-mini",
    ],
  },
};

export function SettingsPage({ token }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [configPath, setConfigPath] = useState(null);

  const [provider, setProvider] = useState("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [hasExistingKey, setHasExistingKey] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiGet("/api/v2/config", token);
        if (cancelled) return;
        setConfigPath(data.config_path || null);
        const cfg = (data && data.config) || {};
        const llm = cfg.llm || {};
        const dp = llm.default_provider || "anthropic";
        setProvider(dp);
        const block = llm[dp] || {};
        // Sanitized config redacts api_key — we only know whether it's
        // set, not its value. Use a placeholder string convention.
        setHasExistingKey(!!block.api_key);
        setApiKey("");
        setBaseUrl(block.base_url || PROVIDER_PRESETS[dp]?.base_url_default || "");
        setDefaultModel(block.default_model || "");
        setError(null);
      } catch (exc) {
        if (!cancelled) setError(String(exc.message || exc));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  function onProviderChange(next) {
    setProvider(next);
    if (!baseUrl || baseUrl === PROVIDER_PRESETS[provider]?.base_url_default) {
      setBaseUrl(PROVIDER_PRESETS[next]?.base_url_default || "");
    }
    setHasExistingKey(false); // user-side state, conservatively reset
  }

  async function onSave(evt) {
    evt && evt.preventDefault && evt.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const body = {
        provider,
        base_url: baseUrl,
        default_model: defaultModel,
      };
      if (apiKey) body.api_key = apiKey;
      const res = await apiPut("/api/v2/config/llm", body, token);
      setSavedAt(Date.now());
      if (res && res.path) setConfigPath(res.path);
      setApiKey("");
      setHasExistingKey(true);
    } catch (exc) {
      setError(String(exc.message || exc));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return html`<section class="xmc-settings"><p>加载中…</p></section>`;
  }

  const preset = PROVIDER_PRESETS[provider] || {};

  return html`
    <section class="xmc-settings" aria-labelledby="settings-title">
      <header class="xmc-settings__header">
        <h2 id="settings-title">设置</h2>
        <p class="xmc-settings__subtitle">
          模型 provider 与凭据。保存后需重启 daemon 生效（Agent 在启动时绑定 LLM）。
        </p>
      </header>

      <form class="xmc-settings__form" onSubmit=${onSave}>
        <fieldset class="xmc-settings__group">
          <legend>Provider</legend>
          <div class="xmc-settings__radios">
            ${Object.entries(PROVIDER_PRESETS).map(([key, p]) => html`
              <label class="xmc-settings__radio" key=${key}>
                <input
                  type="radio"
                  name="provider"
                  value=${key}
                  checked=${provider === key}
                  onChange=${() => onProviderChange(key)}
                />
                <span>${p.label}</span>
              </label>
            `)}
          </div>
        </fieldset>

        <label class="xmc-settings__field">
          <span>API Key${hasExistingKey ? html` <${Badge} tone="success">已设置</${Badge}>` : null}</span>
          <input
            type="password"
            value=${apiKey}
            onInput=${(e) => setApiKey(e.target.value)}
            placeholder=${hasExistingKey ? "留空则保留现有 key" : "粘贴 api_key"}
            autocomplete="off"
          />
        </label>

        <label class="xmc-settings__field">
          <span>Base URL</span>
          <input
            type="text"
            value=${baseUrl}
            onInput=${(e) => setBaseUrl(e.target.value)}
            placeholder=${preset.base_url_default || ""}
          />
          <small class="xmc-settings__hint">
            兼容 API（MiniMax / DeepSeek / 自托管）填它们各自的 base url。
          </small>
        </label>

        <label class="xmc-settings__field">
          <span>默认模型</span>
          <input
            type="text"
            value=${defaultModel}
            onInput=${(e) => setDefaultModel(e.target.value)}
            placeholder="claude-opus-4-7"
            list="xmc-model-suggestions"
          />
          <datalist id="xmc-model-suggestions">
            ${(preset.model_examples || []).map((m) => html`<option key=${m} value=${m} />`)}
          </datalist>
        </label>

        <div class="xmc-settings__actions">
          <${Button}
            type="submit"
            variant="primary"
            disabled=${saving || !defaultModel.trim()}
          >${saving ? "保存中…" : "保存"}</${Button}>
          ${savedAt ? html`<${Badge} tone="success">已保存 — 重启 daemon 后生效</${Badge}>` : null}
          ${error ? html`<${Badge} tone="error">${error}</${Badge}>` : null}
        </div>

        ${configPath ? html`
          <p class="xmc-settings__hint">
            写入路径：<code>${configPath}</code>
          </p>
        ` : null}
      </form>

      <${ModelProfilesSection} token=${token} />
      <${AudioSection} />
    </section>
  `;
}
