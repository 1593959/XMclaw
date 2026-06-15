// 模型配置（Proma 式）— 渠道列表。把扁平的 profile 列表按
// provider+base_url 分组成"渠道"，每渠道一卡：图标/名称/模型计数/
// 启用开关。点击编辑；"+ 添加配置"进编辑器。
//
// 一个 profile = 一个模型；渠道 = 同端点的一组 profile。

import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet, apiPatch } from "../lib/api";

const ChannelEditor = lazy(() => import("./ChannelEditor"));

interface DiskProfile {
  id: string;
  label: string;
  provider: string;
  model: string;
  base_url: string;
  api_key_redacted: string;
  enabled: boolean;
  // Phase 11：能力标签 + 分类，影响图标 / 调度路由。
  capabilities?: string[];
  category?: string;
  tier?: string;
  // 视觉开关（显式覆盖；undefined = 后端走启发式）。
  supports_vision?: boolean;
}

export interface ChannelDraft {
  key: string; // "" = 新建
  provider: string;
  name: string;
  base_url: string;
  enabled: boolean;
  // 编辑既有渠道时附带 profile id；新建渠道时为空字符串。
  // 编辑器的 save() 用此判断哪些 profile 被移除，需要 DELETE。
  models: {
    profileId: string;
    modelId: string;
    label: string;
    enabled: boolean;
    // Phase 11：能力 + 分类（覆盖默认推断）。
    capabilities?: string[];
    category?: string;
    tier?: string;
    // 视觉开关（显式覆盖）。
    supportsVision?: boolean;
  }[];
}

interface Channel {
  key: string;
  provider: string;
  base_url: string;
  name: string;
  profiles: DiskProfile[];
  enabledCount: number;
}

const PROVIDER_META: Record<string, { label: string; icon: string }> = {
  anthropic: { label: "Anthropic", icon: "✳" },
  openai: { label: "OpenAI", icon: "◎" },
  openrouter: { label: "OpenRouter", icon: "⤳" },
  openai_compat: { label: "OpenAI 兼容格式", icon: "▥" },
};

function channelName(provider: string, profiles: DiskProfile[]): string {
  // 取 profile label 的公共前缀，否则用 provider 名。
  const labels = profiles.map((p) => p.label).filter(Boolean);
  if (labels.length) {
    const first = labels[0].split(/[-·\s]/)[0];
    if (first && first.length > 1) return first;
  }
  return PROVIDER_META[provider]?.label || provider;
}

