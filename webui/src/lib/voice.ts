// 语音对话（混合方案）：听 = 浏览器原生 SpeechRecognition（实时、中文、
// 零后端）；说 = 后端 EdgeTTS（/api/v2/voice/tts，高质量中文音色）。
// 开启「语音对话」后形成免提循环：说话→实时转写→自动发送→回复完成→
// EdgeTTS 播报→播完自动再次聆听。手动麦克风按钮可随时插话。
import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../store/app";

function getSR(): any {
  if (typeof window === "undefined") return null;
  return (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition || null;
}

async function ttsPlay(text: string, token: string | null): Promise<HTMLAudioElement | null> {
  const clean = text.replace(/```[\s\S]*?```/g, "（代码块略）").slice(0, 4000).trim();
  if (!clean) return null;
  try {
    const r = await fetch("/api/v2/voice/tts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ text: clean }),
    });
    if (!r.ok) return null;
    const blob = await r.blob();
    const audio = new Audio(URL.createObjectURL(blob));
    void audio.play().catch(() => {});
    return audio;
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
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const lastSpokenRef = useRef<string | null>(null);
  const voiceOnRef = useRef(false);
  voiceOnRef.current = voiceOn;

  const supported = !!getSR();

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

  // 开启语音对话时：把当前最后一条 assistant 标记为「已读」，避免历史消息
  // 被朗读；并立即开始第一次聆听，进入免提对话。
  useEffect(() => {
    if (!voiceOn) {
      stopListening();
      audioRef.current?.pause?.();
      return;
    }
    const lastA = [...entries].reverse().find((e) => e.role === "assistant");
    lastSpokenRef.current = lastA?.id ?? null;
    startListening();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceOn]);

  // 回合完成 → 朗读最新 assistant 回复 → 播完自动再次聆听（免提循环）。
  useEffect(() => {
    if (!voiceOn || busy) return;
    const last = [...entries]
      .reverse()
      .find((e) => e.role === "assistant" && e.status === "complete" && !!e.content);
    if (!last || last.id === lastSpokenRef.current) return;
    lastSpokenRef.current = last.id;
    let cancelled = false;
    ttsPlay(last.content, token).then((audio) => {
      if (cancelled || !audio) {
        if (!cancelled && voiceOnRef.current) startListening();
        return;
      }
      audioRef.current = audio;
      audio.onended = () => {
        if (voiceOnRef.current) startListening();
      };
    });
    return () => {
      cancelled = true;
    };
  }, [entries, busy, voiceOn, token, startListening]);

  return { supported, voiceOn, setVoiceOn, listening, startListening, stopListening };
}
