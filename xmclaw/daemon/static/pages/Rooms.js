// XMclaw — Rooms 页（多 agent 群聊 / 工作流房间）  Group G3 (2026-06-06)
//
// 后端 /api/v2/rooms：建/列/取/改/删 + POST /{id}/run（同步跑）。
//   - chat 模式 → 返回 {speakers, transcript:[{speaker,text}]}
//   - workflow 模式 → 返回 {ok, result, assignments, completed, failed}
// 本页：房间列表 + 建房 + 进入房间后发消息触发 run、渲染多讲者/工作流结果。
// 套仪表台形态 + 复用 Vitals。WS 流式（"看着跑"）留作后续，先同步可用。

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost } from "../lib/api.js";
import { buildWsUrl } from "../lib/ws.js";
import { toast } from "../lib/toast.js";
import { confirmDialog } from "../lib/dialog.js";
import { Vitals, VitalsCell, Readout, Sparkbar } from "../components/molecules/Instrument.js";

// 给每个讲者一个稳定配色（按 id 哈希取色环）。
const _SPEAKER_COLORS = ["#8B5CF6", "#06B6D4", "#34D399", "#F59E0B", "#F87171", "#A78BFA", "#67E8F9"];
function speakerColor(id) {
  if (id === "user" || id === "用户") return "var(--nb-accent, #8B5CF6)";
  let s = 0;
  for (let i = 0; i < (id || "").length; i++) s = (s * 31 + id.charCodeAt(i)) >>> 0;
  return _SPEAKER_COLORS[s % _SPEAKER_COLORS.length];
}