export default function ModelConfig() {
  const token = useApp((s) => s.token);
  const refreshProfiles = useApp((s) => s.refreshProfiles);
  const showToast = useApp((s) => s.showToast);
  const [disk, setDisk] = useState<DiskProfile[]>([]);
  const [editing, setEditing] = useState<ChannelDraft | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (!token) return;
    setLoading(true);
    apiGet<{ on_disk?: DiskProfile[] }>("/api/v2/llm/profiles", token)
      .then((d) => setDisk(d?.on_disk || []))
      .catch(() => setDisk([]))
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(load, [load]);

  // 分组成渠道。
  const channels: Channel[] = (() => {
    const map = new Map<string, DiskProfile[]>();
    for (const p of disk) {
      const key = `${p.provider}::${p.base_url}`;
      (map.get(key) || map.set(key, []).get(key)!).push(p);
    }
    return [...map.entries()].map(([key, profiles]) => ({
      key,
      provider: profiles[0].provider,
      base_url: profiles[0].base_url,
      name: channelName(profiles[0].provider, profiles),
      profiles,
      enabledCount: profiles.filter((p) => p.enabled).length,
    }));
  })();

  // runtime 里能跑 agent 的渠道（这里简单视作所有已启用渠道）。
  const agentChannels = channels.filter((c) => c.enabledCount > 0);

  async function toggleChannel(c: Channel, on: boolean) {
    // 渠道开关 = 批量翻所有模型 profile。
    setDisk((ds) =>
      ds.map((p) => (c.profiles.some((x) => x.id === p.id) ? { ...p, enabled: on } : p)),
    );
    try {
      await Promise.all(
        c.profiles.map((p) =>
          apiPatch(`/api/v2/llm/profiles/${encodeURIComponent(p.id)}/enabled`, { enabled: on }, token),
        ),
      );
      showToast(on ? "渠道已启用" : "渠道已禁用", "ok");
      refreshProfiles();
    } catch {
      showToast("切换失败", "err");
      load();
    }
  }

  function editChannel(c: Channel) {
    setEditing({
      key: c.key,
      provider: c.provider,
      name: c.name,
      base_url: c.base_url,
      enabled: c.enabledCount > 0,
      models: c.profiles.map((p) => ({
        profileId: p.id,
        modelId: p.model,
        label: p.label,
        enabled: p.enabled,
        capabilities: p.capabilities || [],
        category: p.category || "",
        tier: p.tier || "",
        supportsVision: p.supports_vision,
      })),
    });
  }

  if (editing) {
    return (
      <Suspense fallback={<div className="p-5 text-mc-faint text-sm">加载中…</div>}>
        <ChannelEditor
          draft={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
            refreshProfiles();
          }}
        />
      </Suspense>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-6 min-h-0">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-base font-semibold">模型配置</h2>
          <p className="text-xs text-mc-faint mt-0.5">
            管理 AI 供应商连接，配置 API Key 和可用模型。Anthropic 渠道同时可用于 Agent 模式
          </p>
        </div>
        <button
          onClick={() =>
            setEditing({ key: "", provider: "openai_compat", name: "", base_url: "", enabled: true, models: [] })
          }
          className="shrink-0 px-3.5 py-2 rounded-md border border-mc-border text-sm text-mc-text hover:border-mc-accent/50 cursor-pointer"
        >
          ＋ 添加配置
        </button>
      </div>

      {loading ? (
        <div className="text-xs text-mc-faint">加载中…</div>
      ) : channels.length === 0 ? (
        <div className="text-center text-sm text-mc-faint py-10 border border-dashed border-mc-border rounded-lg">
          还没有配置任何供应商 — 点击右上「添加配置」
        </div>
      ) : (
        <div className="space-y-px">
          {channels.map((c) => (
            <ChannelRow key={c.key} c={c} onEdit={() => editChannel(c)} onToggle={(on) => toggleChannel(c, on)} />
          ))}
        </div>
      )}

      {/* Agent 供应商 */}
      {agentChannels.length > 0 && (
        <div className="pt-4">
          <h3 className="text-sm font-semibold">Agent 供应商</h3>
          <p className="text-xs text-mc-faint mt-0.5 mb-3">
            启用 Agent 模式可用的供应商，支持同时开启多个渠道，在 Agent 模式下可直接切换
          </p>
          <div className="space-y-px">
            {agentChannels.map((c) => (
              <div key={c.key} className="flex items-center gap-3 py-3 border-b border-mc-border/40">
                <ProviderIcon provider={c.provider} />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium">{c.name}</div>
                  <div className="text-xs text-mc-faint">
                    {PROVIDER_META[c.provider]?.label || c.provider} · {c.enabledCount} 个模型可用
                  </div>
                </div>
                <span className="w-9 h-5 rounded-full bg-mc-accent relative shrink-0">
                  <span className="absolute top-0.5 right-0.5 w-4 h-4 rounded-full bg-white" />
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ProviderIcon({ provider }: { provider: string }) {
  const meta = PROVIDER_META[provider];
  const tone =
    provider === "anthropic"
      ? "bg-[#d97757] text-white"
      : provider === "openai"
        ? "bg-[#10a37f] text-white"
        : "bg-mc-panel2 text-mc-muted border border-mc-border";
  return (
    <div className={"w-9 h-9 rounded-lg flex items-center justify-center text-base shrink-0 " + tone}>
      {meta?.icon || "▥"}
    </div>
  );
}

function ChannelRow({ c, onEdit, onToggle }: { c: Channel; onEdit: () => void; onToggle: (on: boolean) => void }) {
  const on = c.enabledCount > 0;
  const meta = PROVIDER_META[c.provider];
  return (
    <div className="flex items-center gap-3 py-3 border-b border-mc-border/40 group">
      <ProviderIcon provider={c.provider} />
      <button onClick={onEdit} className="flex-1 min-w-0 text-left cursor-pointer">
        <div className="text-sm font-medium">{c.name}</div>
        <div className="text-xs text-mc-faint">
          {meta?.label || c.provider}
          {" · "}
          {on ? `${c.enabledCount} 个模型已启用` : `${c.profiles.length} 个模型`}
          {c.provider === "anthropic" && on && " · 可用于 Agent"}
        </div>
      </button>
      <button
        onClick={() => onToggle(!on)}
        className={"relative w-9 rounded-full transition-colors cursor-pointer shrink-0 " + (on ? "bg-mc-accent" : "bg-mc-border")}
        style={{ height: 20 }}
        aria-pressed={on}
        title={on ? "禁用渠道" : "启用渠道"}
      >
        <span
          className="absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform"
          style={{ transform: on ? "translateX(16px)" : "translateX(0)" }}
        />
      </button>
    </div>
  );
}
