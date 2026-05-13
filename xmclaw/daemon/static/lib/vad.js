// XMclaw — energy-based Voice Activity Detection (Wave 14).
//
// Why: Wave 7 leans on SpeechRecognition's built-in pause detection,
// which is fine in a quiet room but trips constantly on TV noise /
// fan hum / nearby chatter. This module sits in front of the
// recognizer and emits clean voice-start / voice-end events derived
// from short-window RMS energy.
//
// Use shapes:
//   * Stand-alone — for "press to talk" with auto-stop. Caller gates
//     downstream STT on speech events.
//   * Inside voice_loop — replace the recognizer's natural end with
//     VAD's end so we stop listening as soon as the user pauses,
//     even when the SpeechRecognition implementation hasn't decided
//     yet.
//
// Algorithm (intentionally simple):
//   1. AudioContext + ScriptProcessor / AnalyserNode polls 50Hz
//   2. For each frame: rms = sqrt(mean(sample^2))
//   3. First 1 s = ambient calibration → ``noise_floor``
//   4. speech_threshold = max(noise_floor * 3.0, MIN_THRESHOLD)
//   5. State machine: idle → above → speaking; speaking → below for
//      ``hangoverMs`` → ended.
//
// We don't ship a wasm VAD model — overkill for the use case + bigger
// asset footprint. Energy-based gets 90% of the value at 2% of the
// complexity. If the user really wants noisy bar / cafe quality, swap
// the inner detector for webrtc-vad-wasm — the same VAD interface
// (onSpeechStart / onSpeechEnd) covers it.

const MIN_THRESHOLD = 0.015;        // absolute floor (mic noise on most
                                     // laptops sits around 0.005-0.01)
const CALIBRATE_MS = 1000;          // initial ambient sampling window
const FRAME_INTERVAL_MS = 20;       // poll @ 50Hz
const FFT_SIZE = 512;               // AnalyserNode time-domain buffer

export const vadSupported = !!(
  typeof window !== "undefined"
  && (window.AudioContext || window.webkitAudioContext)
  && typeof navigator !== "undefined"
  && navigator.mediaDevices
  && navigator.mediaDevices.getUserMedia
);

export const VAD_STATES = Object.freeze({
  IDLE: "idle",
  CALIBRATING: "calibrating",
  LISTENING: "listening",
  SPEAKING: "speaking",
});

/**
 * Start a VAD session.
 *
 * @param {object} opts
 * @param {function(): void} [opts.onSpeechStart]
 * @param {function({duration_ms:number, peak:number}): void} [opts.onSpeechEnd]
 * @param {function({level:number, threshold:number, state:string}): void} [opts.onTick]
 *        Fires every frame with current energy — useful for UI meter.
 * @param {function(Error): void} [opts.onError]
 * @param {number} [opts.hangoverMs=400]
 *        Below-threshold duration before declaring speech ended.
 * @param {number} [opts.minSpeechMs=200]
 *        Minimum above-threshold duration to call a speech segment
 *        valid (filters single-syllable mic pops).
 *
 * Returns a controller with start() / stop() / getState().
 */
