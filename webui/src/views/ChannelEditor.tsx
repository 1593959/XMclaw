// 渠道编辑器（Proma 式）— 新增/编辑一个供应商渠道。
// 渠道 = 同 provider+base_url 的一组模型 profile。
// provider 类型 / 名称 / Base URL(+预览) / API Key(+测试连接+眼睛) /
// 启用开关 / 已启用模型 / 可用模型(从供应商获取 + 手动添加)。

import { useState } from "react";
import { useApp } from "../store/app";
import { apiPost } from "../lib/api";
import type { ChannelDraft } from "./ModelConfig";

const PROVIDERS = [
  { id: "anthropic", label: "Anthropic", preview: "https://api.anthropic.com/v1/messages", base: "https://api.anthropic.com" },
  { id: "openai", label: "OpenAI", preview: "https://api.openai.com/v1/chat/completions", base: "https://api.openai.com/v1" },
  { id: "openrouter", label: "OpenRouter", preview: "https://openrouter.ai/api/v1/chat/completions", base: "https://openrouter.ai/api/v1" },
  { id: "openai_compat", label: "OpenAI 兼容格式", preview: "<base_url>/chat/completions", base: "" },
];

interface ModelRow {
  modelId: string;
  label: string;
  enabled: boolean;
}

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "m";
}

