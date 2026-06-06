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
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { useSafeFetch } from "../lib/use_safe_fetch.js";
import { toast } from "../lib/toast.js";
import { Vitals, VitalsCell, Readout, Sparkbar } from "../components/molecules/Instrument.js";

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
      ${(a.role || a.goal)
        ? html`<div style="display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.3rem">
            ${a.role ? html`<${Badge} tone="purple" title="人设角色">🎭 ${a.role}</${Badge}>` : null}
            ${a.goal ? html`<span style="font-size:.72rem;color:var(--xmc-fg-muted)">🎯 ${a.goal}</span>` : null}
          </div>`
        : null}
      ${a.backstory
        ? html`<small style="display:block;margin-top:.2rem;color:var(--xmc-fg-muted);font-size:.7rem">📖 ${a.backstory}</small>`
        : null}
      ${a.model
        ? html`<small style="display:block;margin-top:.25rem;color:var(--xmc-fg-muted);font-size:.75rem">模型: <code>${a.model}</code></small>`
        : null}
      ${(a.system_prompt_preview && !a.role)
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

// B-134: persona templates so users don't have to hand-write config
// JSON for common sub-agent roles. Each template seeds agent_id +
// system_prompt; the LLM block stays empty so the sub-agent inherits
// the daemon's primary LLM config (lazy fallback in build_workspace).
const PERSONA_TEMPLATES = [
  {
    key: "code_reviewer",
    label: "🔍 代码审查",
    agent_id: "code_reviewer",
    role: "代码审查员",
    goal: "找出代码里的 bug、安全与性能问题并给出可落地的修改建议",
    backstory: "资深工程师，挑剔但只对事不对人，从不空泛表扬",
    system_prompt: (
      "你是 XMclaw 的代码审查子 agent。当主 agent 派活给你时：\n"
      + "1. 用 file_read / list_dir 看完相关文件\n"
      + "2. 找潜在 bug、安全问题、性能坑、命名不一致\n"
      + "3. 用 bullet 列表给具体行号 + 修改建议\n"
      + "保持简洁、不要表扬，只说问题。"
    ),
  },
  {
    key: "test_runner",
    label: "🧪 测试执行",
    agent_id: "test_runner",
    role: "测试执行员",
    goal: "跑测试、定位失败、给最小复现，不擅自改代码",
    backstory: "严格遵循流程，结果导向，只汇报事实",
    system_prompt: (
      "你是 XMclaw 的测试子 agent。任务流程：\n"
      + "1. bash 运行 pytest / npm test / 用户指定的命令\n"
      + "2. 失败时用 file_read 定位错误源，给最小复现\n"
      + "3. 不擅自修代码，只汇报结果 + 建议（除非主 agent 明说）"
    ),
  },
  {
    key: "doc_writer",
    label: "📝 文档撰写",
    agent_id: "doc_writer",
    role: "技术文档撰写",
    goal: "把代码功能写成清晰的中文文档/注释/API doc",
    backstory: "擅长把复杂逻辑讲给新人听，落盘前先给主 agent 确认",
    system_prompt: (
      "你是 XMclaw 的文档子 agent。当被派活：\n"
      + "1. file_read 看代码弄懂功能\n"
      + "2. 写清晰的中文 README / 注释 / API doc\n"
      + "3. 用 file_write 落盘前先把改动的全文返回给主 agent 确认"
    ),
  },
  {
    key: "researcher",
    label: "🌐 网络研究",
    agent_id: "researcher",
    role: "网络研究员",
    goal: "找最新资料、读核心源、给带链接的事实总结",
    backstory: "信息敏感，优先 30 天内资料，严格区分事实与观点",
    system_prompt: (
      "你是 XMclaw 的研究子 agent。主 agent 给你一个题目时：\n"
      + "1. web_search 找最新资料（优先 30 天内）\n"
      + "2. web_fetch 读 1-3 篇核心源\n"
      + "3. 总结时给链接，区分事实 vs 观点；2-3 段中文。"
    ),
  },
  {
    key: "debugger",
    label: "🐛 Bug 排查",
    agent_id: "debugger",
    role: "Bug 排查员",
    goal: "复现错误、顺 stack trace 找根因、给最小修复补丁",
    backstory: "顺藤摸瓜的侦探型，改动必讲清为什么这么改",
    system_prompt: (
      "你是 XMclaw 的 debug 子 agent。流程：\n"
      + "1. 收到错误 → bash 复现\n"
      + "2. file_read + grep 顺着 stack trace 找根因\n"
      + "3. 提出修复方案（写最小补丁），说清为什么这么改"
    ),
  },
  {
    key: "planner",
    label: "🗺 任务规划",
    agent_id: "planner",
    role: "任务规划师",
    goal: "把大任务拆成 3-7 个可验证的有序步骤",
    backstory: "条理清晰，每步都标明工具与完成判据，不说废话",
    system_prompt: (
      "你是 XMclaw 的规划子 agent。主 agent 抛过来一个大任务：\n"
      + "1. 拆成 3-7 个有序步骤\n"
      + "2. 每步写：要做什么、用什么工具、怎么验证完成\n"
      + "3. 直接返回 markdown 列表，不要废话"
    ),
  },
];

function _templateConfigJson(tpl) {
  return JSON.stringify({
    system_prompt: tpl.system_prompt,
    // LLM block intentionally omitted — workspace 会回退到主 agent
    // 的 daemon config，避免每个子 agent 都重复填一次 provider/model
  }, null, 2);
}

function CreateAgentForm({ token, onCreated }) {
  const [agentId, setAgentId] = useState("");
  const [role, setRole] = useState("");
  const [goal, setGoal] = useState("");
  const [backstory, setBackstory] = useState("");
  const [style, setStyle] = useState("");
  const [configText, setConfigText] = useState(
    '{\n  "llm": {\n    "provider": "anthropic",\n    "model": "claude-sonnet-4-6"\n  }\n}'
  );
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const applyTemplate = (tpl) => {
    setAgentId(tpl.agent_id);
    setRole(tpl.role || "");
    setGoal(tpl.goal || "");
    setBackstory(tpl.backstory || "");
    setStyle(tpl.style || "");
    setConfigText(_templateConfigJson(tpl));
  };

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
        // G5: 结构化人格随顶层传给后端，自动合成进 system_prompt
        role: role.trim(), goal: goal.trim(),
        backstory: backstory.trim(), style: style.trim(),
        config,
      });
      toast.success(`agent ${agentId} 已创建`);
      setAgentId(""); setRole(""); setGoal(""); setBackstory(""); setStyle("");
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
      <div style="margin-bottom:.5rem;font-size:.72rem">
        <small style="display:block;margin-bottom:.25rem;color:var(--xmc-fg-muted)">从模板快速开始 (B-134)：</small>
        <div style="display:flex;flex-wrap:wrap;gap:.3rem">
          ${PERSONA_TEMPLATES.map((tpl) => html`
            <button
              type="button"
              key=${tpl.key}
              class="xmc-h-btn xmc-h-btn--ghost"
              style="padding:.18rem .5rem;font-size:.72rem"
              onClick=${() => applyTemplate(tpl)}
              title=${tpl.system_prompt.slice(0, 80) + "..."}
            >${tpl.label}</button>
          `)}
        </div>
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
      <div style="margin-bottom:.4rem">
        <small style="display:block;margin-bottom:.25rem;color:var(--xmc-fg-muted);font-size:.72rem">
          🎭 结构化人格 (G5 — 编排时主持人/主管靠这些判断该谁上；会自动合成进 system_prompt)：
        </small>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.4rem">
          <input type="text" value=${role} onInput=${(e) => setRole(e.target.value)}
            placeholder="角色 role（如：研究员）"
            style="padding:.3rem;font-size:.76rem" />
          <input type="text" value=${style} onInput=${(e) => setStyle(e.target.value)}
            placeholder="风格 style（如：严谨）"
            style="padding:.3rem;font-size:.76rem" />
        </div>
        <input type="text" value=${goal} onInput=${(e) => setGoal(e.target.value)}
          placeholder="目标 goal（这个 agent 要达成什么）"
          style="display:block;width:100%;margin-top:.4rem;padding:.3rem;font-size:.76rem" />
        <input type="text" value=${backstory} onInput=${(e) => setBackstory(e.target.value)}
          placeholder="背景 backstory（一句话人物设定，可空）"
          style="display:block;width:100%;margin-top:.4rem;padding:.3rem;font-size:.76rem" />
      </div>
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
  const [busy, setBusy] = useState(false);

  const setAgentsFromResp = (d) => {
    const list = Array.isArray(d) ? d : (d && d.agents) || [];
    setAgents(list);
  };
  const { error, refresh: load } = useSafeFetch("/api/v2/agents", token, setAgentsFromResp);

  useEffect(() => {
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

  if (error) return html`<section class="xmc-datapage"><h2>智能体</h2><p class="xmc-datapage__error">${String(error.message || error)}</p></section>`;
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

      <${Vitals}>
        <${VitalsCell} icon=${html`<${Sparkbar} live=${agents.length > 0} />`}>
          <${Readout} label="已注册" value=${agents.length} unit="agents" />
        </${VitalsCell}>
        <${VitalsCell}><${Readout} label="对话型 LLM" value=${llmCount} unit="llm" /></${VitalsCell}>
        <${VitalsCell}><${Readout} label="观察者" value=${evoCount} unit="evolution" /></${VitalsCell}>
      </${Vitals}>

      <${CreateAgentForm} token=${token} onCreated=${load} />
      <ul class="xmc-datapage__list">
        ${agents.map((a) => html`<${AgentRow} a=${a} onDelete=${onDelete} busy=${busy} />`)}
      </ul>
    </section>
  `;
}
