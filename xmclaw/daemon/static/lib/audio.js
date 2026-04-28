// XMclaw — audio (STT + TTS)
//
// 100% browser-native via Web Speech API:
//   * SpeechRecognition  → microphone → live transcript
//   * SpeechSynthesis    → assistant text → spoken playback
//
// No external services. No API keys. Works offline (except the actual
// recognition engine on Chrome/Edge which proxies to a Google service —
// privacy implication: voice goes to Google when using Chrome STT).
// Firefox lacks SpeechRecognition; we feature-detect and disable the
// mic button gracefully.
//
// Persisted preferences (localStorage):
//   xmc_audio_lang        recognition + synthesis BCP-47 locale (default zh-CN)
//   xmc_audio_voice_uri   chosen synthesis voice (URI from getVoices())
//   xmc_audio_rate        synthesis rate 0.5–2.0 (default 1.0)
//   xmc_audio_volume      synthesis volume 0–1   (default 1.0)
//   xmc_audio_auto_speak  read assistant replies aloud (default false)

const LS = {
  lang:      "xmc_audio_lang",
  voiceUri:  "xmc_audio_voice_uri",
  rate:      "xmc_audio_rate",
  volume:    "xmc_audio_volume",
  autoSpeak: "xmc_audio_auto_speak",
};

function lsGet(k, fallback) {
  try {
    const v = localStorage.getItem(k);
    return v == null ? fallback : v;
  } catch {
    return fallback;
  }
}
function lsSet(k, v) {
  try { localStorage.setItem(k, String(v)); } catch {}
}

// ── feature detection ────────────────────────────────────────────────

const SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
const SS = window.speechSynthesis || null;

export const sttSupported = !!SR;
export const ttsSupported = !!SS;

// ── settings store ───────────────────────────────────────────────────

export function getAudioPrefs() {
  return {
    lang: lsGet(LS.lang, "zh-CN"),
    voiceUri: lsGet(LS.voiceUri, ""),
    rate: parseFloat(lsGet(LS.rate, "1.0")) || 1.0,
    volume: parseFloat(lsGet(LS.volume, "1.0")) || 1.0,
    autoSpeak: lsGet(LS.autoSpeak, "false") === "true",
  };
}

export function setAudioPrefs(patch) {
  const prefs = { ...getAudioPrefs(), ...patch };
  lsSet(LS.lang, prefs.lang);
  lsSet(LS.voiceUri, prefs.voiceUri || "");
  lsSet(LS.rate, prefs.rate);
  lsSet(LS.volume, prefs.volume);
  lsSet(LS.autoSpeak, prefs.autoSpeak ? "true" : "false");
  return prefs;
}

// ── TTS ──────────────────────────────────────────────────────────────

let _voicesCache = null;

export function listVoices() {
  if (!SS) return [];
  if (_voicesCache && _voicesCache.length) return _voicesCache;
  _voicesCache = SS.getVoices() || [];
  return _voicesCache;
}

// Voices populate asynchronously on Chrome — fire a callback once
// they're ready so the settings panel can show choices.
export function onVoicesReady(cb) {
  if (!SS) return () => {};
  const handler = () => {
    _voicesCache = SS.getVoices() || [];
    cb(_voicesCache);
  };
  // Already loaded?
  const initial = SS.getVoices();
  if (initial && initial.length) {
    _voicesCache = initial;
    cb(initial);
  }
  SS.addEventListener("voiceschanged", handler);
  return () => SS.removeEventListener("voiceschanged", handler);
}

let _activeUtterance = null;

