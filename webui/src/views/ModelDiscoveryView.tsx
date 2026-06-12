// 模型发现 — 输入 base_url + api_key → 拉取可用模型 → 多选 → 批量注册

import { useState } from "react";
import { useApp } from "../store/app";
import { apiPost } from "../lib/api";

interface DiscoveredModel {
  id: string;
  name: string;
  context_length?: number;
  created_at?: number;
  created_human?: string;
}

interface DiscoverResult {
  ok: boolean;
  base_url: string;
  endpoint_id: string;
  provider: string;
  api_key_redacted: string;
  models: DiscoveredModel[];
  model_count: number;
  fetched_at?: number;
  elapsed_ms: number;
  error?: string;
  note?: string;
  connectivity_ok?: boolean;
}

function formatCtxLen(ctx?: number): string {
  if (!ctx) return "—";
  if (ctx >= 1_000_000) return `${(ctx / 1_000_000).toFixed(1)}M`;
  if (ctx >= 1_000) return `${(ctx / 1_000).toFixed(0)}K`;
  return String(ctx);
}

export default function ModelDiscoveryView() {
  const token = useApp((s) => s.token);
  const storeRefreshProfiles = useApp((s) => s.refreshProfiles);

  // Discovery form state
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [provider, setProvider] = useState("openai");
  const [discovered, setDiscovered] = useState<DiscoverResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Bulk options
  const [maxTokens, setMaxTokens] = useState("");
  const [promptCache, setPromptCache] = useState<string>("");
  const [extendedThinking, setExtendedThinking] = useState<string>("");

  const onRefreshProfiles = async () => {
    await storeRefreshProfiles();
  };

  const handleDiscover = async () => {
    setError(null);
    setDiscovered(null);
    setSelected(new Set());
    if (!baseUrl.trim() || !apiKey.trim()) {
      setError("base_url 和 api_key 均为必填");
      return;
    }
    setLoading(true);
    try {
      const result = await apiPost<DiscoverResult>(
        "/api/v2/llm/endpoints/discover",
        { base_url: baseUrl.trim(), api_key: apiKey.trim(), provider },
        token,
      );
      if (!result.ok) {
        setError(result.error || "发现失败");
        setDiscovered(null);
      } else {
        setDiscovered(result);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  const toggleModel = (modelId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(modelId)) next.delete(modelId);
      else next.add(modelId);
      return next;
    });
  };

  const selectAll = () => {
    if (!discovered) return;
    if (selected.size === discovered.models.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(discovered.models.map((m) => m.id)));
    }
  };

  const handleApply = async () => {
    if (!discovered || selected.size === 0) return;
    setApplying(true);
    setError(null);
    try {
      const models = discovered.models
        .filter((m) => selected.has(m.id))
        .map((m) => m.id);

      const options: Record<string, unknown> = {};
      if (maxTokens.trim()) {
        const v = parseInt(maxTokens.trim(), 10);
        if (!isNaN(v) && v > 0) options.max_tokens = v;
      }
      if (promptCache === "true") options.prompt_cache_enabled = true;
      else if (promptCache === "false") options.prompt_cache_enabled = false;
      if (extendedThinking === "true") options.extended_thinking = true;
      else if (extendedThinking === "false") options.extended_thinking = false;

      const result = await apiPost<{ ok: boolean; hotloaded?: string[]; failed?: unknown[]; error?: string }>(
        "/api/v2/llm/endpoints/apply",
        {
          endpoint_id: discovered.endpoint_id,
          base_url: discovered.base_url,
          api_key: apiKey.trim(),
          provider: discovered.provider,
          models,
          options,
        },
        token,
      );
      if (!result.ok) {
        setError(result.error || "注册失败");
      } else {
        const count = result.hotloaded?.length || models.length;
        setError(null);
        // Reset form
        setDiscovered(null);
        setSelected(new Set());
        // Refresh profile list（apply 走热加载，无需重启即可在 Composer 选用）
        await onRefreshProfiles();
        useApp.getState().showToast(`已注册 ${count} 个模型，已热加载可直接选用`, "ok");
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
    } finally {
      setApplying(false);
    }
  };

  if (!token) {
    return <div className="p-5 text-mc-muted">未认证</div>;
  }

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-5 min-h-0 flex flex-col">
      <div className="shrink-0">
        <h2 className="text-base font-semibold">模型发现</h2>
        <p className="text-xs text-mc-faint mt-0.5">
          输入端点 URL + API Key，拉取可用模型并批量注册
        </p>
      </div>

      {/* Discovery form */}
      <div className="shrink-0 space-y-3">
        <div className="flex gap-2">
          <input
            type="url"
            placeholder="https://api.openai.com/v1"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            className="flex-1 rounded-md bg-mc-panel2 border border-mc-border px-3 py-2 text-sm text-mc-text placeholder:text-mc-faint focus:outline-none focus:border-mc-accent"
          />
          <input
            type="password"
            placeholder="sk-..."
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleDiscover();
            }}
            className="flex-1 rounded-md bg-mc-panel2 border border-mc-border px-3 py-2 text-sm text-mc-text placeholder:text-mc-faint focus:outline-none focus:border-mc-accent"
          />
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className="rounded-md bg-mc-panel2 border border-mc-border px-2 py-2 text-sm text-mc-text focus:outline-none focus:border-mc-accent"
          >
            <option value="openai">OpenAI</option>
            <option value="anthropic">Anthropic</option>
            <option value="openrouter">OpenRouter</option>
            <option value="openai_compat">OpenAI兼容</option>
          </select>
          <button
            onClick={handleDiscover}
            disabled={loading || !baseUrl.trim() || !apiKey.trim()}
            className="shrink-0 rounded-md bg-mc-accent px-4 py-2 text-sm font-medium text-white hover:bg-mc-accent/90 disabled:opacity-50"
          >
            {loading ? "拉取中..." : "拉取模型"}
          </button>
        </div>

        {error && (
          <div className="text-sm text-mc-err bg-mc-err/10 border border-mc-err/30 rounded-md px-3 py-2">
            {error}
          </div>
        )}
      </div>

      {/* Discovered models */}
      {discovered && (
        <div className="shrink-0 space-y-3">
          <div className="flex items-center justify-between">
            <div className="text-sm">
              <span className="text-mc-faint">{discovered.base_url}</span>
              <span className="ml-2 text-mc-muted">已拉取 {discovered.model_count} 个模型 ({discovered.elapsed_ms.toFixed(0)}ms)</span>
              {discovered.connectivity_ok !== undefined && (
                <span className={`ml-2 ${discovered.connectivity_ok ? "text-mc-ok" : "text-mc-err"}`}>
                  {discovered.connectivity_ok ? "API Key 有效" : "API Key 无效"}
                </span>
              )}
            </div>
            <button
              onClick={selectAll}
              className="text-xs text-mc-accent hover:underline"
            >
              {selected.size === discovered.models.length ? "取消全选" : "全选"}
            </button>
          </div>

          {/* Options */}
          <div className="flex gap-3 flex-wrap text-xs">
            <label className="flex items-center gap-1">
              max_tokens
              <input
                type="number"
                placeholder="可选"
                value={maxTokens}
                onChange={(e) => setMaxTokens(e.target.value)}
                className="w-24 rounded bg-mc-panel2 border border-mc-border px-2 py-1 text-sm focus:outline-none focus:border-mc-accent"
              />
            </label>
            <label className="flex items-center gap-1">
              prompt_cache
              <select
                value={promptCache}
                onChange={(e) => setPromptCache(e.target.value)}
                className="rounded bg-mc-panel2 border border-mc-border px-2 py-1 text-sm focus:outline-none focus:border-mc-accent"
              >
                <option value="">默认</option>
                <option value="true">启用</option>
                <option value="false">禁用</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              extended_thinking
              <select
                value={extendedThinking}
                onChange={(e) => setExtendedThinking(e.target.value)}
                className="rounded bg-mc-panel2 border border-mc-border px-2 py-1 text-sm focus:outline-none focus:border-mc-accent"
              >
                <option value="">默认</option>
                <option value="true">启用</option>
                <option value="false">禁用</option>
              </select>
            </label>
          </div>

          {/* Model list */}
          <div className="border border-mc-border rounded-md overflow-hidden">
            <div className="max-h-64 overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="bg-mc-panel2 sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-2 text-xs font-medium text-mc-faint w-10">
                      <input
                        type="checkbox"
                        checked={selected.size === discovered.models.length && discovered.models.length > 0}
                        onChange={selectAll}
                        className="accent-mc-accent"
                      />
                    </th>
                    <th className="text-left px-3 py-2 text-xs font-medium text-mc-faint">ID</th>
                    <th className="text-left px-3 py-2 text-xs font-medium text-mc-faint">名称</th>
                    <th className="text-left px-3 py-2 text-xs font-medium text-mc-faint">上下文</th>
                  </tr>
                </thead>
                <tbody>
                  {discovered.models.map((m) => (
                    <tr
                      key={m.id}
                      className={`border-t border-mc-border/50 cursor-pointer hover:bg-mc-panel2/50 ${
                        selected.has(m.id) ? "bg-mc-accent/10" : ""
                      }`}
                      onClick={() => toggleModel(m.id)}
                    >
                      <td className="px-3 py-2">
                        <input
                          type="checkbox"
                          checked={selected.has(m.id)}
                          onChange={() => toggleModel(m.id)}
                          className="accent-mc-accent"
                        />
                      </td>
                      <td className="px-3 py-2 font-mono text-xs truncate max-w-[300px]" title={m.id}>
                        {m.id}
                      </td>
                      <td className="px-3 py-2 text-mc-text">{m.name}</td>
                      <td className="px-3 py-2 text-mc-muted font-mono text-xs">
                        {formatCtxLen(m.context_length)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="border-t border-mc-border px-3 py-2 flex items-center justify-between bg-mc-panel2">
              <span className="text-xs text-mc-muted">
                已选 {selected.size}/{discovered.models.length}
              </span>
              <button
                onClick={handleApply}
                disabled={selected.size === 0 || applying}
                className="rounded-md bg-mc-ok px-4 py-1.5 text-xs font-medium text-white hover:bg-mc-ok/90 disabled:opacity-50"
              >
                {applying ? "注册中..." : `注册 ${selected.size} 个模型`}
              </button>
            </div>
          </div>

          {discovered.note && (
            <div className="text-xs text-mc-muted bg-mc-panel2 rounded-md px-3 py-2">
              {discovered.note}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
