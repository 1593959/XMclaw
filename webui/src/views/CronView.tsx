// 定时任务（Cron）— 列出/创建/暂停/恢复/立即触发/删除。
// 后端：GET/POST /api/v2/cron + POST /{id}/{pause|resume|trigger} + DELETE /{id}。

import { useCallback, useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiDelete, apiGetFresh, apiPost } from "../lib/api";

interface CronJob {
  id: string;
  name: string;
  schedule: string;
  prompt: string;
  agent_id?: string;
  enabled: boolean;
  next_run_at?: number;
  last_run_at?: number | null;
  last_error?: string | null;
  run_count?: number;
  run_once?: boolean;
}

function relTime(ts?: number | null): string {
  if (!ts) return "—";
  const d = ts - Date.now() / 1000;
  const abs = Math.abs(d);
  const unit = abs < 60 ? `${Math.round(abs)} 秒` : abs < 3600 ? `${Math.round(abs / 60)} 分钟` : abs < 86400 ? `${Math.round(abs / 3600)} 小时` : `${Math.round(abs / 86400)} 天`;
  return d >= 0 ? `${unit}后` : `${unit}前`;
}

export default function CronView() {
  const token = useApp((s) => s.token);
  const showToast = useApp((s) => s.showToast);
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("every 1h");
  const [prompt, setPrompt] = useState("");

  const load = useCallback(() => {
    if (!token) return;
    const ctl = new AbortController();
    setLoading(true);
    apiGetFresh<{ jobs?: CronJob[] }>("/api/v2/cron", token, ctl.signal)
      .then((d) => setJobs(d?.jobs || []))
      .catch(() => {
        if (!ctl.signal.aborted) setJobs([]);
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false);
      });
    return () => ctl.abort();
  }, [token]);

  useEffect(load, [load]);

  async function act(id: string, action: "pause" | "resume" | "trigger") {
    try {
      await apiPost(`/api/v2/cron/${encodeURIComponent(id)}/${action}`, {}, token);
      showToast(action === "trigger" ? "已触发执行" : action === "pause" ? "已暂停" : "已恢复", "ok");
      if (action !== "trigger") load();
    } catch {
      showToast("操作失败", "err");
    }
  }

  async function remove(id: string) {
    setJobs((js) => js.filter((j) => j.id !== id));
    try {
      await apiDelete(`/api/v2/cron/${encodeURIComponent(id)}`, token);
      showToast("已删除", "ok");
    } catch {
      showToast("删除失败", "err");
      load();
    }
  }

  async function create() {
    if (!name.trim() || !schedule.trim() || !prompt.trim()) {
      showToast("名称 / 周期 / 指令均必填", "err");
      return;
    }
    try {
      const r = await apiPost<{ ok?: boolean; error?: string }>(
        "/api/v2/cron",
        { name: name.trim(), schedule: schedule.trim(), prompt: prompt.trim() },
        token,
      );
      if (r.error) {
        showToast(r.error, "err");
        return;
      }
      showToast("已创建定时任务", "ok");
      setName("");
      setPrompt("");
      setSchedule("every 1h");
      setCreating(false);
      load();
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  return (
    <div className="p-5 space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold">定时任务</h3>
          <p className="text-xs text-mc-faint mt-0.5">按周期自动唤醒 agent 执行指令（如每天整理、定时巡检）</p>
        </div>
        <button
          onClick={() => setCreating((v) => !v)}
          className="shrink-0 px-3 py-1.5 rounded-md border border-mc-border text-sm hover:border-mc-accent/50 cursor-pointer"
        >
          {creating ? "取消" : "＋ 新建"}
        </button>
      </div>

      {creating && (
        <div className="border border-mc-border rounded-lg p-3 space-y-2 bg-mc-panel2/40">
          <div className="flex gap-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="任务名称"
              className="flex-1 text-sm px-3 py-1.5 rounded-md border border-mc-border bg-mc-panel2 outline-none focus:border-mc-accent placeholder:text-mc-faint"
            />
            <input
              value={schedule}
              onChange={(e) => setSchedule(e.target.value)}
              placeholder="周期，如 every 1h / every 30m / 0 9 * * *"
              className="flex-1 text-sm px-3 py-1.5 rounded-md border border-mc-border bg-mc-panel2 outline-none focus:border-mc-accent placeholder:text-mc-faint font-mono"
            />
          </div>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="到点执行的指令，例如：整理桌面下载目录并汇报"
            rows={2}
            className="w-full text-sm px-3 py-1.5 rounded-md border border-mc-border bg-mc-panel2 outline-none focus:border-mc-accent resize-none placeholder:text-mc-faint"
          />
          <button
            onClick={create}
            className="px-3.5 py-1.5 rounded-md bg-mc-accent text-white text-sm font-medium hover:bg-mc-accent-dim cursor-pointer"
          >
            创建
          </button>
        </div>
      )}

      {loading ? (
        <div className="text-xs text-mc-faint">加载中…</div>
      ) : jobs.length === 0 ? (
        <div className="text-center text-sm text-mc-faint py-8 border border-dashed border-mc-border rounded-lg">
          还没有定时任务 — 点「新建」添加
        </div>
      ) : (
        <div className="space-y-1.5">
          {jobs.map((j) => (
            <div
              key={j.id}
              className={"border border-mc-border rounded-md px-3 py-2.5 bg-mc-panel2/40 group " + (j.enabled ? "" : "opacity-60")}
            >
              <div className="flex items-center gap-2">
                <span className="text-[13px] font-medium truncate">{j.name || j.id}</span>
                <span className="font-mono text-[11px] text-mc-accent">{j.schedule}</span>
                {!j.enabled && <span className="text-[10.5px] text-mc-warn">已暂停</span>}
                {j.run_once && <span className="text-[10.5px] text-mc-faint">单次</span>}
                <div className="flex-1" />
                <div className="flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity text-[11px]">
                  <button onClick={() => act(j.id, "trigger")} className="text-mc-faint hover:text-mc-accent cursor-pointer">
                    立即运行
                  </button>
                  <button
                    onClick={() => act(j.id, j.enabled ? "pause" : "resume")}
                    className="text-mc-faint hover:text-mc-warn cursor-pointer"
                  >
                    {j.enabled ? "暂停" : "恢复"}
                  </button>
                  <button onClick={() => remove(j.id)} className="text-mc-faint hover:text-mc-err cursor-pointer">
                    删除
                  </button>
                </div>
              </div>
              {j.prompt && <div className="text-[12px] text-mc-muted mt-1 line-clamp-2">{j.prompt}</div>}
              <div className="flex gap-3 mt-1 text-[10.5px] text-mc-faint">
                <span>下次 {relTime(j.next_run_at)}</span>
                <span>已运行 {j.run_count ?? 0} 次</span>
                {j.last_error && <span className="text-mc-err truncate">错误: {j.last_error}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