function CreateRoomForm({ token, agents, onCreated }) {
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [strategy, setStrategy] = useState("chat");
  const [picked, setPicked] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const toggle = (id) => setPicked((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  // 4 种编排策略（后端 RoomOrchestrator）
  const STRATS = [
    { id: "chat", label: "群聊", hint: "共享历史 + 主持人 LLM 选讲者（AutoGen）" },
    { id: "sequential", label: "固定流水线", hint: "A→B→C 顺序接力（你预先排序）" },
    { id: "supervisor", label: "主管派活", hint: "主管 LLM 按角色动态分派（CrewAI 分层）" },
    { id: "autonomous", label: "目标驱动·自主", hint: "任务/进度台账 + 卡住重规划（Magentic-One）" },
  ];
  const submit = async () => {
    if (!name.trim()) { toast.error("给房间起个名"); return; }
    if (picked.size === 0) { toast.error("至少选一个参与者 agent"); return; }
    setBusy(true);
    try {
      const r = await apiPost("/api/v2/rooms", {
        name: name.trim(), purpose: purpose.trim(),
        participants: [...picked], strategy,
        // mode 仅用于旧前端渲染分支：chat 走对话视图，其余走结果视图。
        mode: strategy === "chat" ? "chat" : "workflow",
      }, token);
      if (r.ok) { toast.success("房间已建"); setName(""); setPurpose(""); setPicked(new Set()); onCreated && onCreated(); }
      else toast.error(r.error || "建房失败");
    } catch (e) { toast.error(String(e.message || e)); }
    finally { setBusy(false); }
  };
  return html`
    <details class="xmc-mem-maint" style="margin:8px 0">
      <summary class="xmc-mem-maint__summary">＋ 新建房间<span class="xmc-mem-maint__hint">多 agent 群聊 / 工作流</span></summary>
      <div style="display:flex;flex-direction:column;gap:8px;padding:10px 2px">
        <input class="xmc-h-input" placeholder="房间名（如：竞品分析组）" value=${name} onInput=${(e) => setName(e.target.value)} />
        <textarea class="xmc-h-input" rows="2" placeholder="用途 / 目标（非群聊策略会作为编排目标拆解执行）" value=${purpose} onInput=${(e) => setPurpose(e.target.value)}></textarea>
        <div>
          <div class="xi-readout__label" style="margin-bottom:5px">编排策略</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            ${STRATS.map((s) => html`
              <button type="button" key=${s.id} title=${s.hint}
                class=${"xmc-mem-seg" + (strategy === s.id ? " is-active" : "")}
                onClick=${() => setStrategy(s.id)}>${s.label}</button>
            `)}
          </div>
          <div style="opacity:.6;font-size:.72rem;margin-top:4px">${(STRATS.find((s) => s.id === strategy) || {}).hint}</div>
        </div>
        <div>
          <div class="xi-readout__label" style="margin-bottom:5px">参与者（点选）</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            ${(agents || []).map((a) => html`
              <button type="button" key=${a.agent_id}
                class=${"xmc-mem-seg" + (picked.has(a.agent_id) ? " is-active" : "")}
                onClick=${() => toggle(a.agent_id)}>${a.agent_id}</button>
            `)}
            ${(agents || []).length === 0 ? html`<span style="opacity:.6;font-size:.8rem">没有 agent — 先去「代理」页建几个</span>` : null}
          </div>
        </div>
        <button type="button" class="xmc-h-btn xmc-h-btn--primary" disabled=${busy} onClick=${submit} style="align-self:flex-start">${busy ? "建中…" : "创建房间"}</button>
      </div>
    </details>
  `;
}

function RoomRunPanel({ token, room }) {
  const [msg, setMsg] = useState("");
  const [running, setRunning] = useState(false);
  const [chatRows, setChatRows] = useState([]);       // [{speaker,text}]
  const [wf, setWf] = useState(null);                 // workflow result
  const [live, setLive] = useState([]);               // 实时活动流 [{agent,label,kind}]
  // 实时活动：运行时开 WS 订阅房间 session，按 agent_id 显示谁在思考/调工具/发言。
  // 事件本就发到 group:<room_id>（每个 agent 的 run_turn 用房间 session_id）。
  const openLiveWs = () => {
    try {
      const ws = new WebSocket(buildWsUrl("group:" + room.room_id, token));
      ws.onmessage = (ev) => {
        let f; try { f = JSON.parse(ev.data); } catch (_) { return; }
        if (f.replayed) return;            // 历史回放不算实时
        const a = f.agent_id || "?";
        const t = f.type;
        let label = null, kind = "info";
        if (t === "llm_request") { label = "正在思考…"; kind = "think"; }
        else if (t === "tool_call_emitted") { label = "调用 " + ((f.payload && (f.payload.name || f.payload.tool_name)) || "工具"); kind = "tool"; }
        else if (t === "tool_invocation_finished") { label = "工具完成"; kind = "tool"; }
        else if (t === "llm_response") { label = "已发言"; kind = "done"; }
        if (label) setLive((prev) => [...prev.slice(-40), { agent: a, label, kind, ts: Date.now() }]);
      };
      return ws;
    } catch (_) { return null; }
  };
  const run = async () => {
    setRunning(true); setWf(null); setLive([]);
    const ws = openLiveWs();
    try {
      const out = await apiPost(`/api/v2/rooms/${encodeURIComponent(room.room_id)}/run`, { message: msg.trim() }, token);
      if (room.mode === "workflow") setWf(out);
      else setChatRows(out.transcript || []);
      setMsg("");
    } catch (e) { toast.error("运行失败：" + (e.message || e)); }
    finally {
      setRunning(false);
      try { ws && ws.close(); } catch (_) {}
    }
  };
  return html`
    <div class="xi-panel" style="padding:12px;margin-top:10px">
      <div class="xi-seclabel" style="margin-bottom:8px">${room.mode === "workflow" ? "工作流运行" : "群聊"} · ${room.room_id}</div>
      ${(running || live.length > 0) ? html`
        <div style="margin-bottom:10px;border:1px solid var(--nb-border);border-radius:7px;padding:7px 10px;background:color-mix(in srgb,var(--nb-cyan,#06B6D4) 6%,transparent)">
          <div class="xi-seclabel" style="margin-bottom:5px">实时活动 ${running ? "· 运行中…" : "· 已结束"}</div>
          <div style="display:flex;flex-direction:column;gap:3px;max-height:140px;overflow:auto;font-family:var(--nb-font-mono);font-size:11.5px">
            ${live.length === 0 ? html`<span style="opacity:.6">等待 agent 响应…</span>` : null}
            ${live.slice(-14).map((e, i) => html`
              <div key=${i} style="display:flex;gap:6px;align-items:center">
                <span style="width:6px;height:6px;border-radius:50%;background:${speakerColor(e.agent)};flex:0 0 auto"></span>
                <span style="color:${speakerColor(e.agent)}">${e.agent}</span>
                <span style="opacity:.8">${e.kind === "think" ? "💭" : e.kind === "tool" ? "🔧" : e.kind === "done" ? "✓" : "·"} ${e.label}</span>
              </div>`)}
          </div>
        </div>` : null}
      ${room.mode === "workflow"
        ? html`
          ${wf ? html`
            <div style="display:flex;flex-direction:column;gap:8px">
              <div class="nb-recall-memo" style="--rc:var(--nb-success,#34D399)">
                <div class="nb-recall-memo__head" style="cursor:default">
                  <span class="nb-recall-memo__spark">✓</span>
                  <span class="nb-recall-memo__title">工作流${wf.ok ? "完成" : "未完成"}</span>
                  <span class="nb-recall-memo__q">完成 ${wf.completed || 0} · 失败 ${wf.failed || 0}</span>
                </div>
              </div>
              ${wf.assignments && Object.keys(wf.assignments).length ? html`
                <div>
                  <div class="xi-seclabel">任务分派</div>
                  ${Object.entries(wf.assignments).map(([t, a]) => html`
                    <div style="font-family:var(--nb-font-mono);font-size:12px;padding:2px 0">
                      <span style="color:${speakerColor(a)}">●</span> ${a} ← <code>${t}</code>
                    </div>`)}
                </div>` : null}
              <div>
                <div class="xi-seclabel">最终结果</div>
                <div class="nb-md" style="white-space:pre-wrap">${wf.result || "(空)"}</div>
              </div>
            </div>
          ` : html`<div style="opacity:.6;font-size:.85rem">输入补充说明（可空）后点「运行工作流」，编排器会按房间目标拆解、分派、聚合。</div>`}
        `
        : html`
          <div style="display:flex;flex-direction:column;gap:8px;max-height:360px;overflow:auto">
            ${chatRows.length === 0 ? html`<div style="opacity:.6;font-size:.85rem">发条消息开始群聊，agent 会轮流/由主持人挑选发言。</div>` : null}
            ${chatRows.map((r, i) => html`
              <div key=${i} style="display:flex;gap:8px;align-items:flex-start">
                <span style="flex:0 0 auto;width:8px;height:8px;border-radius:50%;margin-top:6px;background:${speakerColor(r.speaker)};box-shadow:0 0 6px ${speakerColor(r.speaker)}"></span>
                <div style="min-width:0">
                  <div style="font-family:var(--nb-font-mono);font-size:11px;color:${speakerColor(r.speaker)}">${r.speaker === "user" ? "你" : r.speaker}</div>
                  <div class="nb-md" style="font-size:13.5px;white-space:pre-wrap">${r.text}</div>
                </div>
              </div>`)}
          </div>
        `}
      <div style="display:flex;gap:8px;margin-top:10px">
        <input class="xmc-h-input" style="flex:1" placeholder=${room.mode === "workflow" ? "补充说明（可空）" : "对房间说点什么…"}
          value=${msg} onInput=${(e) => setMsg(e.target.value)}
          onKeyDown=${(e) => { if (e.key === "Enter" && !running) run(); }} />
        <button type="button" class="xmc-h-btn xmc-h-btn--primary" disabled=${running} onClick=${run}>
          ${running ? "跑中…" : (room.mode === "workflow" ? "运行工作流" : "发送")}
        </button>
      </div>
    </div>
  `;
}

export function RoomsPage({ token }) {
  const [rooms, setRooms] = useState(null);
  const [agents, setAgents] = useState([]);
  const [openId, setOpenId] = useState(null);

  const load = useCallback(async () => {
    try {
      const r = await apiGet("/api/v2/rooms", token);
      setRooms(Array.isArray(r?.rooms) ? r.rooms : []);
    } catch (_) { setRooms([]); }
    try {
      const a = await apiGet("/api/v2/agents", token);
      const list = Array.isArray(a) ? a : (a?.agents || []);
      // 排除 main 之外都可入房；main 也可参与。
      setAgents(list.map((x) => ({ agent_id: x.agent_id || x.id })).filter((x) => x.agent_id));
    } catch (_) { setAgents([]); }
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const del = async (id) => {
    if (!(await confirmDialog({ title: "删除房间", body: `删除房间 ${id}？`, confirmLabel: "删除", confirmTone: "danger" }))) return;
    try {
      await fetch(
        `/api/v2/rooms/${encodeURIComponent(id)}` + (token ? `?token=${encodeURIComponent(token)}` : ""),
        { method: "DELETE" },
      );
    } catch (e) { toast.error("删除失败：" + (e.message || e)); }
    if (openId === id) setOpenId(null);
    load();
  };

  return html`
    <section class="xmc-datapage" aria-labelledby="rooms-title">
      <header class="xmc-datapage__header">
        <h2 id="rooms-title">群聊 / 工作流房间</h2>
        <p class="xmc-datapage__subtitle">多 agent 协作：工作流编排(目标→拆解→分派→聚合) 或 群聊(轮流/主持人)</p>
      </header>

      <${Vitals}>
        <${VitalsCell} icon=${html`<${Sparkbar} live=${(rooms || []).length > 0} />`}>
          <${Readout} label="房间" value=${(rooms || []).length} unit="rooms" />
        </${VitalsCell}>
        <${VitalsCell}><${Readout} label="可用 AGENT" value=${agents.length} unit="agents" /></${VitalsCell}>
      </${Vitals}>

      <${CreateRoomForm} token=${token} agents=${agents} onCreated=${load} />

      ${rooms === null
        ? html`<div style="opacity:.6;padding:1rem">载入中…</div>`
        : rooms.length === 0
          ? html`<div class="xmc-h-empty">还没有房间。上面「新建房间」拉几个 agent 进来开工。</div>`
          : html`
            <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
              ${rooms.map((room) => html`
                <div class="xi-panel" style="padding:11px" key=${room.room_id}>
                  <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
                    <span class=${"xmc-fact__chip"} data-c="kind">${room.mode === "workflow" ? "工作流" : "群聊"}</span>
                    <strong style="font-size:1rem">${room.name || room.room_id}</strong>
                    <span style="flex:1"></span>
                    <button type="button" class="xmc-h-btn" onClick=${() => setOpenId(openId === room.room_id ? null : room.room_id)}>${openId === room.room_id ? "收起" : "进入"}</button>
                    <button type="button" class="xmc-fact__act xmc-fact__act--danger" title="删除" onClick=${() => del(room.room_id)}>🗑</button>
                  </div>
                  ${room.purpose ? html`<div style="font-size:.82rem;color:var(--nb-fg-secondary);margin-top:4px">${room.purpose}</div>` : null}
                  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">
                    ${(room.participants || []).map((p) => html`<span key=${p} style="font-family:var(--nb-font-mono);font-size:11px;color:${speakerColor(p)}">● ${p}</span>`)}
                  </div>
                  ${openId === room.room_id ? html`<${RoomRunPanel} token=${token} room=${room} />` : null}
                </div>`)}
            </div>
          `}
    </section>
  `;
}