export function speak(text, opts = {}) {
  if (!SS || !text) return null;
  // Cancel any in-flight utterance — speaking the next reply
  // shouldn't queue infinitely.
  try { SS.cancel(); } catch {}

  const prefs = getAudioPrefs();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = opts.lang || prefs.lang || "zh-CN";
  u.rate = clamp(opts.rate ?? prefs.rate, 0.5, 2.0);
  u.volume = clamp(opts.volume ?? prefs.volume, 0, 1);
  const voiceUri = opts.voiceUri || prefs.voiceUri;
  if (voiceUri) {
    const voice = listVoices().find((v) => v.voiceURI === voiceUri);
    if (voice) u.voice = voice;
  }

  if (opts.onEnd) u.addEventListener("end", opts.onEnd);
  if (opts.onError) u.addEventListener("error", opts.onError);

  _activeUtterance = u;
  try { SS.speak(u); } catch (e) {
    console.warn("[xmc/tts] speak failed", e);
    return null;
  }
  return u;
}

export function stopSpeaking() {
  if (!SS) return;
  try { SS.cancel(); } catch {}
  _activeUtterance = null;
}

export function isSpeaking() {
  if (!SS) return false;
  return !!SS.speaking;
}

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

// Strip markdown / code fences before TTS — the synthesizer would
// literally read out "asterisk asterisk" or "backtick" without this,
// which makes any code-heavy reply unbearable to listen to.
export function plainTextForTts(text) {
  if (!text) return "";
  let t = String(text);
  // Code fences: drop wholesale (they're long + unlistenable).
  t = t.replace(/```[\s\S]*?```/g, " (代码块) ");
  // Inline code → keep contents but strip backticks.
  t = t.replace(/`([^`]*)`/g, "$1");
  // Bold/italic markers.
  t = t.replace(/\*\*([^*]+)\*\*/g, "$1");
  t = t.replace(/\*([^*]+)\*/g, "$1");
  t = t.replace(/__([^_]+)__/g, "$1");
  t = t.replace(/_([^_]+)_/g, "$1");
  // Headings: drop the leading hashes.
  t = t.replace(/^#{1,6}\s+/gm, "");
  // Links: keep label, drop URL.
  t = t.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  // Bare URLs: short-circuit to "(链接)".
  t = t.replace(/https?:\/\/\S+/g, "(链接)");
  // Bullet markers.
  t = t.replace(/^\s*[-*+]\s+/gm, "");
  t = t.replace(/^\s*\d+\.\s+/gm, "");
  // Collapse whitespace.
  t = t.replace(/\s+/g, " ").trim();
  return t;
}

// ── STT ──────────────────────────────────────────────────────────────
//
// Returns a controller object the caller can start() / stop(). We
// don't support continuous recognition by default — the typical UX
// is "press mic, dictate one sentence, release, edit, send", not
// always-on listening.

export function createRecognizer({ onPartial, onFinal, onError, onEnd, lang } = {}) {
  if (!SR) {
    return {
      supported: false,
      start() { onError && onError(new Error("SpeechRecognition unavailable")); },
      stop() {},
      isActive: () => false,
    };
  }
  const r = new SR();
  r.lang = lang || getAudioPrefs().lang || "zh-CN";
  // Continuous=false → recognizer stops after a pause. interimResults
  // gives us the live "partial" stream while the user speaks.
  r.continuous = false;
  r.interimResults = true;
  r.maxAlternatives = 1;

  let active = false;

  r.addEventListener("result", (evt) => {
    let interim = "";
    let final = "";
    for (let i = evt.resultIndex; i < evt.results.length; i++) {
      const res = evt.results[i];
      const txt = res[0].transcript;
      if (res.isFinal) final += txt;
      else interim += txt;
    }
    if (interim && onPartial) onPartial(interim);
    if (final && onFinal) onFinal(final);
  });

  r.addEventListener("error", (evt) => {
    active = false;
    if (onError) onError(evt.error || new Error("recognition error"));
  });

  r.addEventListener("end", () => {
    active = false;
    if (onEnd) onEnd();
  });

  return {
    supported: true,
    start() {
      if (active) return;
      try {
        r.start();
        active = true;
      } catch (e) {
        // Some browsers throw "InvalidStateError" if start() is called
        // twice in a row before the previous session fully ended.
        if (onError) onError(e);
      }
    },
    stop() {
      try { r.stop(); } catch {}
      active = false;
    },
    isActive: () => active,
  };
}
