// XMclaw — Memory page
//
// Three tabs:
//   1. 标识 — edit the 7 canonical persona files (SOUL/AGENTS/USER/MEMORY/
//      IDENTITY/TOOLS/BOOTSTRAP). Backed by GET/PUT /api/v2/profiles/active.
//      Saves rebuild app.state.agent's system prompt so edits land on the
//      next turn — no daemon restart needed.
//   2. 笔记 — the legacy memory notes browser (GET /api/v2/memory + per-file
//      GET/POST). Lets the user keep arbitrary topic notes alongside the
//      structured persona files. POST upserts on save, so notes are now
//      editable too (the prior version was read-only).
//   3. 日记 — daily journal (GET /api/v2/journal + per-date GET/PUT).
//      "Today" loads on tab open; the date list shows past entries with
//      previews. Empty save deletes the file.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { confirmDialog } from "../lib/dialog.js";
import { NotesTab, JournalTab } from "./Memory-NotesJournal.js";
import { IdentityTab } from "./Memory-Identity.js";

// ── shared ────────────────────────────────────────────────────────────

async function apiPut(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const res = await fetch(url, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

async function apiPost(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

// B-77: turn an apiGet failure into a human-readable diagnostic.
// apiGet throws Error("<status> <statusText>: <detail>") on 4xx/5xx and a
// TypeError("Failed to fetch") on network failure. The previous catch
// fallbacks all collapsed to a single "endpoint unavailable" string,
// which conflated four very different states (404 = stale daemon, 401 =
// token mismatch, 5xx = backend bug, network = daemon down) and made
// screenshots like "怎么回事" unactionable. This maps each shape to a
// distinguishable reason — same surface, real signal.
function _diagnoseFetch(err) {
  const msg = String((err && err.message) || err || "");
  if (/^401\b/.test(msg)) return "鉴权失败（pairing token 失效，刷新页面或检查 daemon --no-auth 设置）";
  if (/^403\b/.test(msg)) return "禁止访问（403）";
  if (/^404\b/.test(msg)) return "endpoint 不存在（daemon 可能未重启到最新版本）";
  if (/^5\d\d\b/.test(msg)) return "后端异常（" + msg + "）";
  if (/Failed to fetch|NetworkError/i.test(msg)) return "无法连接 daemon（进程可能已退出）";
  return msg || "未知错误";
}

function todayIso() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

const TAB_LABELS = [
  { id: "identity", label: "标识", hint: "SOUL / AGENTS / USER / MEMORY 等核心人格文件" },
  { id: "notes", label: "笔记", hint: "随手保存的主题笔记（~/.xmclaw/memory/*.md）" },
  { id: "journal", label: "日记", hint: "按日期归档的对话/事件记录" },
  { id: "providers", label: "Providers", hint: "已挂载的记忆 provider（B-26 Hermes-style 抽象）" },
];

// ── Providers tab (B-27/B-28/B-29) ───────────────────────────────────

function MemoryActivitySparkline({ token }) {
  // B-29: poll /api/v2/events?types=memory_op every 5s, plot a 60-second
  // sparkline of provider call rate so users see live memory activity
  // at a glance without dropping into the Trace page.
  const [points, setPoints] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      apiGet("/api/v2/events?limit=400&types=memory_op", token)
        .then((d) => {
          if (cancelled) return;
          const evs = d.events || [];
          const now = Date.now() / 1000;
          // 12 buckets × 5s = 60s window
          const buckets = new Array(12).fill(0);
          for (const e of evs) {
            const age = now - (e.ts || 0);
            if (age < 0 || age > 60) continue;
            const idx = 11 - Math.min(11, Math.floor(age / 5));
            if (idx >= 0) buckets[idx] += 1;
          }
          setPoints(buckets);
        })
        .catch(() => {});
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [token]);

  if (!points) return null;
  const max = Math.max(1, ...points);
  const W = 200, H = 30, PAD = 2;
  const stepX = (W - PAD * 2) / (points.length - 1);
  const path = points.map((v, i) => {
    const x = PAD + i * stepX;
    const y = H - PAD - (v / max) * (H - PAD * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const total = points.reduce((a, b) => a + b, 0);
  return html`
    <div class="xmc-h-card" style="padding:.6rem .8rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap">
      <small style="color:var(--xmc-fg-muted);font-family:var(--xmc-font-mono)">memory ops · last 60s</small>
      <svg viewBox="0 0 ${W} ${H}" width=${W} height=${H} style="display:block">
        <polyline fill="none" stroke="var(--xmc-accent, #6aa3f0)" stroke-width="1.5" points=${path} />
      </svg>
      <small style="font-family:var(--xmc-font-mono);font-size:.7rem">${total} calls · peak ${max}/5s</small>
    </div>
  `;
}

function ProvidersTab({ token }) {
  const [data, setData] = useState(null);
  const [available, setAvailable] = useState(null);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // B-49: indexer state — running? watched count? poll interval?
  const [indexer, setIndexer] = useState(null);
  // B-52: dream cron state.
  const [dream, setDream] = useState(null);
  const [dreamRunning, setDreamRunning] = useState(false);
  // B-53: backup list (lazy-loaded on toggle).
  const [showBackups, setShowBackups] = useState(false);
  const [backups, setBackups] = useState(null);
  // B-76: inline embedding-config form (only shown when indexer is unwired).
  const [showEmb, setShowEmb] = useState(false);
  const [embForm, setEmbForm] = useState({
    provider: "openai",
    base_url: "http://127.0.0.1:11434/v1",
    model: "qwen3-embedding:0.6b",
    dimensions: 1024,
    api_key: "",
  });
  const [embSaving, setEmbSaving] = useState(false);
  // B-96: LLM-pick top-K relevant-files state.
  const [picker, setPicker] = useState(null);
  const [pickerForm, setPickerForm] = useState({ enabled: false, k: 3, max_chars: 4000 });
  const [pickerSaving, setPickerSaving] = useState(false);

  const reload = useCallback(() => {
    apiGet("/api/v2/memory/providers", token)
      .then((d) => {
        setData(d);
        // Pre-select whichever external provider is currently active
        const ext = (d.providers || []).find((p) => p.kind === "external");
        if (ext) setSelected(ext.name);
        else if (d.wired) setSelected("none");
      })
      .catch((e) => setError(String(e.message || e)));
    apiGet("/api/v2/memory/providers/available", token)
      .then((d) => setAvailable(d.providers || []))
      .catch(() => setAvailable([]));
    apiGet("/api/v2/memory/indexer_status", token)
      .then(setIndexer)
      .catch((e) => setIndexer({ wired: false, reason: _diagnoseFetch(e) }));
    apiGet("/api/v2/memory/dream/status", token)
      .then(setDream)
      .catch((e) => setDream({ wired: false, reason: _diagnoseFetch(e) }));
    apiGet("/api/v2/memory/relevant_picker/status", token)
      .then((d) => {
        setPicker(d);
        // Sync form with current config so the toggle reflects reality.
        if (d.config) setPickerForm(d.config);
      })
      .catch(() => setPicker(null));
  }, [token]);

  useEffect(reload, [reload]);

  const loadBackups = useCallback(() => {
    apiGet("/api/v2/memory/dream/backups", token)
      .then((d) => setBackups(d.backups || []))
      .catch((e) => toast.error("加载备份失败：" + (e.message || e)));
  }, [token]);

  const onToggleBackups = () => {
    const next = !showBackups;
    setShowBackups(next);
    if (next && backups === null) loadBackups();
  };

  const onRestore = async (name) => {
    const ok = await confirmDialog({
      title: "还原 MEMORY.md",
      body: `从备份 "${name}" 还原。\n当前 MEMORY.md 会先自动备份（可再 restore 回来）。`,
      confirmLabel: "还原",
      confirmTone: "danger",
    });
    if (!ok) return;
    try {
      const r = await apiPost(`/api/v2/memory/dream/restore/${encodeURIComponent(name)}`, token, {});
      if (r.ok) {
        toast.success(`已还原（前一份 MEMORY.md 备份为 ${r.pre_restore_backup}）`);
        loadBackups();
        reload();
      }
    } catch (e) {
      toast.error("还原失败：" + (e.message || e));
    }
  };

  const onDreamNow = async () => {
    const ok = await confirmDialog({
      title: "立即压缩 MEMORY.md",
      body: "用 LLM 重写蒸馏，会先备份。30-60 秒视模型而定。",
      confirmLabel: "运行",
    });
    if (!ok) return;
    setDreamRunning(true);
    try {
      const res = await apiPost("/api/v2/memory/dream/run", token, {});
      if (res.ok) {
        toast.success(`压缩完成 — ${res.before_chars} → ${res.after_chars} 字符`);
        reload();
      } else {
        toast.error("压缩失败：" + (res.error || "未知"));
      }
    } catch (e) {
      toast.error("压缩失败：" + (e.message || e));
    } finally {
      setDreamRunning(false);
    }
  };

  // B-76: save the embedding section via POST /api/v2/memory/embedding/configure.
  // Same shape as onSwitch — daemon restart still required to actually
  // pick up the new embedder + start the indexer.
  const onSaveEmbedding = async () => {
    if (!embForm.model) {
      toast.error("model 不能为空");
      return;
    }
    if (!embForm.dimensions || embForm.dimensions <= 0) {
      toast.error("dimensions 必须 > 0（要和模型实际输出维度一致）");
      return;
    }
    setEmbSaving(true);
    try {
      const r = await apiPost(
        "/api/v2/memory/embedding/configure", token, embForm,
      );
      if (r.ok) {
        toast.success("已保存 — 重启 daemon 生效");
        setShowEmb(false);
      } else {
        toast.error("保存失败：" + (r.error || "未知"));
      }
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setEmbSaving(false);
    }
  };

  // B-96: save the LLM-pick top-K relevant-files config.
  const onSavePicker = async () => {
    setPickerSaving(true);
    try {
      const r = await apiPost(
        "/api/v2/memory/relevant_picker/configure", token, pickerForm,
      );
      if (r.ok) {
        toast.success("已保存 — 重启 daemon 生效");
        // Re-fetch to flip the restart_pending flag.
        apiGet("/api/v2/memory/relevant_picker/status", token)
          .then(setPicker)
          .catch(() => {});
      }
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setPickerSaving(false);
    }
  };

  const onSwitch = async (newProvider) => {
    if (!newProvider || newProvider === selected) return;
    setBusy(true);
    try {
      const r = await apiPost("/api/v2/memory/providers/switch", token, {
        provider: newProvider,
      });
      if (r.ok) {
        setSelected(newProvider);
        toast.success(
          `已切换到 ${newProvider} — ${r.restart_required ? '需重启 daemon 生效' : '已生效'}`,
        );
      }
    } catch (e) {
      toast.error("切换失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (error) return html`<p class="xmc-datapage__error">${error}</p>`;
  if (!data) return html`<p class="xmc-datapage__hint">加载中…</p>`;
  if (!data.wired) {
    return html`
      <div class="xmc-h-card" style="padding:1rem">
        <h3 style="margin:0 0 .5rem">⚠ 记忆系统还没准备好</h3>
        <p class="xmc-datapage__subtitle" style="line-height:1.6">
          可能原因：
        </p>
        <ul class="xmc-datapage__subtitle" style="margin:.4rem 0 .8rem 1.2rem;line-height:1.7">
          <li>daemon 还在启动 — 等几秒刷新</li>
          <li>没有配置 LLM API key — 顶部"首次设置进度"横幅会提示</li>
          <li><code>memory.enabled=false</code> 关掉了整个记忆 store — 在 Config → 记忆与向量库 改回 true</li>
        </ul>
        <p class="xmc-datapage__subtitle" style="font-size:.75rem">
          先去 <a href="/ui/doctor">Doctor 页面</a> 看哪一项 fail，按 advisory 修。
        </p>
      </div>
    `;
  }

  return html`
    <div>
      <${MemoryActivitySparkline} token=${token} />
      ${indexer
        ? html`
            <div class="xmc-h-card" style="padding:.6rem .8rem;margin:.6rem 0;background:var(--color-bg);border-left:3px solid var(--color-primary, #6aa3f0)">
              <strong style="font-size:.85rem">向量索引（B-41/B-43）</strong>
              ${indexer.wired
                ? html`
                    <div style="margin-top:.3rem;display:flex;gap:.6rem;flex-wrap:wrap;font-size:.8rem">
                      <span class="xmc-h-badge xmc-h-badge--${indexer.running ? 'success' : 'warn'}">
                        ${indexer.running ? '运行中' : '未运行'}
                      </span>
                      <span class="xmc-datapage__subtitle">监视文件: <strong>${indexer.watched_count}</strong></span>
                      <span class="xmc-datapage__subtitle">已索引: <strong>${indexer.known_count}</strong></span>
                      <span class="xmc-datapage__subtitle">轮询: <strong>${indexer.poll_interval_s}s</strong></span>
                    </div>
                  `
                : html`
                    <div style="margin-top:.3rem;color:var(--xmc-fg-muted);font-size:.78rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
                      <span>⚠ ${indexer.reason || '未启用'}</span>
                      <button
                        type="button"
                        class="xmc-h-btn xmc-h-btn--ghost"
                        style="font-size:.7rem;padding:.15rem .5rem"
                        onClick=${() => setShowEmb((v) => !v)}
                      >
                        ${showEmb ? '收起' : '配置 embedding'}
                      </button>
                    </div>
                    ${showEmb ? html`
                      <div style="margin-top:.6rem;display:grid;grid-template-columns:auto 1fr;gap:.4rem .6rem;align-items:center;font-size:.78rem">
                        <label>provider</label>
                        <select
                          value=${embForm.provider}
                          onChange=${(e) => setEmbForm({ ...embForm, provider: e.target.value })}
                          class="xmc-h-input"
                        >
                          <option value="openai">openai (covers Ollama / vLLM / DashScope)</option>
                        </select>
                        <label>base_url</label>
                        <input
                          type="text"
                          class="xmc-h-input"
                          value=${embForm.base_url}
                          placeholder="http://127.0.0.1:11434/v1"
                          onInput=${(e) => setEmbForm({ ...embForm, base_url: e.target.value })}
                        />
                        <label>model</label>
                        <input
                          type="text"
                          class="xmc-h-input"
                          value=${embForm.model}
                          placeholder="qwen3-embedding:0.6b"
                          onInput=${(e) => setEmbForm({ ...embForm, model: e.target.value })}
                        />
                        <label>dimensions</label>
                        <input
                          type="number"
                          class="xmc-h-input"
                          value=${embForm.dimensions}
                          min="1"
                          onInput=${(e) => setEmbForm({ ...embForm, dimensions: Number(e.target.value) || 0 })}
                        />
                        <label>api_key</label>
                        <input
                          type="password"
                          class="xmc-h-input"
                          value=${embForm.api_key}
                          placeholder="（Ollama 本地不需要）"
                          onInput=${(e) => setEmbForm({ ...embForm, api_key: e.target.value })}
                        />
                      </div>
                      <div style="margin-top:.6rem;display:flex;gap:.4rem;justify-content:flex-end">
                        <button
                          type="button"
                          class="xmc-h-btn xmc-h-btn--ghost"
                          style="font-size:.75rem"
                          onClick=${() => setShowEmb(false)}
                        >取消</button>
                        <button
                          type="button"
                          class="xmc-h-btn xmc-h-btn--primary"
                          style="font-size:.75rem"
                          disabled=${embSaving}
                          onClick=${onSaveEmbedding}
                        >${embSaving ? '保存中…' : '保存（需重启 daemon）'}</button>
                      </div>
                      <div style="margin-top:.4rem;font-size:.7rem;color:var(--xmc-fg-muted)">
                        提示：dimensions 必须和模型实际输出维度一致——qwen3-embedding:0.6b = 1024，text-embedding-3-small = 1536。
                      </div>
                    ` : null}
                  `}
            </div>
          `
        : null}
      ${dream
        ? html`
            <div class="xmc-h-card" style="padding:.6rem .8rem;margin:.6rem 0;background:var(--color-bg);border-left:3px solid var(--color-primary, #6aa3f0)">
              <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
                <strong style="font-size:.85rem">Auto-Dream 压缩（B-51）</strong>
                ${dream.wired
                  ? html`<button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onDreamNow} disabled=${dreamRunning} style="font-size:.7rem">
                      ${dreamRunning ? '运行中…' : '立刻运行'}
                    </button>`
                  : null}
              </div>
              ${dream.wired
                ? html`
                    <div style="margin-top:.3rem;display:flex;gap:.6rem;flex-wrap:wrap;font-size:.8rem">
                      <span class="xmc-h-badge xmc-h-badge--${dream.running ? 'success' : 'warn'}">
                        ${dream.running ? '运行中' : '未运行'}
                      </span>
                      <span class="xmc-datapage__subtitle">每日 <strong>${String(dream.hour).padStart(2, '0')}:${String(dream.minute).padStart(2, '0')}</strong></span>
                      ${dream.last_run_at
                        ? html`<span class="xmc-datapage__subtitle">最近一次: <strong>${new Date(dream.last_run_at * 1000).toLocaleString('zh-CN')}</strong></span>`
                        : html`<span class="xmc-datapage__subtitle">尚未运行过</span>`}
                      ${dream.last_result && dream.last_result.ok
                        ? html`<span class="xmc-h-badge xmc-h-badge--success">节省 ${dream.last_result.saved_chars}</span>`
                        : null}
                      ${dream.last_result && !dream.last_result.ok
                        ? html`<span class="xmc-h-badge xmc-h-badge--error" title=${dream.last_result.error || ''}>上次失败</span>`
                        : null}
                      <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onToggleBackups} style="font-size:.7rem;margin-left:auto">
                        ${showBackups ? '隐藏' : '显示'}备份 ${backups != null ? `(${backups.length})` : ''}
                      </button>
                    </div>
                    ${showBackups
                      ? html`
                          <div style="margin-top:.4rem;padding:.4rem .6rem;background:var(--color-card);border:1px solid var(--color-border);border-radius:4px;max-height:240px;overflow-y:auto">
                            ${backups == null
                              ? html`<small class="xmc-datapage__subtitle">加载中…</small>`
                              : backups.length === 0
                                ? html`<small class="xmc-datapage__subtitle">尚无备份</small>`
                                : html`
                                    <ul class="xmc-datapage__list" style="margin:0">
                                      ${backups.map((b) => html`
                                        <li class="xmc-datapage__row" key=${b.name} style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;padding:.25rem 0;font-size:.75rem">
                                          <span style="display:flex;flex-direction:column;gap:.1rem;min-width:0;flex:1 1 auto">
                                            <code style="font-size:.7rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${b.name}</code>
                                            <small class="xmc-datapage__subtitle">${b.size}B · ${new Date(b.mtime * 1000).toLocaleString('zh-CN')}</small>
                                          </span>
                                          <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => onRestore(b.name)} style="font-size:.7rem;flex:0 0 auto">还原</button>
                                        </li>
                                      `)}
                                    </ul>
                                  `}
                          </div>
                        `
                      : null}
                  `
                : html`
                    <div style="margin-top:.3rem;color:var(--xmc-fg-muted);font-size:.78rem">
                      ⚠ ${dream.reason || '未启用'}（需配置 LLM）
                    </div>
                  `}
            </div>
          `
        : null}
      ${picker
        ? html`
            <div class="xmc-h-card" style="padding:.6rem .8rem;margin:.6rem 0;background:var(--color-bg);border-left:3px solid var(--color-primary, #6aa3f0)">
              <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
                <strong style="font-size:.85rem">LLM 多文件记忆召回（B-93）</strong>
                <span class="xmc-h-badge xmc-h-badge--${picker.runtime.enabled ? 'success' : 'muted'}" style="font-size:.7rem">
                  ${picker.runtime.enabled ? '运行中' : '关闭'}
                </span>
              </div>
              <div style="margin-top:.3rem;color:var(--xmc-fg-muted);font-size:.78rem;line-height:1.6">
                每次回合开始让一个子 LLM 从 <code>~/.xmclaw/memory/*.md</code>
                里挑出最相关的 top-K 笔记整篇注入。补 <code>memory_search</code>
                的"段落级向量召回"以"概念级文件召回"。<strong>每回合多一次 LLM 调用</strong>，
                所以默认关闭。
              </div>
              ${picker.restart_pending ? html`
                <div style="margin-top:.4rem;color:var(--color-warning, #c8a86a);font-size:.75rem">
                  🔄 配置已修改但未重启 daemon — 当前运行中的设置：enabled=${picker.runtime.enabled}, k=${picker.runtime.k}, max_chars=${picker.runtime.max_chars}
                </div>
              ` : null}
              <div style="margin-top:.6rem;display:grid;grid-template-columns:auto 1fr;gap:.4rem .6rem;align-items:center;font-size:.78rem">
                <label>开启</label>
                <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer">
                  <input
                    type="checkbox"
                    checked=${pickerForm.enabled}
                    onChange=${(e) => setPickerForm({ ...pickerForm, enabled: e.target.checked })}
                  />
                  <span style="color:var(--xmc-fg-muted);font-size:.74rem">勾上后每次对话会多一次 LLM 调用挑相关笔记</span>
                </label>
                <label>top-K</label>
                <input
                  type="number"
                  class="xmc-h-input"
                  min="1" max="20"
                  value=${pickerForm.k}
                  onInput=${(e) => setPickerForm({ ...pickerForm, k: Number(e.target.value) || 3 })}
                />
                <label>max_chars</label>
                <input
                  type="number"
                  class="xmc-h-input"
                  min="500" max="50000" step="500"
                  value=${pickerForm.max_chars}
                  onInput=${(e) => setPickerForm({ ...pickerForm, max_chars: Number(e.target.value) || 4000 })}
                />
              </div>
              <div style="margin-top:.5rem;display:flex;gap:.4rem;justify-content:flex-end">
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--primary"
                  style="font-size:.75rem"
                  disabled=${pickerSaving}
                  onClick=${onSavePicker}
                >${pickerSaving ? '保存中…' : '保存（需重启 daemon）'}</button>
              </div>
            </div>
          `
        : null}
      <p class="xmc-datapage__subtitle" style="margin:.6rem 0 1rem">
        XMclaw 的内存层是 Hermes-style 可插拔架构（B-25/B-26 完成）：
        <strong>1 个内置 provider + 至多 1 个外部 provider</strong>。
        外部 provider 优先（active recall），内置 provider 永远在底（fallback）。
      </p>
      <ul class="xmc-datapage__list">
        ${(data.providers || []).map((p) => html`
          <li class="xmc-datapage__row" key=${p.name}>
            <div style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap">
              <strong style="font-size:1rem">${p.name}</strong>
              <span class="xmc-h-badge xmc-h-badge--${p.kind === 'builtin' ? 'success' : 'info'}" style="font-size:.7rem">
                ${p.kind === 'builtin' ? '内置 (永久)' : '外部 (可换)'}
              </span>
            </div>
            <div style="margin-top:.25rem;color:var(--xmc-fg-muted);font-size:.78rem">
              ${p.tool_count > 0
                ? html`暴露 ${p.tool_count} 个 LLM 工具: ${(p.tools || []).slice(0, 3).map((t) => html`<code key=${t} style="margin-right:.3rem">${t}</code>`)}`
                : html`<small>不暴露 LLM 工具</small>`}
            </div>
          </li>
        `)}
      </ul>
      <h3 style="margin:1.2rem 0 .5rem">切换外部 provider</h3>
      ${available && available.length > 0 ? html`
        <div class="xmc-datapage__row" style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
          <select
            value=${selected}
            onChange=${(e) => onSwitch(e.target.value)}
            disabled=${busy}
            style="padding:.4rem .5rem;font-size:.9rem;min-width:220px"
          >
            ${available.map((p) => html`
              <option value=${p.id} key=${p.id}>${p.label}</option>
            `)}
          </select>
          <small class="xmc-datapage__subtitle">切换需重启 daemon 生效</small>
        </div>
        ${(() => {
          const cur = (available || []).find((p) => p.id === selected);
          if (!cur) return null;
          return html`
            <div class="xmc-h-card" style="padding:.5rem .8rem;margin-top:.5rem;background:var(--color-bg)">
              <small style="color:var(--xmc-fg-muted)">${cur.description}</small>
              ${(cur.needs || []).length > 0 ? html`
                <div style="margin-top:.3rem">
                  <small style="color:var(--xmc-fg-muted)">需要配置：</small>
                  ${cur.needs.map((n) => html`<code key=${n} style="margin-right:.4rem;font-size:.7rem">${n}</code>`)}
                </div>
              ` : null}
            </div>
          `;
        })()}
      ` : null}

      <h3 style="margin:1.2rem 0 .5rem">如何写一个新 provider</h3>
      <p class="xmc-datapage__subtitle">
        实现 <code>xmclaw/providers/memory/base.MemoryProvider</code> ABC（put / query / forget +
        可选的 prefetch / sync_turn / on_session_end / on_pre_compress / get_tool_schemas /
        handle_tool_call），放到 <code>xmclaw/providers/memory/&lt;name&gt;.py</code>，
        在 <code>factory.py</code> 注册即可 — agent_loop 不需修改。
        参考实现 <code>builtin_file.py</code>（内置）/ <code>sqlite_vec.py</code>（外部）/
        <code>hindsight.py</code>（云 KG 模板）。
      </p>
    </div>
  `;
}

// IdentityTab moved to ./Memory-Identity.js (B-52)



// NotesTab + JournalTab live in ./Memory-NotesJournal.js (split out
// in B-49 to keep this file under the 500-line UI scaffold budget).

// ── shell ─────────────────────────────────────────────────────────────

export function MemoryPage({ token }) {
  const [tab, setTab] = useState("identity");
  const activeMeta = useMemo(() => TAB_LABELS.find((t) => t.id === tab), [tab]);

  return html`
    <section class="xmc-datapage" aria-labelledby="memory-title">
      <header class="xmc-datapage__header">
        <h2 id="memory-title">记忆</h2>
        <p class="xmc-datapage__subtitle">${activeMeta ? activeMeta.hint : ""}</p>
      </header>
      <nav class="xmc-mem-tabs" role="tablist" aria-label="记忆类别" style="display:flex;gap:.4rem;border-bottom:1px solid var(--color-border);margin-bottom:.8rem;flex-wrap:wrap">
        ${TAB_LABELS.map((t) => {
          const isActive = t.id === tab;
          return html`
            <button
              type="button"
              role="tab"
              aria-selected=${isActive}
              onClick=${() => setTab(t.id)}
              key=${t.id}
              style=${`appearance:none;background:none;border:none;padding:.5rem .9rem;font:inherit;cursor:pointer;color:${isActive ? "var(--color-primary)" : "var(--xmc-fg-muted)"};border-bottom:2px solid ${isActive ? "var(--color-primary)" : "transparent"};font-weight:${isActive ? "600" : "500"}`}
            >
              ${t.label}
            </button>
          `;
        })}
      </nav>
      ${tab === "identity" ? html`<${IdentityTab} token=${token} />` : null}
      ${tab === "notes" ? html`<${NotesTab} token=${token} />` : null}
      ${tab === "journal" ? html`<${JournalTab} token=${token} />` : null}
      ${tab === "providers" ? html`<${ProvidersTab} token=${token} />` : null}
    </section>
  `;
}
