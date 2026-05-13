// XMclaw — continuous voice loop (Sprint 2 Wave 7).
//
// Wraps audio.js's one-shot recognizer + speak() into a hands-free
// "press mic once, talk back and forth" state machine:
//
//   idle ──start()──▶ listening
//   listening ──onFinal──▶ submitting   (caller's onUtterance fires;
//                                        caller should send to agent)
//   submitting ──deliverReply(text)──▶ speaking
//   speaking ──TTS onEnd──▶ listening   (auto-restart recognizer)
//   listening ──stop()──▶ idle          (any state → stop → idle)
//
// Caller wiring:
//
//   const loop = createVoiceLoop({
//     lang: "zh-CN",
//     onUtterance: (text) => composer.send(text),
//     onPhaseChange: (phase) => setPhase(phase),
//   });
//   loop.start();
//   // ... user speaks → onUtterance fires → caller sends to agent
//   // ... agent reply lands:
//   loop.deliverReply(replyText);
//   // ... TTS plays → recognizer auto-restarts → loop continues
//
// Caller MUST call deliverReply() (or cancel()) after onUtterance fires;
// otherwise the loop stays in "submitting" forever and the user
// can't speak again. Pass empty string to skip TTS but restart
// listening.
//
// Browser dependencies:
//   - SpeechRecognition (createRecognizer): Chrome / Edge / Safari TP.
//     Firefox lacks it — the loop reports unsupported and refuses
//     to start.
//   - SpeechSynthesis (speak): all major browsers.
//
// Lifecycle / cleanup: caller must stop() in component teardown
// to release the mic + cancel the TTS utterance.

import {
  createRecognizer,
  sttSupported,
  speak,
  stopSpeaking,
  isSpeaking,
  plainTextForTts,
  getAudioPrefs,
} from "./audio.js";

export const PHASES = Object.freeze({
  IDLE: "idle",
  LISTENING: "listening",
  SUBMITTING: "submitting",
  SPEAKING: "speaking",
});

export const voiceLoopSupported = sttSupported;

// Delay between recognizer end and restart. ChromeSR throws
// InvalidStateError if you call start() immediately after end fires;
// 150ms is the empirical sweet spot.
const RESTART_DELAY_MS = 150;

// After TTS finishes, give the audio output a beat to settle so the
// recognizer doesn't pick up the tail-end of the synthesized voice
// through the speaker → mic loopback. Tunable, but 250ms is enough on
// most laptop speakers without padded fan noise.
const POST_TTS_DELAY_MS = 250;

export function createVoiceLoop({
  lang,
  onUtterance,
  onPhaseChange,
  onError,
} = {}) {
  if (!sttSupported) {
    return {
      supported: false,
      start() {
        if (onError) onError(new Error("SpeechRecognition unavailable"));
      },
      stop() {},
      deliverReply() {},
      cancel() {},
      isActive: () => false,
      getPhase: () => PHASES.IDLE,
    };
  }

  let phase = PHASES.IDLE;
  let rec = null;
  let restartTimer = null;
  let postTtsTimer = null;
  let stopped = false;

  function setPhase(next) {
    if (phase === next) return;
    phase = next;
    if (onPhaseChange) {
      try { onPhaseChange(next); } catch { /* swallow */ }
    }
  }

  function clearTimers() {
    if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
    if (postTtsTimer) { clearTimeout(postTtsTimer); postTtsTimer = null; }
  }

  function startRecognizer() {
    if (stopped) return;
    clearTimers();
    setPhase(PHASES.LISTENING);
    rec = createRecognizer({
      lang: lang || getAudioPrefs().lang || "zh-CN",
      onFinal: (text) => {
        const trimmed = (text || "").trim();
        if (!trimmed) {
          // Silence-only utterance — just restart instead of submitting
          // an empty message that would no-op against the agent.
          return;
        }
        setPhase(PHASES.SUBMITTING);
        if (onUtterance) {
          try { onUtterance(trimmed); } catch (e) {
            if (onError) onError(e);
          }
        }
      },
      onError: (err) => {
        // "no-speech" / "aborted" are routine — just restart.
        const msg = (err && err.message) || String(err) || "";
        if (msg === "no-speech" || msg === "aborted") {
          scheduleRestart();
          return;
        }
        if (onError) onError(err);
        // Hard errors still try to recover after a brief pause.
        scheduleRestart();
      },
      onEnd: () => {
        // Natural end (pause detected). If we're still in listening
        // phase (no utterance recognized), restart. If we've moved to
        // SUBMITTING or SPEAKING, the recognizer end is expected —
        // wait for deliverReply() to drive us back to listening.
        if (phase === PHASES.LISTENING) {
          scheduleRestart();
        }
      },
    });
    rec.start();
  }

  function scheduleRestart() {
    if (stopped) return;
    if (restartTimer) return;
    restartTimer = setTimeout(() => {
      restartTimer = null;
      if (!stopped) startRecognizer();
    }, RESTART_DELAY_MS);
  }

  return {
    supported: true,
    start() {
      stopped = false;
      startRecognizer();
    },
    stop() {
      stopped = true;
      clearTimers();
      if (rec) {
        try { rec.stop(); } catch { /* ignore */ }
        rec = null;
      }
      try { stopSpeaking(); } catch { /* ignore */ }
      setPhase(PHASES.IDLE);
    },
    cancel() {
      // Caller's onUtterance fired but they're not going to call
      // deliverReply (e.g. the agent send was aborted). Restart
      // listening immediately.
      if (rec) {
        try { rec.stop(); } catch { /* ignore */ }
        rec = null;
      }
      scheduleRestart();
    },
    deliverReply(replyText) {
      if (stopped) return;
      const clean = plainTextForTts(replyText || "");
      if (!clean) {
        scheduleRestart();
        return;
      }
      setPhase(PHASES.SPEAKING);
      // Make sure recognizer is stopped so TTS playback doesn't get
      // mic'd back in.
      if (rec) {
        try { rec.stop(); } catch { /* ignore */ }
        rec = null;
      }
      speak(clean, {
        lang: lang || getAudioPrefs().lang || "zh-CN",
        onEnd: () => {
          if (stopped) return;
          postTtsTimer = setTimeout(() => {
            postTtsTimer = null;
            if (!stopped) startRecognizer();
          }, POST_TTS_DELAY_MS);
        },
        onError: () => {
          if (!stopped) scheduleRestart();
        },
      });
      // If speak() failed synchronously (no SS support), fall back to
      // immediate restart.
      if (!isSpeaking()) {
        scheduleRestart();
      }
    },
    isActive: () => !stopped && phase !== PHASES.IDLE,
    getPhase: () => phase,
  };
}
