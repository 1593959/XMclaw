// XMclaw — model provider presets (B-148)
//
// Curated catalogue of providers + their typical base_url + popular
// model names. The "+ 添加模型" wizard reads from here so users don't
// have to know that MiniMax exposes Anthropic-compat at
// /anthropic, or what DeepSeek's base_url is.

export const PROVIDER_PRESETS = [
  {
    id: "anthropic",
    label: "Anthropic / Claude",
    icon: "🧠",
    provider_kind: "anthropic",  // wire-protocol kind for daemon factory
    base_url: "https://api.anthropic.com",
    models: [
      "claude-opus-4-7",
      "claude-sonnet-4-6",
      "claude-haiku-4-5-20251001",
    ],
    note: "官方 Claude API。需 https://console.anthropic.com 申请 key (sk-ant-...)。",
  },
  {
    id: "openai",
    label: "OpenAI",
    icon: "🤖",
    provider_kind: "openai",
    base_url: "https://api.openai.com/v1",
    models: ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "o1-mini", "o3-mini"],
    note: "官方 OpenAI API。Key 形如 sk-...。",
  },
  {
    id: "minimax",
    label: "MiniMax 海螺",
    icon: "🐚",
    provider_kind: "anthropic",  // MiniMax 提供 Anthropic 兼容端点
    base_url: "https://api.minimaxi.com/anthropic",
    models: [
      "minimax-portal/MiniMax-M2.7-highspeed",
      "minimax-portal/MiniMax-M2.7",
    ],
    note: "MiniMax 海螺，用 Anthropic 协议接入。Key 在 https://platform.minimaxi.com 申请。",
  },
  {
    id: "deepseek",
    label: "DeepSeek 深度求索",
    icon: "🌊",
    provider_kind: "openai",
    base_url: "https://api.deepseek.com/v1",
    models: ["deepseek-chat", "deepseek-reasoner"],
    note: "DeepSeek，OpenAI 兼容协议。Key 在 https://platform.deepseek.com 申请。",
  },
  {
    id: "moonshot",
    label: "月之暗面 Kimi",
    icon: "🌙",
    provider_kind: "openai",
    base_url: "https://api.moonshot.cn/v1",
    models: ["moonshot-v1-32k", "moonshot-v1-128k", "kimi-latest"],
    note: "月之暗面 Kimi，OpenAI 兼容。https://platform.moonshot.cn 申请 key。",
  },
  {
    id: "glm",
    label: "智谱 GLM",
    icon: "✨",
    provider_kind: "openai",
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    models: ["glm-4-plus", "glm-4-flash", "glm-4-air"],
    note: "智谱 GLM，OpenAI 兼容。https://bigmodel.cn 申请 key。",
  },
  {
    id: "qwen",
    label: "通义千问 Qwen",
    icon: "💎",
    provider_kind: "openai",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    models: ["qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-72b-instruct"],
    note: "阿里云通义千问，OpenAI 兼容。https://dashscope.console.aliyun.com 申请。",
  },
  {
    id: "ollama",
    label: "Ollama 本地",
    icon: "🏠",
    provider_kind: "openai",
    base_url: "http://localhost:11434/v1",
    models: ["llama3.2", "qwen2.5", "deepseek-r1", "gemma2"],
    note: "本地 Ollama，无需 API key（填任意值即可）。先 ollama serve 启动再用。",
  },
  {
    id: "custom",
    label: "自定义 / 其他兼容 API",
    icon: "🔧",
    provider_kind: "openai",  // 默认按 OpenAI 兼容；用户可改
    base_url: "",
    models: [],
    note: "任何 OpenAI 或 Anthropic 兼容的服务。手填 base_url + 模型名。",
  },
];

export function findPreset(presetId) {
  return PROVIDER_PRESETS.find((p) => p.id === presetId) || null;
}

// Best-guess match for an existing profile's provider_kind + base_url
// → which preset card is "active" when editing. Returns the preset id
// or "custom" when no clean match.
export function presetIdFromProfile(profile) {
  if (!profile) return "custom";
  const url = (profile.base_url || "").replace(/\/$/, "");
  const kind = (profile.provider || profile.provider_kind || "").toLowerCase();
  for (const p of PROVIDER_PRESETS) {
    if (p.id === "custom") continue;
    if (p.provider_kind !== kind) continue;
    const presetUrl = (p.base_url || "").replace(/\/$/, "");
    if (presetUrl && url && presetUrl === url) return p.id;
  }
  return "custom";
}
