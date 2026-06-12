// 能力域（10.M3 收编旧 Skills/Evolution 页核心）— 技能清单 +
// 进化管线读数（observer arms 晋升进度）。

import { useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet, apiPost } from "../lib/api";

interface SkillVersion {
  version: number;
  is_head: boolean;
  manifest?: { description?: string; [k: string]: unknown };
}
interface Skill {
  id: string;
  head_version: number;
  source?: string;
  versions: SkillVersion[];
}
interface Arm {
  skill_id: string;
  version: number;
  plays: number;
  mean_score: number;
  progress?: {
    plays_progress?: number;
    mean_progress?: number;
    ready_to_propose?: boolean;
  };
}
interface Snapshot {
  observer?: { is_running?: boolean; arms?: Arm[]; ready_to_propose_count?: number };
  trigger?: { is_active?: boolean };
  [k: string]: unknown;
}

export default function SkillsView() {
  const token = useApp((s) => s.token);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [pendingRestarts, setPendingRestarts] = useState<unknown[]>([]);
  const [loadFailures, setLoadFailures] = useState<Array<Record<string, unknown>>>([]);
  const [snap, setSnap] = useState<Snapshot | null>(null);

  useEffect(() => {
    if (!token) return;
    apiGet<{ skills?: Skill[]; pending_restarts?: unknown[]; load_failures?: Array<Record<string, unknown>> }>(
      "/api/v2/skills",
      token,
    )
      .then((d) => {
        setSkills(d?.skills || []);
        setPendingRestarts(d?.pending_restarts || []);
        setLoadFailures(d?.load_failures || []);
      })
      .catch(() => setSkills([]));
    apiGet<Snapshot>("/api/v2/evolution/snapshot", token).then(setSnap).catch(() => setSnap(null));
  }, [token]);

  const arms = snap?.observer?.arms || [];

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-4">
      <div>
        <h2 className="text-base font-semibold">能力</h2>
        <p className="text-xs text-mc-faint mt-0.5">技能库与自进化管线 — Honest Grader 驱动晋升</p>
      </div>

      {loadFailures.length > 0 && (
        <div className="border border-mc-err/40 bg-mc-err/5 rounded-md px-3 py-2 text-[12px] text-mc-err">
          {loadFailures.length} 个技能加载失败：
          {loadFailures.map((f, i) => (
            <span key={i} className="font-mono ml-1">{String(f.skill_id || f.path || "?")}</span>
          ))}
        </div>
      )}
      {pendingRestarts.length > 0 && (
        <div className="border border-mc-warn/40 bg-mc-warn/5 rounded-md px-3 py-2 text-[12px] text-mc-warn">
          {pendingRestarts.length} 个技能改动需要重启 daemon 生效
        </div>
      )}

      <div className="flex gap-3 flex-wrap">
        <div className="rounded-md bg-mc-panel2 px-4 py-3">
          <div className="text-[11px] text-mc-faint">技能数</div>
          <div className="text-xl font-semibold mt-0.5">{skills.length}</div>
        </div>
        <div className="rounded-md bg-mc-panel2 px-4 py-3">
          <div className="text-[11px] text-mc-faint">进化观察中</div>
          <div className="text-xl font-semibold mt-0.5">
            {snap?.observer?.is_running ? `${arms.length} arm` : "停"}
          </div>
        </div>
        <div className="rounded-md bg-mc-panel2 px-4 py-3">
          <div className="text-[11px] text-mc-faint">待晋升</div>
          <div className={"text-xl font-semibold mt-0.5 " + ((snap?.observer?.ready_to_propose_count || 0) > 0 ? "text-mc-ok" : "")}>
            {snap?.observer?.ready_to_propose_count ?? "—"}
          </div>
        </div>
      </div>

      {arms.length > 0 && (
        <div>
          <h3 className="text-[13px] font-medium mb-1.5">晋升进度</h3>
          <div className="space-y-1.5">
            {arms.map((a) => {
              const p = Math.round(
                Math.min(a.progress?.plays_progress ?? 0, a.progress?.mean_progress ?? 0) * 100,
              );
              return (
                <div key={`${a.skill_id}@${a.version}`} className="border border-mc-border rounded-md px-3 py-2">
                  <div className="flex items-center gap-2 text-[12.5px]">
                    <span className="font-mono">{a.skill_id}</span>
                    <span className="text-mc-faint">v{a.version}</span>
                    <span className="text-mc-faint">×{a.plays} 次 · 均分 {a.mean_score?.toFixed(2)}</span>
                    {a.progress?.ready_to_propose && <span className="text-mc-ok text-[11px]">可晋升</span>}
                    <div className="flex-1" />
                    <span className="text-[11px] text-mc-muted">{p}%</span>
                  </div>
                  <div className="h-1 rounded-full bg-mc-border mt-1.5">
                    <div className="h-1 rounded-full bg-mc-accent" style={{ width: `${p}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div>
        <h3 className="text-[13px] font-medium mb-1.5">技能清单</h3>
        <div className="space-y-1">
          {skills.map((s) => (
            <SkillRow key={s.id} skill={s} token={token} />
          ))}
          {skills.length === 0 && <div className="text-xs text-mc-faint">暂无注册技能</div>}
        </div>
      </div>
    </div>
  );
}

interface HistoryRecord {
  action?: string;
  from_version?: number;
  to_version?: number;
  reason?: string;
  ts?: number;
}

function SkillRow({ skill: s, token }: { skill: Skill; token: string | null }) {
  const showToast = useApp((s) => s.showToast);
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState<HistoryRecord[] | null>(null);
  const [busy, setBusy] = useState(false);
  const head = s.versions?.find((v) => v.is_head);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && history === null && token) {
      apiGet<{ records?: HistoryRecord[] }>(`/api/v2/skills/${encodeURIComponent(s.id)}/history`, token)
        .then((d) => setHistory(d?.records || []))
        .catch(() => setHistory([]));
    }
  }

  async function rollback(toVersion: number) {
    if (!token || busy) return;
    setBusy(true);
    try {
      const r = await apiPost<{ ok?: boolean; error?: string }>(
        `/api/v2/skills/${encodeURIComponent(s.id)}/rollback`,
        { to_version: toVersion, reason: "用户从 UI 手动回滚" },
        token,
      );
      if (r.error) showToast(r.error, "err");
      else showToast(`已回滚 ${s.id} 到 v${toVersion}`, "ok");
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border border-mc-border rounded-md bg-mc-panel2/40">
      <button onClick={toggle} className="w-full text-left px-3 py-2 flex items-baseline gap-2 cursor-pointer">
        <span className="text-mc-faint text-[10px]">{open ? "▾" : "▸"}</span>
        <span className="font-mono text-[12.5px]">{s.id}</span>
        <span className="text-[11px] text-mc-faint">v{s.head_version}</span>
        {s.source && <span className="text-[11px] text-mc-faint">{s.source}</span>}
        <span className="text-[11.5px] text-mc-muted truncate flex-1">
          {String(head?.manifest?.description || "")}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-2.5 pt-0.5 space-y-2 border-t border-mc-border/50">
          <div className="text-[11px] text-mc-faint mt-2">版本（点击回滚到非当前版本）</div>
          <div className="flex gap-1.5 flex-wrap">
            {(s.versions || [])
              .slice()
              .sort((a, b) => b.version - a.version)
              .map((v) => (
                <button
                  key={v.version}
                  disabled={v.is_head || busy}
                  onClick={() => rollback(v.version)}
                  className={
                    "text-[11px] px-2 py-0.5 rounded border cursor-pointer disabled:cursor-default " +
                    (v.is_head
                      ? "border-mc-accent/50 text-mc-accent bg-mc-accent/10"
                      : "border-mc-border text-mc-muted hover:border-mc-accent/40 hover:text-mc-accent")
                  }
                  title={v.is_head ? "当前版本" : `回滚到 v${v.version}`}
                >
                  v{v.version}
                  {v.is_head ? " · 当前" : ""}
                </button>
              ))}
          </div>
          {history && history.length > 0 && (
            <div className="space-y-0.5">
              <div className="text-[11px] text-mc-faint">晋升 / 回滚记录</div>
              {history.slice(-6).reverse().map((h, i) => (
                <div key={i} className="text-[11px] text-mc-muted">
                  {h.action || "?"} v{h.from_version ?? "?"} → v{h.to_version ?? "?"}
                  {h.reason ? ` · ${h.reason}` : ""}
                </div>
              ))}
            </div>
          )}
          {history && history.length === 0 && (
            <div className="text-[11px] text-mc-faint">无晋升/回滚历史</div>
          )}
        </div>
      )}
    </div>
  );
}
