// XMclaw — Settings audio sub-section (B-148 split-out)
//
// Extracted from Settings.js to keep that file under the 500-line
// budget after the model-library redesign. Same code, different home.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import {
  sttSupported,
  ttsSupported,
  listVoices,
  onVoicesReady,
  getAudioPrefs,
  setAudioPrefs,
  speak,
} from "../lib/audio.js";


export function AudioSection() {
  const [voices, setVoices] = useState(() => listVoices());
  const [prefs, setPrefs] = useState(() => getAudioPrefs());

  useEffect(() => onVoicesReady(setVoices), []);

  const update = (patch) => {
    const next = setAudioPrefs(patch);
    setPrefs(next);
  };

  const sample = () => {
    speak("这是 XMclaw 的语音样例。Hello from XMclaw.", { onError: () => {} });
  };

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
        <input type="text" value=${prefs.lang} onInput=${(e) => update({ lang: e.target.value })} placeholder="zh-CN" />
        <small class="xmc-settings__hint">影响识别+朗读语言。例：<code>zh-CN</code> / <code>en-US</code> / <code>ja-JP</code>。</small>
      </label>

      <label class="xmc-settings__field">
        <span>朗读声音</span>
        <select value=${prefs.voiceUri} onChange=${(e) => update({ voiceUri: e.target.value })} disabled=${!ttsSupported || voices.length === 0}>
          <option value="">系统默认（按上方语言匹配）</option>
          ${langKeys.map((lang) => html`
            <optgroup label=${lang} key=${lang}>
              ${grouped[lang].map((v) => html`
                <option value=${v.voiceURI} key=${v.voiceURI}>${v.name}${v.localService ? " · 本地" : " · 云端"}</option>
              `)}
            </optgroup>
          `)}
        </select>
        <small class="xmc-settings__hint">${voices.length} 个声音可选。Edge 上 zh-CN 通常有 Xiaoxiao / Yunyang 等高质量声音。</small>
      </label>

      <label class="xmc-settings__field">
        <span>语速 (${prefs.rate.toFixed(2)}×)</span>
        <input type="range" min="0.5" max="2" step="0.05" value=${String(prefs.rate)} onInput=${(e) => update({ rate: parseFloat(e.target.value) })} />
      </label>

      <label class="xmc-settings__field">
        <span>音量 (${Math.round(prefs.volume * 100)}%)</span>
        <input type="range" min="0" max="1" step="0.05" value=${String(prefs.volume)} onInput=${(e) => update({ volume: parseFloat(e.target.value) })} />
      </label>

      <label class="xmc-settings__field" style="display:flex;align-items:center;gap:.6rem">
        <input type="checkbox" checked=${prefs.autoSpeak} onChange=${(e) => update({ autoSpeak: e.target.checked })} />
        <span>自动朗读 agent 回复</span>
      </label>

      <div class="xmc-settings__actions" style="margin-top:.6rem">
        <button type="button" class="xmc-h-btn" onClick=${sample} disabled=${!ttsSupported}>试听</button>
      </div>
    </section>
  `;
}
