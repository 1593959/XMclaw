import { lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from "react";
import SegTabs from "../components/SegTabs";
import { apiGet, apiGetFresh, apiPost } from "../lib/api";
import { useApp } from "../store/app";

const CognitionView = lazy(() => import("./CognitionView"));

const SKILL_TABS = [
  { id: "skills" as const, label: "技能" },
  { id: "cognition" as const, label: "认知" },
];

interface SkillManifest {
  title?: string;
  description?: string;
  when_to_use?: string;
  triggers?: string[];
  trust_level?: string;
  requires_restart?: boolean;
  created_by?: string;
  [key: string]: unknown;
}

interface SkillVersion {
  version: number;
  is_head: boolean;
  manifest?: SkillManifest;
}

interface Skill {
  id: string;
  head_version: number;
  source?: string;
  versions: SkillVersion[];
}

interface SkillRootDir {
  id: string;
  path: string;
  has_skill_md: boolean;
  has_manifest_json: boolean;
  has_skill_py: boolean;
}

interface SkillRoot {
  kind: string;
  path: string;
  exists: boolean;
  skill_dirs: SkillRootDir[];
  skill_dir_count: number;
  error?: string;
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
  observer?: {
    is_running?: boolean;
    arms?: Arm[];
    ready_to_propose_count?: number;
  };
}

interface SkillsResponse {
  skills?: Skill[];
  pending_restarts?: Array<Record<string, unknown>>;
  load_failures?: Array<Record<string, unknown>>;
  roots?: SkillRoot[];
}

interface HistoryRecord {
  action?: string;
  kind?: string;
  from_version?: number;
  to_version?: number;
  reason?: string;
  ts?: number;
}

export default function SkillsView() {
  const token = useApp((s) => s.token);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [roots, setRoots] = useState<SkillRoot[]>([]);
  const [pendingRestarts, setPendingRestarts] = useState<Array<Record<string, unknown>>>([]);
  const [loadFailures, setLoadFailures] = useState<Array<Record<string, unknown>>>([]);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [tab, setTab] = useState<"skills" | "cognition">("skills");

  const refresh = () => {
    if (!token) return;
    const ctl = new AbortController();
    apiGetFresh<SkillsResponse>("/api/v2/skills", token, ctl.signal)
      .then((data) => {
        setSkills(data.skills || []);
        setRoots(data.roots || []);
        setPendingRestarts(data.pending_restarts || []);
        setLoadFailures(data.load_failures || []);
      })
      .catch(() => {
        if (!ctl.signal.aborted) {
          setSkills([]);
          setRoots([]);
        }
      });
    apiGetFresh<Snapshot>("/api/v2/evolution/snapshot", token, ctl.signal)
      .then(setSnap)
      .catch(() => {
        if (!ctl.signal.aborted) setSnap(null);
      });
    return () => ctl.abort();
  };

  useEffect(() => refresh(), [token]);

  const arms = snap?.observer?.arms || [];
  const registeredIds = useMemo(() => new Set(skills.map((s) => s.id)), [skills]);
  const ghostDirs = roots.flatMap((root) =>
    (root.skill_dirs || [])
      .filter((dir) => !registeredIds.has(dir.id))
      .map((dir) => ({ root, dir })),
  );

  if (tab === "cognition") {
    return (
      <div className="flex-1 flex flex-col min-h-0">
        <div className="px-5 pt-4 shrink-0">
          <SegTabs tabs={SKILL_TABS} cur={tab} onPick={setTab} />
        </div>
        <div className="flex-1 overflow-y-auto min-h-0">
          <Suspense fallback={<div className="p-5 text-mc-faint text-sm">加载中...</div>}>
            <CognitionView />
          </Suspense>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-4">
      <SegTabs tabs={SKILL_TABS} cur={tab} onPick={setTab} />

      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">技能</h2>
          <p className="text-xs text-mc-faint mt-0.5">
            已注册技能、安装目录、加载失败和自进化状态
          </p>
        </div>
        <button
          type="button"
          onClick={refresh}
          className="text-xs px-3 py-1.5 rounded-md border border-mc-border text-mc-muted hover:text-mc-text cursor-pointer"
        >
          刷新
        </button>
      </div>

      <div className="flex gap-3 flex-wrap">
        <Stat label="已注册" value={skills.length} />
        <Stat label="扫描目录" value={roots.reduce((n, r) => n + (r.skill_dir_count || 0), 0)} />
        <Stat label="未注册目录" value={ghostDirs.length} tone={ghostDirs.length ? "text-mc-warn" : ""} />
        <Stat label="加载失败" value={loadFailures.length} tone={loadFailures.length ? "text-mc-err" : ""} />
        <Stat
          label="待重启"
          value={pendingRestarts.length}
          tone={pendingRestarts.length ? "text-mc-warn" : ""}
        />
        <Stat
          label="可晋升"
          value={snap?.observer?.ready_to_propose_count ?? "-"}
          tone={(snap?.observer?.ready_to_propose_count || 0) > 0 ? "text-mc-ok" : ""}
        />
      </div>

      {loadFailures.length > 0 && (
        <Banner tone="err">
          {loadFailures.length} 个技能加载失败：
          {loadFailures.map((item, index) => (
            <span key={index} className="font-mono ml-1">
              {String(item.skill_id || item.path || "?")}
            </span>
          ))}
        </Banner>
      )}

      {pendingRestarts.length > 0 && (
        <Banner tone="warn">{pendingRestarts.length} 个技能改动需要重启 daemon 后生效。</Banner>
      )}

      <SkillRoots roots={roots} ghostDirs={ghostDirs} />

      {arms.length > 0 && (
        <section className="space-y-1.5">
          <h3 className="text-[13px] font-medium">晋升进度</h3>
          {arms.map((arm) => {
            const progress = Math.round(
              Math.min(arm.progress?.plays_progress ?? 0, arm.progress?.mean_progress ?? 0) * 100,
            );
            return (
              <div key={`${arm.skill_id}@${arm.version}`} className="border border-mc-border rounded-md px-3 py-2">
                <div className="flex items-center gap-2 text-[12.5px]">
                  <span className="font-mono">{arm.skill_id}</span>
                  <span className="text-mc-faint">v{arm.version}</span>
                  <span className="text-mc-faint">调用 {arm.plays} 次 · 均分 {arm.mean_score?.toFixed(2)}</span>
                  {arm.progress?.ready_to_propose && <span className="text-mc-ok text-[11px]">可晋升</span>}
                  <div className="flex-1" />
                  <span className="text-[11px] text-mc-muted">{progress}%</span>
                </div>
                <div className="h-1 rounded-full bg-mc-border mt-1.5">
                  <div className="h-1 rounded-full bg-mc-accent" style={{ width: `${progress}%` }} />
                </div>
              </div>
            );
          })}
        </section>
      )}

      <section className="space-y-1.5">
        <h3 className="text-[13px] font-medium">技能清单</h3>
        {skills.map((skill) => (
          <SkillRow key={skill.id} skill={skill} token={token} />
        ))}
        {skills.length === 0 && (
          <div className="rounded-md border border-mc-border bg-mc-panel2/40 px-3 py-3 text-xs text-mc-faint">
            暂无已注册技能。请检查上方目录是否存在 SKILL.md、manifest.json 或 skill.py。
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ label, value, tone = "" }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded-md bg-mc-panel2 px-4 py-3 min-w-24">
      <div className="text-[11px] text-mc-faint">{label}</div>
      <div className={`text-xl font-semibold mt-0.5 ${tone || "text-mc-text"}`}>{value}</div>
    </div>
  );
}

function Banner({ tone, children }: { tone: "warn" | "err"; children: ReactNode }) {
  const cls = tone === "err"
    ? "border-mc-err/40 bg-mc-err/5 text-mc-err"
    : "border-mc-warn/40 bg-mc-warn/5 text-mc-warn";
  return <div className={`border rounded-md px-3 py-2 text-[12px] ${cls}`}>{children}</div>;
}

function SkillRoots({
  roots,
  ghostDirs,
}: {
  roots: SkillRoot[];
  ghostDirs: Array<{ root: SkillRoot; dir: SkillRootDir }>;
}) {
  if (roots.length === 0) return null;
  return (
    <section className="space-y-2">
      <h3 className="text-[13px] font-medium">安装与扫描目录</h3>
      <div className="grid gap-2">
        {roots.map((root) => (
          <div key={`${root.kind}:${root.path}`} className="rounded-md border border-mc-border bg-mc-panel2/40 px-3 py-2">
            <div className="flex items-center gap-2 text-[12px]">
              <span className="text-mc-accent">{root.kind === "canonical" ? "主目录" : "额外目录"}</span>
              <span className="font-mono truncate">{root.path}</span>
              <span className={root.exists ? "text-mc-ok" : "text-mc-warn"}>
                {root.exists ? "存在" : "不存在"}
              </span>
              <span className="text-mc-faint">目录 {root.skill_dir_count}</span>
            </div>
            {root.error && <div className="text-[11px] text-mc-err mt-1">{root.error}</div>}
          </div>
        ))}
      </div>
      {ghostDirs.length > 0 && (
        <div className="rounded-md border border-mc-warn/40 bg-mc-warn/5 px-3 py-2">
          <div className="text-[12px] text-mc-warn">发现目录但未注册为可用技能</div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {ghostDirs.slice(0, 12).map(({ root, dir }) => (
              <span key={`${root.path}:${dir.id}`} className="text-[11px] px-2 py-0.5 rounded border border-mc-border text-mc-muted">
                {dir.id} · {formatSkillFiles(dir)}
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function SkillRow({ skill, token }: { skill: Skill; token: string | null }) {
  const showToast = useApp((s) => s.showToast);
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState<HistoryRecord[] | null>(null);
  const [busy, setBusy] = useState(false);
  const head = skill.versions?.find((v) => v.is_head);
  const manifest = head?.manifest || {};

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && history === null && token) {
      apiGet<{ records?: HistoryRecord[] }>(`/api/v2/skills/${encodeURIComponent(skill.id)}/history`, token)
        .then((data) => setHistory(data.records || []))
        .catch(() => setHistory([]));
    }
  }

  async function rollback(toVersion: number) {
    if (!token || busy) return;
    setBusy(true);
    try {
      const resp = await apiPost<{ ok?: boolean; error?: string }>(
        `/api/v2/skills/${encodeURIComponent(skill.id)}/rollback`,
        { to_version: toVersion, reason: "用户从技能页面手动回滚" },
        token,
      );
      if (resp.error) showToast(resp.error, "err");
      else showToast(`已回滚 ${skill.id} 到 v${toVersion}`, "ok");
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err), "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border border-mc-border rounded-md bg-mc-panel2/40">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full text-left px-3 py-2 flex items-start gap-2 cursor-pointer"
      >
        <span className="text-mc-faint text-[10px] mt-0.5">{open ? "▾" : "▸"}</span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[12.5px]">{skill.id}</span>
            <span className="text-[11px] text-mc-faint">v{skill.head_version}</span>
            {skill.source && <span className="text-[11px] text-mc-faint">{sourceLabel(skill.source)}</span>}
            {manifest.trust_level && <span className="text-[11px] text-mc-faint">{String(manifest.trust_level)}</span>}
          </div>
          <div className="text-[12px] text-mc-muted truncate mt-0.5">
            {String(manifest.description || manifest.title || "未提供描述")}
          </div>
        </div>
      </button>

      {open && (
        <div className="px-3 pb-3 pt-0.5 space-y-3 border-t border-mc-border/50">
          <InfoBlock title="何时使用" text={String(manifest.when_to_use || "未声明")} />
          {Array.isArray(manifest.triggers) && manifest.triggers.length > 0 && (
            <div className="flex gap-1.5 flex-wrap">
              {manifest.triggers.map((trigger) => (
                <span key={trigger} className="text-[11px] px-2 py-0.5 rounded border border-mc-border text-mc-muted">
                  {trigger}
                </span>
              ))}
            </div>
          )}

          <div>
            <div className="text-[11px] text-mc-faint mb-1">版本（可回滚到非当前版本）</div>
            <div className="flex gap-1.5 flex-wrap">
              {(skill.versions || [])
                .slice()
                .sort((a, b) => b.version - a.version)
                .map((version) => (
                  <button
                    type="button"
                    key={version.version}
                    disabled={version.is_head || busy}
                    onClick={() => rollback(version.version)}
                    className={
                      "text-[11px] px-2 py-0.5 rounded border cursor-pointer disabled:cursor-default " +
                      (version.is_head
                        ? "border-mc-accent/50 text-mc-accent bg-mc-accent/10"
                        : "border-mc-border text-mc-muted hover:border-mc-accent/40 hover:text-mc-accent")
                    }
                  >
                    v{version.version}
                    {version.is_head ? " · 当前" : ""}
                  </button>
                ))}
            </div>
          </div>

          {history && history.length > 0 && (
            <div className="space-y-0.5">
              <div className="text-[11px] text-mc-faint">晋升 / 回滚记录</div>
              {history.slice(-6).reverse().map((item, index) => (
                <div key={index} className="text-[11px] text-mc-muted">
                  {item.action || item.kind || "变更"} v{item.from_version ?? "?"} → v{item.to_version ?? "?"}
                  {item.reason ? ` · ${item.reason}` : ""}
                </div>
              ))}
            </div>
          )}
          {history && history.length === 0 && (
            <div className="text-[11px] text-mc-faint">暂无晋升或回滚历史</div>
          )}
        </div>
      )}
    </div>
  );
}

function InfoBlock({ title, text }: { title: string; text: string }) {
  return (
    <div>
      <div className="text-[11px] text-mc-faint mb-1">{title}</div>
      <div className="text-[12px] text-mc-muted whitespace-pre-wrap leading-relaxed">{text}</div>
    </div>
  );
}

function sourceLabel(source: string): string {
  return {
    "built-in": "内置",
    user: "用户",
    evolved: "进化",
    llm: "自动生成",
    unknown: "未知",
  }[source] || source;
}

function formatSkillFiles(dir: SkillRootDir): string {
  const parts = [];
  if (dir.has_skill_md) parts.push("SKILL.md");
  if (dir.has_manifest_json) parts.push("manifest.json");
  if (dir.has_skill_py) parts.push("skill.py");
  return parts.length ? parts.join(" / ") : "缺少入口文件";
}
