// XMclaw — Composer (Nebula redesign)
// Replaced with glassmorphism input, auto-resize textarea, and drag-drop zone.
// Props interface preserved: onSend, onCancel, draft (value/onChange), images, etc.

const { h } = window.__xmc.preact;
const { useState, useRef, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Button } from "../atoms/button.js";
import { Badge } from "../atoms/badge.js";
import { usePopoverApi } from "./SlashPopover.js";
import { createRecognizer, sttSupported } from "../../lib/audio.js";
import {
  createVoiceLoop,
  voiceLoopSupported,
  PHASES as VOICE_PHASES,
} from "../../lib/voice_loop.js";
import { toast } from "../../lib/toast.js";

const _HISTORY_KEY = "xmc-prompt-history-v1";
const _HISTORY_MAX = 50;

function _readHistory() {
  try {
    const raw = localStorage.getItem(_HISTORY_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((s) => typeof s === "string") : [];
  } catch (_) {
    return [];
  }
}

function _writeHistory(list) {
  try {
    localStorage.setItem(_HISTORY_KEY, JSON.stringify(list.slice(-_HISTORY_MAX)));
  } catch (_) {
    /* private mode / quota — fail silent */
  }
}

export function appendPromptHistory(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  const cur = _readHistory();
  if (cur.length > 0 && cur[cur.length - 1] === trimmed) return;
  cur.push(trimmed);
  _writeHistory(cur);
}

export function Composer({
  value,
  onChange,
  onSend,
  onCancel,
  planMode,
  onTogglePlan,
  outputStyle,
  onCycleOutputStyle,
  ultrathink,
  onToggleUltrathink,
  canSend,
  busy,
  slashStore,
  token,
  images,
  onAddImages,
  onRemoveImage,
  lastAssistantText,
}) {
  const fileInputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);
  const dragCounterRef = useRef(0);

  // ── Global drag & drop for dropzone overlay ──
  useEffect(() => {
    function onDragEnter(e) {
      e.preventDefault();
      dragCounterRef.current += 1;
      if (e.dataTransfer?.types?.includes("Files")) {
        setIsDragging(true);
      }
    }
    function onDragLeave(e) {
      e.preventDefault();
      dragCounterRef.current -= 1;
      if (dragCounterRef.current <= 0) {
        dragCounterRef.current = 0;
        setIsDragging(false);
      }
    }
    function onDragOver(e) {
      e.preventDefault();
    }
    function onDrop(e) {
      e.preventDefault();
      dragCounterRef.current = 0;
      setIsDragging(false);
      handleFiles(e.dataTransfer?.files);
    }

    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
    };
  }, []);

  function handleFiles(fileList) {
    if (!fileList || fileList.length === 0) return;
    const files = Array.from(fileList).filter((f) =>
      f.type.startsWith("image/")
      || f.type.startsWith("video/")
      || f.type.startsWith("audio/")
    );
    if (files.length === 0) {
      toast.error("仅支持图片 / 音频 / 视频文件");
      return;
    }
    const SIZE_CAP = 8 * 1024 * 1024;
    const tooBig = files.filter((f) => f.size > SIZE_CAP);
    if (tooBig.length > 0) {
      toast.error(`文件 ${tooBig[0].name} 超过 8 MB，请先压缩`);
      return;
    }
    Promise.all(
      files.map(
        (f) => new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () =>
            resolve({
              name: f.name,
              type: f.type,
              size: f.size,
              dataUrl: reader.result,
            });
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(f);
        }),
      ),
    ).then((entries) => {
      if (onAddImages) onAddImages(entries);
    }).catch((err) => {
      toast.error("读取文件失败：" + (err?.message || err));
    });
  }

  function pickFiles() {
    if (fileInputRef.current) fileInputRef.current.click();
  }

  function handlePaste(evt) {
    const items = evt.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const it of items) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length > 0) {
      evt.preventDefault();
      handleFiles(files);
    }
  }

  // Dropzone overlay handlers
  function handleDropzoneDragOver(e) {
    e.preventDefault();
    e.currentTarget.classList.add("dragover");
  }
  function handleDropzoneDragLeave(e) {
    e.currentTarget.classList.remove("dragover");
  }
  function handleDropzoneDrop(e) {
    e.preventDefault();
    e.currentTarget.classList.remove("dragover");
    dragCounterRef.current = 0;
    setIsDragging(false);
    handleFiles(e.dataTransfer?.files);
  }

  const slash = usePopoverApi({
    input: value,
    onApply: (next) => onChange(next),
    store: slashStore || {},
    token,
  });

  // ── Mic / STT ──
  const [listening, setListening] = useState(false);
  const recRef = useRef(null);
  const baseTextRef = useRef("");
  const historyIdxRef = useRef(null);
  const draftBeforeHistoryRef = useRef("");

  useEffect(() => () => {
    if (recRef.current) recRef.current.stop();
  }, []);

  // ── Continuous voice loop ──
  const [voiceActive, setVoiceActive] = useState(false);
  const [voicePhase, setVoicePhase] = useState(VOICE_PHASES.IDLE);
  const voiceRef = useRef(null);
  const wasBusyRef = useRef(false);
  const pendingValueRef = useRef(null);

  function startVoiceLoop() {
    if (!voiceLoopSupported) {
      toast.error("当前浏览器不支持连续语音（建议 Chrome 或 Edge）");
      return;
    }
    if (voiceRef.current) {
      voiceRef.current.stop();
      voiceRef.current = null;
    }
    const loop = createVoiceLoop({
      onUtterance: (text) => {
        pendingValueRef.current = text;
        onChange(text);
      },
      onPhaseChange: (p) => setVoicePhase(p),
      onError: (err) => {
        const msg = err?.message || String(err) || "voice loop error";
        if (msg !== "no-speech" && msg !== "aborted") {
          toast.error("连续语音：" + msg);
        }
      },
    });
    voiceRef.current = loop;
    loop.start();
    setVoiceActive(true);
  }

  function stopVoiceLoop() {
    if (voiceRef.current) {
      voiceRef.current.stop();
      voiceRef.current = null;
    }
    setVoiceActive(false);
    setVoicePhase(VOICE_PHASES.IDLE);
    pendingValueRef.current = null;
  }

  useEffect(() => {
    if (!voiceActive) return;
    if (pendingValueRef.current == null) return;
    if (value !== pendingValueRef.current) return;
    if (!canSend) return;
    pendingValueRef.current = null;
    onSend();
  }, [voiceActive, value, canSend, onSend]);

  useEffect(() => {
    if (!voiceActive || !voiceRef.current) return;
    const wasBusy = wasBusyRef.current;
    wasBusyRef.current = busy;
    if (wasBusy && !busy) {
      voiceRef.current.deliverReply(lastAssistantText || "");
    }
  }, [busy, voiceActive, lastAssistantText]);

  useEffect(() => () => {
    if (voiceRef.current) voiceRef.current.stop();
  }, []);

  const startListening = () => {
    if (!sttSupported) {
      toast.error("当前浏览器不支持语音输入（建议 Chrome 或 Edge）");
      return;
    }
    if (recRef.current?.isActive?.()) {
      recRef.current.stop();
      return;
    }
    baseTextRef.current = value || "";
    const rec = createRecognizer({
      onPartial: (interim) => {
        const sep = baseTextRef.current && !baseTextRef.current.endsWith(" ") ? " " : "";
        onChange(baseTextRef.current + sep + interim);
      },
      onFinal: (final) => {
        const sep = baseTextRef.current && !baseTextRef.current.endsWith(" ") ? " " : "";
        baseTextRef.current = baseTextRef.current + sep + final;
        onChange(baseTextRef.current);
      },
      onError: (err) => {
        setListening(false);
        const msg = err?.message || String(err) || "语音识别失败";
        if (msg !== "no-speech" && msg !== "aborted") {
          toast.error("语音识别：" + msg);
        }
      },
      onEnd: () => setListening(false),
    });
    recRef.current = rec;
    rec.start();
    setListening(true);
  };

  function handleKeyDown(evt) {
    if (slash.handleKey(evt)) return;
    if (evt.key === "Enter" && !evt.shiftKey && !evt.isComposing) {
      evt.preventDefault();
      if (canSend) {
        historyIdxRef.current = null;
        onSend();
      }
      return;
    }
    if (evt.key === "Enter" && (evt.ctrlKey || evt.metaKey)) {
      evt.preventDefault();
      if (canSend) {
        historyIdxRef.current = null;
        onSend();
      }
      return;
    }
    if (evt.key === "Escape") {
      historyIdxRef.current = null;
      evt.target.blur();
      return;
    }
    if (evt.key === "ArrowUp" && !evt.shiftKey && !evt.altKey) {
      const ta = evt.target;
      const v = ta.value || "";
      const caretAtStart = ta.selectionStart === 0 || !v.slice(0, ta.selectionStart).includes("\n");
      if (!caretAtStart) return;
      const hist = _readHistory();
      if (hist.length === 0) return;
      if (historyIdxRef.current === null) {
        draftBeforeHistoryRef.current = v;
        historyIdxRef.current = hist.length - 1;
      } else if (historyIdxRef.current > 0) {
        historyIdxRef.current -= 1;
      } else {
        return;
      }
      evt.preventDefault();
      onChange(hist[historyIdxRef.current]);
      return;
    }
    if (evt.key === "ArrowDown" && !evt.shiftKey && !evt.altKey) {
      if (historyIdxRef.current === null) return;
      const ta = evt.target;
      const v = ta.value || "";
      const caretAtEnd =
        ta.selectionStart === v.length || !v.slice(ta.selectionStart).includes("\n");
      if (!caretAtEnd) return;
      const hist = _readHistory();
      if (historyIdxRef.current >= hist.length - 1) {
        historyIdxRef.current = null;
        evt.preventDefault();
        onChange(draftBeforeHistoryRef.current);
        return;
      }
      historyIdxRef.current += 1;
      evt.preventDefault();
      onChange(hist[historyIdxRef.current]);
    }
  }

  function handleInput(evt) {
    onChange(evt.target.value);
    const ta = evt.target;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  }

  const stagedImages = Array.isArray(images) ? images : [];

  return html`
    <div class="nb-composer-wrapper" onPaste=${handlePaste}>
      ${isDragging
        ? html`
            <div
              class="nb-dropzone"
              onDragOver=${handleDropzoneDragOver}
              onDragLeave=${handleDropzoneDragLeave}
              onDrop=${handleDropzoneDrop}
            >
              <div class="nb-dropzone__icon">📎</div>
              <div class="nb-dropzone__text">拖放文件到此处上传</div>
              <div class="nb-dropzone__hint">图片、音频、视频（最大 8MB）</div>
            </div>
          `
        : null}

      ${stagedImages.length > 0
        ? html`
            <div class="nb-composer__attachments">
              ${stagedImages.map((img, idx) => html`
                <div class="nb-composer__attachment" key=${idx}>
                  ${img.type && img.type.startsWith("video/")
                    ? html`<video src=${img.dataUrl} muted />`
                    : img.type && img.type.startsWith("audio/")
                    ? html`<div class="nb-composer__attachment-audio">🎵 ${img.name || "audio"}</div>`
                    : html`<img src=${img.dataUrl} alt=${img.name || ""} />`}
                  <button
                    type="button"
                    class="nb-composer__attachment-remove"
                    onClick=${() => onRemoveImage && onRemoveImage(idx)}
                    title="移除"
                    aria-label="移除附件"
                  >×</button>
                </div>
              `)}
            </div>
          `
        : null}

      <input
        ref=${fileInputRef}
        type="file"
        accept="image/*,video/*,audio/*"
        multiple
        style="display:none"
        onChange=${(e) => {
          handleFiles(e.target.files);
          e.target.value = "";
        }}
      />

      <div class="nb-composer-area">
        <div class="nb-composer" data-busy=${busy ? "1" : "0"}>
          <button
            type="button"
            class="nb-composer__btn"
            onClick=${pickFiles}
            aria-label="附加图片 / 音频 / 视频"
            title="附加图片、音频或视频（也可直接粘贴或拖拽）"
          >+</button>

          ${slash.render()}
          <textarea
            rows="1"
            placeholder=${planMode
              ? "Plan 模式 — 让助手先规划再执行。Enter 发送，Shift+Enter 换行。"
              : "对 XMclaw 说…   ( / 命令 · @ 技能 )"}
            value=${value}
            onInput=${handleInput}
            onKeyDown=${handleKeyDown}
            aria-label="message composer"
          ></textarea>

          <button
            type="button"
            class=${"nb-composer__btn" + (listening ? " is-on" : "") + (sttSupported ? "" : " is-disabled")}
            onClick=${startListening}
            disabled=${!sttSupported}
            aria-pressed=${listening ? "true" : "false"}
            aria-label=${listening ? "停止听写" : "开始语音输入"}
            title=${sttSupported
              ? (listening ? "停止听写（再次点击）" : "语音输入 — 点击开始说话")
              : "当前浏览器不支持语音输入"}
          >
            ${listening ? "🔴" : "🎙"}
          </button>

          ${busy && onCancel
            ? html`<button
                type="button"
                class="nb-composer__send nb-composer__send--stop"
                onClick=${() => onCancel()}
                aria-label="stop"
                title="停止当前回答（在 hop 边界生效）"
              >
                ⏹
              </button>`
            : html`<button
                type="button"
                class="nb-composer__send"
                disabled=${!canSend}
                onClick=${() => canSend && onSend()}
                aria-label="send"
                title="发送"
              >
                ➤
              </button>`}
        </div>

        <div class="nb-composer__toolbar">
          <button
            type="button"
            class=${"nb-composer__chip" + (planMode ? " is-on" : "")}
            aria-pressed=${planMode ? "true" : "false"}
            onClick=${onTogglePlan}
            title="Plan 模式：助手先列计划再执行"
          >
            ${planMode ? "Plan" : "Act"}
          </button>
          <button
            type="button"
            class=${"nb-composer__chip" + ((outputStyle && outputStyle !== "default") ? " is-on" : "")}
            aria-pressed=${(outputStyle && outputStyle !== "default") ? "true" : "false"}
            onClick=${onCycleOutputStyle}
            title="输出风格：default → Explanatory（边写边解释）→ Learning（让你动手填 TODO）"
          >
            ${outputStyle === "Explanatory"
              ? "Explain"
              : outputStyle === "Learning"
                ? "Learn"
                : "Style"}
          </button>
          <button
            type="button"
            class=${"nb-composer__chip" + (ultrathink ? " is-on" : "")}
            aria-pressed=${ultrathink ? "true" : "false"}
            onClick=${onToggleUltrathink}
            title="Ultrathink：触发更深的推理（消耗更多 token）"
          >
            ★ Ultrathink
          </button>
          <button
            type="button"
            class=${"nb-composer__chip" + (voiceActive ? " is-on" : "") + (voiceLoopSupported ? "" : " is-disabled")}
            aria-pressed=${voiceActive ? "true" : "false"}
            onClick=${voiceActive ? stopVoiceLoop : startVoiceLoop}
            disabled=${!voiceLoopSupported}
            title=${voiceLoopSupported
              ? (voiceActive
                ? `连续语音中（${voicePhase === VOICE_PHASES.LISTENING ? "听你说" : voicePhase === VOICE_PHASES.SUBMITTING ? "提交中" : voicePhase === VOICE_PHASES.SPEAKING ? "在说话" : "待机"}）— 点击退出`
                : "连续对话模式：解放双手，说完它就回，回完接着听")
              : "当前浏览器不支持连续语音"}
          >
            ${voiceActive
              ? (voicePhase === VOICE_PHASES.LISTENING ? "🎧 听"
                : voicePhase === VOICE_PHASES.SUBMITTING ? "✉ 发"
                : voicePhase === VOICE_PHASES.SPEAKING ? "🔊 说"
                : "🔁 对话")
              : "🔁 对话"}
          </button>
          ${busy
            ? html`<${Badge} tone="info">streaming…</${Badge}>`
            : null}
        </div>

        <div class="nb-composer-hint">
          <span>Enter 发送</span> · <span>Shift+Enter 换行</span> · <span>⌘K 命令面板</span>
        </div>
      </div>
    </div>
  `;
}