export default function ChannelEditor({
  draft,
  onClose,
  onSaved,
}: {
  draft: ChannelDraft;
  onClose: () => void;
  onSaved: () => void;
}) {
  const token = useApp((s) => s.token);
  const showToast = useApp((s) => s.showToast);
  const [provider, setProvider] = useState(draft.provider || "openai_compat");
  const [name, setName] = useState(draft.name || "");
  const [baseUrl, setBaseUrl] = useState(draft.base_url || "");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [enabled, setEnabled] = useState(draft.enabled ?? true);
  const [models, setModels] = useState<ModelRow[]>(draft.models || []);
  const [newModelId, setNewModelId] = useState("");
  const [newModelLabel, setNewModelLabel] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [fetching, setFetching] = useState(false);
  const [available, setAvailable] = useState<{ id: string; name: string }[]>([]);
  const [saving, setSaving] = useState(false);

  const pmeta = PROVIDERS.find((p) => p.id === provider);
  const editingKey = draft.key; // 非空 = 编辑既有渠道

  async function testConnection() {
    if (!baseUrl.trim() || !apiKey.trim()) {
      setTestResult("需要 Base URL 和 API Key");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const r = await apiPost<{ ok: boolean; connectivity_ok?: boolean; model_count?: number; error?: string }>(
        "/api/v2/llm/endpoints/discover",
        { base_url: baseUrl.trim(), api_key: apiKey.trim(), provider },
        token,
      );
      if (r.ok && r.connectivity_ok !== false) {
        setTestResult(`连接成功 · ${r.model_count ?? 0} 个模型可用`);
      } else {
        setTestResult(r.error || "连接失败 — 检查 Base URL / API Key");
      }
    } catch (e) {
      setTestResult(e instanceof Error ? e.message : String(e));
    } finally {
      setTesting(false);
    }
  }

  async function fetchModels() {
    if (!baseUrl.trim() || !apiKey.trim()) {
      showToast("需要 Base URL 和 API Key", "err");
      return;
    }
    setFetching(true);
    try {
      const r = await apiPost<{ ok: boolean; models?: { id: string; name: string }[]; error?: string }>(
        "/api/v2/llm/endpoints/discover",
        { base_url: baseUrl.trim(), api_key: apiKey.trim(), provider },
        token,
      );
      if (r.ok && r.models) {
        const existing = new Set(models.map((m) => m.modelId));
        setAvailable(r.models.filter((m) => !existing.has(m.id)));
      } else {
        showToast(r.error || "拉取失败", "err");
      }
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setFetching(false);
    }
  }

  function addModel(id: string, label?: string) {
    const mid = id.trim();
    if (!mid || models.some((m) => m.modelId === mid)) return;
    setModels((ms) => [...ms, { modelId: mid, label: (label || "").trim(), enabled: true }]);
    setAvailable((av) => av.filter((m) => m.id !== mid));
  }

  async function save() {
    if (models.length === 0) {
      showToast("至少添加一个模型", "err");
      return;
    }
    if (!editingKey && !apiKey.trim()) {
      showToast("新建渠道需要 API Key", "err");
      return;
    }
    setSaving(true);
    const namePrefix = slug(name || provider);
    let okCount = 0;
    try {
      for (const m of models) {
        const pid = `${namePrefix}-${slug(m.modelId)}`;
        const body: Record<string, unknown> = {
          id: pid,
          label: m.label || m.modelId,
          provider,
          model: m.modelId,
          base_url: baseUrl.trim() || undefined,
          enabled: enabled && m.enabled,
        };
        // api_key 留空时后端保留既有；新建必填已校验。
        if (apiKey.trim()) body.api_key = apiKey.trim();
        const r = await apiPost<{ ok: boolean; error?: string }>("/api/v2/llm/profiles", body, token);
        if (r.ok) okCount += 1;
      }
      // hotload 让新 profile 立即可选。
      await apiPost("/api/v2/llm/endpoints/hotload", { profiles: [] }, token).catch(() => {});
      showToast(`已保存 ${okCount} 个模型`, "ok");
      onSaved();
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-5 min-h-0">
      <div className="flex items-center gap-2">
        <button onClick={onClose} className="text-mc-faint hover:text-mc-text cursor-pointer text-sm">
          ← 返回
        </button>
        <h2 className="text-base font-semibold">{editingKey ? "编辑渠道" : "添加配置"}</h2>
      </div>

      <Field label="供应商类型">
        <select
          value={provider}
          onChange={(e) => {
            setProvider(e.target.value);
            const p = PROVIDERS.find((x) => x.id === e.target.value);
            if (p && !baseUrl) setBaseUrl(p.base);
          }}
          disabled={!!editingKey}
          className="w-full rounded-md bg-mc-panel2 border border-mc-border px-3 py-2 text-sm outline-none focus:border-mc-accent disabled:opacity-60"
        >
          {PROVIDERS.map((p) => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
      </Field>

      <Field label="供应商名称">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={`例如: My ${pmeta?.label || ""}`}
          className="w-full rounded-md bg-mc-panel2 border border-mc-border px-3 py-2 text-sm outline-none focus:border-mc-accent placeholder:text-mc-faint"
        />
      </Field>

      <Field label="Base URL" hint={`预览：${pmeta?.preview || ""}`}>
        <input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder={pmeta?.base || "https://..."}
          className="w-full rounded-md bg-mc-panel2 border border-mc-border px-3 py-2 text-sm outline-none focus:border-mc-accent placeholder:text-mc-faint"
        />
      </Field>

      <Field
        label="API Key"
        action={
          <button
            onClick={testConnection}
            disabled={testing}
            className="text-xs px-2.5 py-1 rounded border border-mc-border text-mc-muted hover:text-mc-accent hover:border-mc-accent/50 cursor-pointer disabled:opacity-50"
          >
            {testing ? "测试中…" : "⚡ 测试连接"}
          </button>
        }
      >
        <div className="relative">
          <input
            type={showKey ? "text" : "password"}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={editingKey ? "留空保留现有 Key" : "输入 API Key"}
            className="w-full rounded-md bg-mc-panel2 border border-mc-border px-3 py-2 pr-9 text-sm outline-none focus:border-mc-accent placeholder:text-mc-faint"
          />
          <button
            onClick={() => setShowKey((v) => !v)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-mc-faint hover:text-mc-text cursor-pointer text-sm"
            aria-label="显示/隐藏"
          >
            {showKey ? "🙈" : "👁"}
          </button>
        </div>
        {testResult && (
          <div className={"text-xs mt-1.5 " + (testResult.includes("成功") ? "text-mc-ok" : "text-mc-err")}>
            {testResult}
          </div>
        )}
      </Field>

      <div className="flex items-center justify-between py-1">
        <div>
          <div className="text-sm">启用此渠道</div>
          <div className="text-xs text-mc-faint">关闭后该渠道不会在模型选择中出现</div>
        </div>
        <Toggle on={enabled} onClick={() => setEnabled((v) => !v)} />
      </div>

      {/* 已启用模型 */}
      <div>
        <div className="text-sm font-medium mb-2">已启用模型</div>
        {models.length === 0 ? (
          <div className="text-center text-xs text-mc-faint py-6 border border-dashed border-mc-border rounded-md">
            还没有启用任何模型，从下方可用模型中选择
          </div>
        ) : (
          <div className="flex gap-2 flex-wrap">
            {models.map((m) => (
              <span
                key={m.modelId}
                className="flex items-center gap-1.5 text-[12px] px-2.5 py-1 rounded-full border border-mc-border bg-mc-panel2"
              >
                <span className="font-mono">{m.modelId}</span>
                {m.label && <span className="text-mc-faint">· {m.label}</span>}
                <button
                  onClick={() => setModels((ms) => ms.filter((x) => x.modelId !== m.modelId))}
                  className="text-mc-faint hover:text-mc-err cursor-pointer"
                  aria-label="移除"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* 可用模型 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-medium">可用模型</span>
          <button
            onClick={fetchModels}
            disabled={fetching}
            className="text-xs px-2.5 py-1 rounded border border-mc-border text-mc-muted hover:text-mc-accent hover:border-mc-accent/50 cursor-pointer disabled:opacity-50"
          >
            {fetching ? "获取中…" : "⬇ 从供应商获取"}
          </button>
        </div>
        <div className="flex gap-2 mb-2">
          <input
            value={newModelId}
            onChange={(e) => setNewModelId(e.target.value)}
            placeholder="模型 ID（如 claude-opus-4-6）"
            className="flex-1 rounded-md bg-mc-panel2 border border-mc-border px-3 py-1.5 text-sm outline-none focus:border-mc-accent placeholder:text-mc-faint"
          />
          <input
            value={newModelLabel}
            onChange={(e) => setNewModelLabel(e.target.value)}
            placeholder="显示名称（可选）"
            className="flex-1 rounded-md bg-mc-panel2 border border-mc-border px-3 py-1.5 text-sm outline-none focus:border-mc-accent placeholder:text-mc-faint"
          />
          <button
            onClick={() => {
              addModel(newModelId, newModelLabel);
              setNewModelId("");
              setNewModelLabel("");
            }}
            className="px-3 rounded-md border border-mc-border text-mc-muted hover:text-mc-accent cursor-pointer text-lg leading-none"
          >
            ＋
          </button>
        </div>
        {available.length > 0 && (
          <div className="max-h-48 overflow-y-auto border border-mc-border rounded-md divide-y divide-mc-border/50">
            {available.map((m) => (
              <button
                key={m.id}
                onClick={() => addModel(m.id, m.name)}
                className="w-full text-left px-3 py-1.5 hover:bg-mc-panel2 cursor-pointer flex items-center gap-2"
              >
                <span className="text-mc-accent text-xs">＋</span>
                <span className="font-mono text-[12px] truncate">{m.id}</span>
                {m.name && m.name !== m.id && <span className="text-mc-faint text-xs truncate">{m.name}</span>}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex gap-2 pt-2 sticky bottom-0 bg-mc-bg py-3">
        <button
          onClick={save}
          disabled={saving}
          className="px-4 py-2 rounded-md bg-mc-accent text-white text-sm font-medium hover:bg-mc-accent-dim cursor-pointer disabled:opacity-50"
        >
          {saving ? "保存中…" : "保存渠道"}
        </button>
        <button
          onClick={onClose}
          className="px-4 py-2 rounded-md border border-mc-border text-mc-muted hover:text-mc-text cursor-pointer text-sm"
        >
          取消
        </button>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  action,
  children,
}: {
  label: string;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-sm font-medium">{label}</span>
        {action}
      </div>
      {hint && <div className="text-xs text-mc-faint mb-1.5">{hint}</div>}
      {children}
    </div>
  );
}

function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={"relative w-10 h-5.5 rounded-full transition-colors cursor-pointer shrink-0 " + (on ? "bg-mc-accent" : "bg-mc-border")}
      style={{ height: 22 }}
      aria-pressed={on}
    >
      <span
        className="absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform"
        style={{ transform: on ? "translateX(18px)" : "translateX(0)" }}
      />
    </button>
  );
}
