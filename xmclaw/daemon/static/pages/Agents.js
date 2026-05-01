// XMclaw — Agents page (B-131 upgrade)
//
// Lists registered agent presets (Epic #17 multi-agent registry) from
// /api/v2/agents and gives the user a real surface to manage them:
//
//   * kind / ready / primary badges so llm vs evolution vs disabled
//     are visible at a glance
//   * model + tool count + system-prompt preview
//   * delete button on non-primary entries
//   * inline create-form (id + free-form config JSON)
//
// Pre-B-131 the page only showed agent_id strings — the user had to
// guess what each agent did.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

async function postJson(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error || d.ok === false) {
    throw new Error(d.error || `HTTP ${r.status}`);
  }
  return d;
}

async function deleteAgent(agentId, token) {
  const url = `/api/v2/agents/${encodeURIComponent(agentId)}`
    + (token ? `?token=${encodeURIComponent(token)}` : "");
  const r = await fetch(url, { method: "DELETE" });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error || d.ok === false) {
    throw new Error(d.error || `HTTP ${r.status}`);
  }
  return d;
}

function AgentRow({ a, onDelete, busy }) {
  const isPrimary = !!a.primary;
  const isEvolution = a.kind === "evolution";
  const ready = a.ready !== false;
  const tone = !ready ? "error" : isEvolution ? "warn" : "success";
  const kindLabel = isEvolution ? "🧬 evolution" : "💬 llm";
  return html`
    <li class="xmc-datapage__row" key=${a.agent_id}>
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap">
        <strong style="font-family:var(--xmc-font-mono)">${a.agent_id}</strong>
        <span style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap">
          ${isPrimary ? html`<${Badge} tone="success">★ primary</${Badge}>` : null}
          <${Badge} tone=${tone} title=${ready ? `${a.kind} agent — ready` : "未就绪 — 缺 LLM 或工具配置"}>${kindLabel}</${Badge}>
          ${!ready ? html`<${Badge} tone="error">未就绪</${Badge}>` : null}
          ${typeof a.tool_count === "number" ? html`<${Badge} tone="muted" title="工具数量">${a.tool_count} tools</${Badge}>` : null}
        </span>
      </div>
      ${a.model
        ? html`<small style="display:block;margin-top:.25rem;color:var(--xmc-fg-muted);font-size:.75rem">模型: <code>${a.model}</code></small>`
        : null}
      ${a.system_prompt_preview
        ? html`<small style="display:block;margin-top:.2rem;color:var(--xmc-fg-muted);font-size:.72rem">${a.system_prompt_preview}…</small>`
        : null}
      ${!isPrimary
        ? html`
            <div style="margin-top:.4rem;display:flex;gap:.4rem">
              <button
                class="xmc-h-btn xmc-h-btn--ghost"
                style="padding:.15rem .55rem;font-size:.72rem"
                disabled=${busy}
                onClick=${() => onDelete(a.agent_id)}
              >删除</button>
            </div>
          `
        : null}
    </li>
  `;
}

function CreateAgentForm({ token, onCreated }) {
  const [agentId, setAgentId] = useState("");
  const [configText, setConfigText] = useState(
    '{\n  "llm": {\n    "provider": "anthropic",\n    "model": "claude-sonnet-4-6"\n  },\n  "system_prompt": "你是一个专注于代码审查的子 agent。"\n}'
  );
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!agentId.trim()) {
      toast.error("agent_id 必填");
      return;
    }
    let config;
    try {
      config = JSON.parse(configText || "{}");
    } catch (err) {
      toast.error(`config JSON 格式错误: ${err.message}`);
      return;
    }
    setBusy(true);
    try {
      await postJson("/api/v2/agents", token, {
        agent_id: agentId.trim(),
        config,
      });
      toast.success(`agent ${agentId} 已创建`);
      setAgentId("");
      setOpen(false);
      onCreated();
    } catch (err) {
      toast.error(`创建失败: ${err.message || err}`);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return html`
      <div style="margin:.6rem 0">
        <button class="xmc-h-btn" onClick=${() => setOpen(true)}>+ 创建新 agent</button>
      </div>
    `;
  }
  return html`
    <form onSubmit=${submit} style="margin:.6rem 0;padding:.7rem;border:1px solid var(--color-border);border-radius:6px;background:color-mix(in srgb, var(--midground) 4%, transparent)">
      <div style="display:flex;gap:.4rem;align-items:center;margin-bottom:.5rem">
        <strong style="font-size:.9rem">创建子 agent</strong>
        <button type="button" class="xmc-h-btn xmc-h-btn--ghost" style="margin-left:auto;padding:.1rem .4rem;font-size:.7rem" onClick=${() => setOpen(false)}>×</button>
      </div>
      <label style="display:block;margin-bottom:.4rem;font-size:.78rem">
        agent_id (字母数字下划线，不能叫 main):
        <input
          type="text"
          value=${agentId}
          onInput=${(e) => setAgentId(e.target.value)}
          placeholder="例: code_reviewer"
          style="display:block;width:100%;margin-top:.2rem;padding:.3rem;font-family:var(--xmc-font-mono);font-size:.8rem"
          required
        />
      </label>
      <label style="display:block;margin-bottom:.4rem;font-size:.78rem">
        config (JSON — 直接抄主 agent config.json 里 llm 节即可):
        <textarea
          value=${configText}
          onInput=${(e) => setConfigText(e.target.value)}
          style="display:block;width:100%;margin-top:.2rem;height:9rem;padding:.3rem;font-family:var(--xmc-font-mono);font-size:.72rem"
        ></textarea>
      </label>
      <div style="display:flex;gap:.4rem">
        <button type="submit" class="xmc-h-btn" disabled=${busy}>${busy ? "创建中…" : "创建"}</button>
        <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => setOpen(false)} disabled=${busy}>取消</button>
      </div>
    </form>
  `;
}

export function AgentsPage({ token }) {
  const [agents, setAgents] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiGet("/api/v2/agents", token)
      .then((d) => {
        const list = Array.isArray(d) ? d : (d && d.agents) || [];
        setAgents(list);
        setError(null);
      })
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  const onDelete = async (agentId) => {
    if (!window.confirm(`确认删除 agent ${agentId}？此操作不可撤销，正在跑的会话会断开。`)) return;
    setBusy(true);
    try {
      await deleteAgent(agentId, token);
      toast.success(`${agentId} 已删除`);
      load();
    } catch (err) {
      toast.error(`删除失败: ${err.message || err}`);
    } finally {
      setBusy(false);
    }
  };

  if (error) return html`<section class="xmc-datapage"><h2>智能体</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!agents) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  const llmCount = agents.filter((a) => a.kind === "llm" || !a.kind).length;
  const evoCount = agents.filter((a) => a.kind === "evolution").length;

  return html`
    <section class="xmc-datapage" aria-labelledby="agents-title">
      <header class="xmc-datapage__header">
        <h2 id="agents-title">智能体</h2>
        <p class="xmc-datapage__subtitle">
          已注册 ${agents.length} 个 — ${llmCount} 个对话型 (llm)${evoCount > 0 ? `，${evoCount} 个观察者 (evolution)` : ""}。
          <code>main</code> 是主 agent (config.json)，其他是子 agent — 主 agent 可通过
          <code>chat_with_agent</code> / <code>submit_to_agent</code> 工具派活给它们。
        </p>
      </header>
      <${CreateAgentForm} token=${token} onCreated=${load} />
      <ul class="xmc-datapage__list">
        ${agents.map((a) => html`<${AgentRow} a=${a} onDelete=${onDelete} busy=${busy} />`)}
      </ul>
    </section>
  `;
}
