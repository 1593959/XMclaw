// 认知域（XMclaw 的自主灵魂）— 当前目标 / 注意力焦点 / 自主任务 / 进化提议。
// 数据：/cognition/state + /cognition/tasks + /cognition/proposals。
// cognition.enabled=false 时端点回 503 {reason, how_to_enable}，渲染启用引导。

import { useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet, type ApiError } from "../lib/api";

interface Goal {
  id: string;
  description: string;
  priority?: number;
  source?: string;
  status?: string;
}
interface Focus {
  percept_id: string;
  content: string;
  salience_score: number;
}
interface CogState {
  goals?: Goal[];
  attention_focus?: Focus[];
  fatigue?: Record<string, number>;
  salience_threshold?: number;
  attention_capacity?: number;
}
interface AutoTask {
  id: string;
  prompt: string;
  status: string;
  priority?: number;
  error?: string | null;
  created_at?: number;
}
interface NotWired {
  reason?: string;
  hint?: string;
  how_to_enable?: string;
}

const STATUS_CLS: Record<string, string> = {
  running: "text-mc-accent",
  pending: "text-mc-faint",
  completed: "text-mc-ok",
  done: "text-mc-ok",
  failed: "text-mc-err",
  active: "text-mc-accent",
};

export default function CognitionView() {
  const token = useApp((s) => s.token);
  const [state, setState] = useState<CogState | null>(null);
  const [tasks, setTasks] = useState<AutoTask[]>([]);
  const [disabled, setDisabled] = useState<NotWired | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    apiGet<CogState>("/api/v2/cognition/state", token)
      .then((d) => {
        setState(d);
        setDisabled(null);
      })
      .catch((e: ApiError) => {
        if (e.status === 503) setDisabled((e.body as NotWired) || { reason: "disabled" });
        else setState(null);
      })
      .finally(() => setLoading(false));
    apiGet<{ tasks?: AutoTask[] }>("/api/v2/cognition/tasks", token)
      .then((d) => setTasks(d?.tasks || []))
      .catch(() => setTasks([]));
  }, [token]);

  if (loading) return <div className="p-5 text-xs text-mc-faint">加载中…</div>;

  if (disabled) {
    return (
      <div className="p-5">
        <h3 className="text-sm font-semibold mb-1">认知 / 自主</h3>
        <div className="border border-mc-warn/40 bg-mc-warn/5 rounded-lg px-4 py-3 text-[13px] text-mc-warn max-w-xl">
          <div className="font-medium mb-1">认知子系统未启用</div>
          <div className="text-mc-muted text-xs leading-relaxed">
            {disabled.hint || "agent 的自主目标、注意力与后台任务调度当前关闭。"}
            {disabled.how_to_enable && (
              <div className="mt-1.5 font-mono text-[11px] text-mc-faint">{disabled.how_to_enable}</div>
            )}
          </div>
        </div>
      </div>
    );
  }

  const goals = state?.goals || [];
  const focus = state?.attention_focus || [];
  const fatigue = Object.entries(state?.fatigue || {});

  return (
    <div className="p-5 space-y-5">
      <div>
        <h3 className="text-sm font-semibold">认知 / 自主</h3>
        <p className="text-xs text-mc-faint mt-0.5">agent 当下的目标、注意力与后台自主任务 — 这是它"自己在想什么"</p>
      </div>

      <div className="flex gap-3 flex-wrap">
        <Metric label="当前目标" value={goals.length} />
        <Metric label="注意力焦点" value={focus.length} />
        <Metric label="自主任务" value={tasks.length} />
        <Metric
          label="显著性阈值"
          value={state?.salience_threshold != null ? state.salience_threshold.toFixed(2) : "—"}
        />
        <Metric label="注意力容量" value={state?.attention_capacity ?? "—"} />
      </div>

      {goals.length > 0 && (
        <Section title="当前目标">
          {goals.map((g) => (
            <div key={g.id} className="border border-mc-border rounded-md px-3 py-2 bg-mc-panel2/40">
              <div className="flex items-baseline gap-2">
                <span className={"text-[11px] " + (STATUS_CLS[g.status || ""] || "text-mc-faint")}>
                  {g.status || "?"}
                </span>
                {g.priority != null && <span className="text-[10.5px] text-mc-faint">P{g.priority}</span>}
                {g.source && <span className="text-[10.5px] text-mc-faint">{g.source}</span>}
              </div>
              <div className="text-[13px] leading-relaxed mt-0.5">{g.description}</div>
            </div>
          ))}
        </Section>
      )}

      {focus.length > 0 && (
        <Section title="注意力焦点">
          {focus.map((f) => (
            <div key={f.percept_id} className="flex items-center gap-2 text-[12.5px] py-1">
              <span className="w-10 text-[10.5px] text-mc-accent tabular-nums shrink-0">
                {Math.round(f.salience_score * 100)}%
              </span>
              <span className="truncate">{f.content}</span>
            </div>
          ))}
        </Section>
      )}

      {tasks.length > 0 && (
        <Section title="自主任务">
          {tasks.map((t) => (
            <div key={t.id} className="border border-mc-border rounded-md px-3 py-2 bg-mc-panel2/40">
              <div className="flex items-baseline gap-2">
                <span className={"text-[11px] " + (STATUS_CLS[t.status] || "text-mc-faint")}>{t.status}</span>
                {t.priority != null && <span className="text-[10.5px] text-mc-faint">P{t.priority}</span>}
              </div>
              <div className="text-[12.5px] leading-relaxed mt-0.5 line-clamp-2">{t.prompt}</div>
              {t.error && <div className="text-[11px] text-mc-err mt-0.5">{t.error}</div>}
            </div>
          ))}
        </Section>
      )}

      {fatigue.length > 0 && (
        <Section title="疲劳度">
          <div className="flex gap-2 flex-wrap">
            {fatigue.map(([k, v]) => (
              <span key={k} className="text-[11px] px-2 py-0.5 rounded-full border border-mc-border text-mc-muted">
                {k} {v}
              </span>
            ))}
          </div>
        </Section>
      )}

      {goals.length === 0 && focus.length === 0 && tasks.length === 0 && (
        <div className="text-xs text-mc-faint">agent 当前没有活跃目标或自主任务 — 空闲中</div>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md bg-mc-panel2 px-4 py-3 min-w-24">
      <div className="text-[11px] text-mc-faint">{label}</div>
      <div className="text-xl font-semibold mt-0.5">{value}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="text-[13px] font-medium mb-1.5">{title}</h4>
      <div className="space-y-1">{children}</div>
    </div>
  );
}