export function createEnergyVad(opts = {}) {
  if (!vadSupported) {
    return {
      supported: false,
      start() {
        if (opts.onError) {
          opts.onError(new Error("AudioContext / getUserMedia unavailable"));
        }
      },
      stop() {},
      getState: () => VAD_STATES.IDLE,
    };
  }

  const hangoverMs = Math.max(50, opts.hangoverMs || 400);
  const minSpeechMs = Math.max(50, opts.minSpeechMs || 200);

  let audioCtx = null;
  let stream = null;
  let analyser = null;
  let dataArray = null;
  let tickTimer = null;

  let state = VAD_STATES.IDLE;
  let calibrationStartTs = 0;
  let calibrationSamples = [];
  let noiseFloor = 0;
  let threshold = MIN_THRESHOLD;

  // Speech segment tracking.
  let aboveSinceTs = 0;
  let belowSinceTs = 0;
  let speechStartTs = 0;
  let peakDuringSpeech = 0;

  function setState(next) {
    state = next;
  }

  function computeRms() {
    if (!analyser || !dataArray) return 0;
    analyser.getByteTimeDomainData(dataArray);
    let sum = 0;
    for (let i = 0; i < dataArray.length; i++) {
      // dataArray values are 0..255 centered at 128; convert to -1..1
      const s = (dataArray[i] - 128) / 128;
      sum += s * s;
    }
    return Math.sqrt(sum / dataArray.length);
  }

  function tick() {
    if (!analyser) return;
    const now = performance.now();
    const level = computeRms();

    if (state === VAD_STATES.CALIBRATING) {
      calibrationSamples.push(level);
      if (now - calibrationStartTs >= CALIBRATE_MS) {
        // Take median of samples → noise floor; threshold = 3x but
        // never below MIN_THRESHOLD.
        const sorted = [...calibrationSamples].sort((a, b) => a - b);
        const mid = sorted[Math.floor(sorted.length / 2)] || 0;
        noiseFloor = mid;
        threshold = Math.max(MIN_THRESHOLD, mid * 3.0);
        setState(VAD_STATES.LISTENING);
        calibrationSamples = [];
      }
    } else if (state === VAD_STATES.LISTENING) {
      if (level > threshold) {
        if (aboveSinceTs === 0) aboveSinceTs = now;
        if (now - aboveSinceTs >= minSpeechMs) {
          // Crossed into speech.
          setState(VAD_STATES.SPEAKING);
          speechStartTs = aboveSinceTs;
          peakDuringSpeech = level;
          belowSinceTs = 0;
          aboveSinceTs = 0;
          if (opts.onSpeechStart) {
            try { opts.onSpeechStart(); } catch { /* swallow */ }
          }
        }
      } else {
        aboveSinceTs = 0;
      }
    } else if (state === VAD_STATES.SPEAKING) {
      if (level > peakDuringSpeech) peakDuringSpeech = level;
      if (level <= threshold) {
        if (belowSinceTs === 0) belowSinceTs = now;
        if (now - belowSinceTs >= hangoverMs) {
          // Speech ended.
          const duration = now - speechStartTs;
          setState(VAD_STATES.LISTENING);
          belowSinceTs = 0;
          aboveSinceTs = 0;
          const peak = peakDuringSpeech;
          peakDuringSpeech = 0;
          speechStartTs = 0;
          if (opts.onSpeechEnd) {
            try {
              opts.onSpeechEnd({ duration_ms: duration, peak });
            } catch { /* swallow */ }
          }
        }
      } else {
        belowSinceTs = 0;
      }
    }

    if (opts.onTick) {
      try { opts.onTick({ level, threshold, state }); } catch { /* swallow */ }
    }
  }

  async function start() {
    if (state !== VAD_STATES.IDLE) return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new Ctx();
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const source = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = FFT_SIZE;
      analyser.smoothingTimeConstant = 0.0; // we do our own smoothing
      source.connect(analyser);
      dataArray = new Uint8Array(analyser.fftSize);
      calibrationStartTs = performance.now();
      calibrationSamples = [];
      setState(VAD_STATES.CALIBRATING);
      tickTimer = setInterval(tick, FRAME_INTERVAL_MS);
    } catch (e) {
      if (opts.onError) opts.onError(e);
      cleanup();
    }
  }

  function cleanup() {
    if (tickTimer) {
      clearInterval(tickTimer);
      tickTimer = null;
    }
    if (stream) {
      for (const t of stream.getTracks()) {
        try { t.stop(); } catch { /* ignore */ }
      }
      stream = null;
    }
    if (audioCtx) {
      try { audioCtx.close(); } catch { /* ignore */ }
      audioCtx = null;
    }
    analyser = null;
    dataArray = null;
    setState(VAD_STATES.IDLE);
  }

  function stop() {
    cleanup();
  }

  return {
    supported: true,
    start,
    stop,
    getState: () => state,
    getThreshold: () => threshold,
    getNoiseFloor: () => noiseFloor,
  };
}
