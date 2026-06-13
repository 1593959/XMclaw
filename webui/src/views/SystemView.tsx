// 系统域（10.M3 收编旧 Settings/Logs/Dashboard 摘要）— 健康读数 +
// 日志尾巴。深度配置仍走 /ui-legacy/ 的旧 Settings（M3 过渡期），这里是驾驶舱仪表。

import { lazy, Suspense, useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet, type ApiError } from "../lib/api";

const ModelConfig = lazy(() => import("./ModelConfig"));
const CronView = lazy(() => import("./CronView"));

type SystemTabId = "status" | "models" | "cron";

interface Health {
  status?: string;
  checks?: Record<string, string>;
  [k: string]: unknown;
}

export default function SystemView() {
  const token = useApp((s) => s.token);
  const hud = useApp((s) => s.hud);
  const [health, setHealth] = useState<Health | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [tab, setTab] = useState<SystemTabId>("status");

  useEffect(() => {
    if (!token) return;
    apiGet<Health>("/api/v2/system/health", token)
      .then(setHealth)
      // degraded 时端点回 503 但 body 仍带 {status, checks} — 捞出来渲染。
      .catch((err: ApiError) => setHealth((err?.body as Health) || null));
    apiGet<{ lines?: string[]; logs?: string[] }>("/api/v2/logs?limit=120", token)
      .then((d) => setLogs(d?.lines || d?.logs || []))
      .catch(() => setLogs([]));
  }, [token]);

  if (tab === "models" || tab === "cron") {
    return (
      <div className="flex-1 flex flex-col min-h-0">
        <div className="flex gap-1.5 px-5 pt-4 shrink-0">
          <SystemTab id="status" cur={tab} onPick={setTab}>健康 / 日志</SystemTab>
          <SystemTab id="models" cur={tab} onPick={setTab}>模型管理</SystemTab>
          <SystemTab id="cron" cur={tab} onPick={setTab}>定时任务</SystemTab>
        </div>
        <div className="flex-1 overflow-y-auto min-h-0">
          <Suspense fallback={<div className="p-5 text-mc-faint text-sm">加载中…</div>}>
            {tab === "models" ? <ModelConfig /> : <CronView />}
          </Suspense>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-4 min-h-0 flex flex-col">
      <div className="flex gap-1.5 shrink-0">
        <SystemTab id="status" cur={tab} onPick={setTab}>健康 / 日志</SystemTab>
        <SystemTab id="models" cur={tab} onPick={setTab}>模型管理</SystemTab>
        <SystemTab id="cron" cur={tab} onPick={setTab}>定时任务</SystemTab>
      </div>
      <div className="shrink-0">
        <h2 className="text-base font-semibold">系统</h2>
        <p className="text-xs text-mc-faint mt-0.5">
          daemon 健康与日志 — 深度配置暂在
          <a href="/ui-legacy/settings" target="_blank" rel="noreferrer" className="text-mc-accent underline mx-1">
            旧 Settings
          </a>
          （M3 过渡期）
        </p>
      </div>

      <div className="flex gap-3 flex-wrap shrink-0">
        <div className="rounded-md bg-mc-panel2 px-4 py-3">
          <div className="text-[11px] text-mc-faint">daemon</div>
          <div
            className={
              "text-xl font-semibold mt-0.5 " + (health?.status === "ok" ? "text-mc-ok" : "text-mc-err")
            }
          >
            {health == null ? "—" : health.status === "ok" ? "健康" : "降级"}
          </div>
        </div>
        {Object.entries(health?.checks || {})
          .filter(([k]) => k !== "status")
          .map(([k, v]) => {
            const good = String(v).startsWith("ok");
            return (
              <div key={k} className="rounded-md bg-mc-panel2 px-4 py-3">
                <div className="text-[11px] text-mc-faint">{k}</div>
                <div className={"text-xl font-semibold mt-0.5 " + (good ? "text-mc-ok" : "text-mc-warn")}>
                  {String(v)}
                </div>
              </div>
            );
          })}
        {hud?.model != null && (
          <div className="rounded-md bg-mc-panel2 px-4 py-3">
            <div className="text-[11px] text-mc-faint">默认模型</div>
            <div className="text-xl font-semibold mt-0.5">{String(hud.model)}</div>
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 flex flex-col">
        <h3 className="text-[13px] font-medium mb-1.5 shrink-0">日志尾巴</h3>
        <pre className="flex-1 min-h-48 overflow-y-auto text-[11px] font-mono text-mc-muted bg-black/30 border border-mc-border rounded-md p-3 whitespace-pre-wrap break-all">
          {logs.length ? logs.join("\n") : "（无日志或端点不可用）"}
        </pre>
      </div>
    </div>
  );
}

function SystemTab({
  id,
  cur,
  onPick,
  children,
}: {
  id: SystemTabId;
  cur: string;
  onPick: (v: SystemTabId) => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={() => onPick(id)}
      className={
        "text-xs px-3 py-1.5 rounded-md border cursor-pointer " +
        (cur === id
          ? "border-mc-accent/50 text-mc-accent bg-mc-accent/10"
          : "border-mc-border text-mc-faint hover:text-mc-muted")
      }
    >
      {children}
    </button>
  );
}
