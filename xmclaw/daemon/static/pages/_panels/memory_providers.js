// XMclaw — Memory page Providers tab (B-323 split).
//
// Lifted out of pages/Memory.js to keep that page under the 500-line
// UI budget (FRONTEND_DESIGN.md §1.4). This panel itself is still
// over budget — see the KNOWN_OVERSIZED list in
// tests/unit/test_v2_ui_scaffold.py — and is on the queue for
// further sub-component extraction (vector-indexer / Auto-Dream /
// pinned / backups / picker / provider-switcher are the obvious
// boundaries).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

// 2026-05-26: consolidated onto lib/api.js. See memory_identity.js
// header note for why. Arg order is (path, body, token).
import { apiGet, apiPost } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";
import { confirmDialog } from "../../lib/dialog.js";
// B-323 follow-up: ProvidersTab's 5 sub-cards live in their own files
// so this panel finally clears the 500-line UI budget. Each card is
// pure presentation; this parent owns state + async handlers.
import { VectorIndexerCard } from "./memory_providers_indexer.js";
import { AutoDreamCard } from "./memory_providers_dream.js";
import { PinnedCard } from "./memory_providers_pinned.js";
import { PickerCard } from "./memory_providers_picker.js";
import {
  ProviderListSection,
  ProviderSwitcher,
  WriteProviderHelp,
} from "./memory_providers_switcher.js";


// Same shape as the helper in Memory.js — diagnose apiGet failure
// codes into actionable Chinese messages.
function _diagnoseFetch(err) {
  const msg = String((err && err.message) || err || "");
  if (/^401\b/.test(msg)) return "鉴权失败（pairing token 失效，刷新页面或检查 daemon --no-auth 设置）";
  if (/^403\b/.test(msg)) return "禁止访问（403）";
  if (/^404\b/.test(msg)) return "endpoint 不存在（daemon 可能未重启到最新版本）";
  if (/^5\d\d\b/.test(msg)) return "后端异常（" + msg + "）";
  if (/Failed to fetch|NetworkError/i.test(msg)) return "无法连接 daemon（进程可能已退出）";
  return msg || "未知错误";
}


// B-344 (audit pass-2 follow-up): the B-323 monolith split
// (commit 3cc12dd) extracted ``MemoryActivitySparkline`` from
// ``pages/Memory.js`` into this file but DROPPED the function
// header. The body sat at module top-level using ``useState`` /
// ``useEffect`` / ``return`` outside any function — JavaScript
// rejected it with ``Uncaught SyntaxError: Illegal return
// statement`` at parse time, killing the entire panel module
// import chain and blanking the UI. The same split also dropped
// the ``export`` so even if the body parsed, no caller could
// import it. Restored both header + ``export`` here.
export function MemoryActivitySparkline({ token }) {
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

export function ProvidersTab({ token }) {
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
  // B-98: pinned bullets state + add-bullet draft.
  const [pinned, setPinned] = useState(null);
  const [pinDraft, setPinDraft] = useState("");
  const [pinBusy, setPinBusy] = useState(false);

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
    apiGet("/api/v2/memory/pinned", token)
      .then((d) => setPinned(Array.isArray(d.items) ? d.items : []))
      .catch(() => setPinned([]));
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
      const r = await apiPost(
        `/api/v2/memory/dream/restore/${encodeURIComponent(name)}`,
        {}, token,
      );
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
      const res = await apiPost("/api/v2/memory/dream/run", {}, token);
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
        "/api/v2/memory/embedding/configure", embForm, token,
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

  // B-98: pin / unpin handlers.
  const onAddPin = async () => {
    const content = pinDraft.trim();
    if (!content) return;
    setPinBusy(true);
    try {
      await apiPost("/api/v2/memory/pinned", { content }, token);
      setPinDraft("");
      apiGet("/api/v2/memory/pinned", token)
        .then((d) => setPinned(Array.isArray(d.items) ? d.items : []))
        .catch(() => {});
      toast.success("已 pin");
    } catch (e) {
      toast.error("pin 失败：" + (e.message || e));
    } finally {
      setPinBusy(false);
    }
  };

  const onRemovePin = async (line) => {
    const ok = await confirmDialog({
      title: "取消 pin",
      body: `从 ## Pinned 删除：\n\n${line}`,
      confirmLabel: "删除",
      confirmTone: "danger",
    });
    if (!ok) return;
    try {
      const url = "/api/v2/memory/pinned" +
        (token ? `?token=${encodeURIComponent(token)}` : "");
      const res = await fetch(url, {
        method: "DELETE",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ line }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      apiGet("/api/v2/memory/pinned", token)
        .then((d) => setPinned(Array.isArray(d.items) ? d.items : []))
        .catch(() => {});
      toast.success("已删除");
    } catch (e) {
      toast.error("删除失败：" + (e.message || e));
    }
  };

  // B-96: save the LLM-pick top-K relevant-files config.
  const onSavePicker = async () => {
    setPickerSaving(true);
    try {
      const r = await apiPost(
        "/api/v2/memory/relevant_picker/configure", pickerForm, token,
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
      const r = await apiPost(
        "/api/v2/memory/providers/switch",
        { provider: newProvider },
        token,
      );
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
      <${VectorIndexerCard}
        indexer=${indexer}
        showEmb=${showEmb} setShowEmb=${setShowEmb}
        embForm=${embForm} setEmbForm=${setEmbForm}
        embSaving=${embSaving} onSaveEmbedding=${onSaveEmbedding}
      />
      <${AutoDreamCard}
        dream=${dream}
        dreamRunning=${dreamRunning} onDreamNow=${onDreamNow}
        showBackups=${showBackups} onToggleBackups=${onToggleBackups}
        backups=${backups} onRestore=${onRestore}
      />
      <${PinnedCard}
        pinned=${pinned}
        pinDraft=${pinDraft} setPinDraft=${setPinDraft}
        pinBusy=${pinBusy} onAddPin=${onAddPin} onRemovePin=${onRemovePin}
      />
      <${PickerCard}
        picker=${picker}
        pickerForm=${pickerForm} setPickerForm=${setPickerForm}
        pickerSaving=${pickerSaving} onSavePicker=${onSavePicker}
      />
      <${ProviderListSection} data=${data} />
      <${ProviderSwitcher}
        available=${available} selected=${selected}
        busy=${busy} onSwitch=${onSwitch}
      />
      <${WriteProviderHelp} />
    </div>
  `;
}
