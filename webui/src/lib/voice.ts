// 语音对话（混合方案）：听 = 浏览器原生 SpeechRecognition（实时、中文、
// 零后端）；说 = 后端 EdgeTTS（/api/v2/voice/tts，高质量中文音色）。
// 开启「语音对话」后形成免提循环：说话→实时转写→自动发送→回复完成→
// EdgeTTS 播报→播完自动再听。手动麦克风按钮可随时插话。
//
// 自动播放：TTS 在 agent 生成后才播，脱离用户手势会被浏览器静默拦截。
// 对策——常驻一个 <audio> 元素，在用户点「语音对话」的手势里先播一段
// 静音 WAV「解锁」它；之后所有 TTS 复用这个已解锁元素，play() 不再被拦。
import { useCallback, useEffect, useRef, useState } from "react";
import { authHeaders } from "./api";
import { useApp } from "../store/app";

// 1 帧静音 WAV（解锁自动播放用）。
const SILENT_WAV =
  "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=";

function getSR(): any {
  if (typeof window === "undefined") return null;
  return (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition || null;
}

async function ttsFetch(text: string, token: string | null): Promise<string | null> {
  const clean = text.replace(/```[\s\S]*?```/g, "（代码块略）").slice(0, 4000).trim();
  if (!clean) return null;
  const url = "/api/v2/voice/tts";
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: authHeaders(token, { "Content-Type": "application/json" }),
      body: JSON.stringify({ text: clean }),
    });
    if (!r.ok) return null;
    const blob = await r.blob();
    return URL.createObjectURL(blob);
  } catch {
    return null;
  }
}

export function useVoice() {
  const sendUser = useApp((s) => s.sendUser);
  const setDraft = useApp((s) => s.setDraft);
  const token = useApp((s) => s.token);
  const entries = useApp((s) => s.chat.entries);
  const busy = useApp((s) => !!s.chat.pendingAssistantId);

  const [voiceOn, setVoiceOn] = useState(false);
  const [listening, setListening] = useState(false);
  const recRef = useRef<any>(null);
  const audioElRef = useRef<HTMLAudioElement | null>(null);
  const lastSpokenRef = useRef<string | null>(null);
  const voiceOnRef = useRef(false);
  voiceOnRef.current = voiceOn;

  const supported = !!getSR();

  function getAudioEl(): HTMLAudioElement {
    if (!audioElRef.current) audioElRef.current = new Audio();
    return audioElRef.current;
  }

  const stopListening = useCallback(() => {
    try {
      recRef.current?.stop();
    } catch {
      /* noop */
    }
    setListening(false);
  }, []);

  const startListening = useCallback(() => {
    const Ctor = getSR();
    if (!Ctor) {
      useApp.getState().showToast("此浏览器不支持语音识别（建议 Chrome）", "err");
      return;
    }
    try {
      recRef.current?.abort?.();
    } catch {
      /* noop */
    }
    const rec = new Ctor();
    rec.lang = "zh-CN";
    rec.interimResults = true;
    rec.continuous = false;
    let finalText = "";
    rec.onresult = (e: any) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t;
        else interim += t;
      }
      setDraft(finalText + interim);
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => {
      setListening(false);
      const t = finalText.trim();
      if (t) {
        sendUser(t);
        setDraft("");
      }
    };
    recRef.current = rec;
    setListening(true);
    try {
      rec.start();
    } catch {
      setListening(false);
    }
  }, [sendUser, setDraft]);

  // 用户手势内调用：先解锁常驻 audio 元素，再开/关语音模式。
  const toggleVoice = useCallback(() => {
    const next = !voiceOnRef.current;
    if (next) {
      // 解锁自动播放（必须在 click 手势的调用栈里）。
      const el = getAudioEl();
      try {
        el.muted = true;
        el.src = SILENT_WAV;
        void el.play().then(() => el.pause()).catch(() => {});
      } catch {
        /* noop */
      }
    }
    setVoiceOn(next);
  }, []);

  // 开启语音对话：把当前最后一条 assistant 标记为「已读」，避免历史被朗读；
  // 立即开始第一次聆听。关闭：停掉一切。
  useEffect(() => {
    if (!voiceOn) {
      stopListening();
      try {
        audioElRef.current?.pause();
      } catch {
        /* noop */
      }
      return;
    }
    const lastA = [...entries].reverse().find((e) => e.role === "assistant");
    lastSpokenRef.current = lastA?.id ?? null;
    startListening();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceOn]);

  // 回合完成 → EdgeTTS 朗读最新 assistant 回复 → 播完自动再听（免提循环）。
  useEffect(() => {
    if (!voiceOn || busy) return;
    const last = [...entries]
      .reverse()
      .find((e) => e.role === "assistant" && e.status === "complete" && !!e.content);
    if (!last || last.id === lastSpokenRef.current) return;
    lastSpokenRef.current = last.id;
    let cancelled = false;
    ttsFetch(last.content, token).then((src) => {
      if (cancelled) return;
      if (!src) {
        if (voiceOnRef.current) startListening();
        return;
      }
      const el = getAudioEl();
      el.muted = false;
      el.src = src;
      el.onended = () => {
        if (voiceOnRef.current) startListening();
      };
      void el.play().catch(() => {
        // 仍被拦（极少数）→ 直接继续聆听，别卡死循环。
        if (voiceOnRef.current) startListening();
      });
    });
    return () => {
      cancelled = true;
    };
  }, [entries, busy, voiceOn, token, startListening]);

  return { supported, voiceOn, setVoiceOn, toggleVoice, listening, startListening, stopListening };
}
