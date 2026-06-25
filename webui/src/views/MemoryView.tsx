import { lazy, Suspense, useEffect, useState } from "react";
import { apiGetFresh, apiPost } from "../lib/api";
import { useApp } from "../store/app";

const MemoryGraph = lazy(() => import("./MemoryGraph"));

interface Overview {
  enabled?: boolean;
  healthy?: boolean;
  total?: number;
  by_kind?: Record<string, number>;
  by_layer?: Record<string, number>;
  contradictions?: number;
  stale?: number;
  error?: string;
}

interface Fact {
  id: string;
  kind: string;
  scope: string;
  text: string;
  confidence: number;
  evidence_count: number;
  layer: string;
  bucket: string;
  forgotten: boolean;
  superseded_by: string | null;
}

interface MemoryCandidate {
  id: string;
  text: string;
  kind: string;
  scope: string;
  bucket: string;
  source: string;
  source_event_id: string | null;
  confidence: number;
  quality_score?: number;
  quality_reasons?: string[];
  reason: string;
  evidence: Array<Record<string, unknown>>;
  neighbor_ids: string[];
  status: string;
  decision_reason: string;
  created_at: number;
  updated_at: number;
  decided_at: number | null;
  promoted_fact_id: string | null;
  metadata: Record<string, unknown>;
}

interface CandidateResponse {
  enabled: boolean;
  items: MemoryCandidate[];
  stats?: {
    total?: number;
    by_status?: Record<string, number>;
    db_path?: string;
  };
}

type MemoryTab = "list" | "graph" | "candidates";

export default function MemoryView() {
  const token = useApp((s) => s.token);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [query, setQuery] = useState("");
  const [facts, setFacts] = useState<Fact[]>([]);
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [candidateStats, setCandidateStats] = useState<CandidateResponse["stats"]>({});
  const [candidatesEnabled, setCandidatesEnabled] = useState(true);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<MemoryTab>("list");
  const [tick, setTick] = useState(0);
  const refetch = () => setTick((t) => t + 1);

  useEffect(() => {
    if (!token) return;
    const ctl = new AbortController();
    apiGetFresh<Overview>("/api/v2/memory/v2/overview", token, ctl.signal)
      .then(setOverview)
      .catch(() => {
        if (!ctl.signal.aborted) setOverview(null);
      });
    return () => ctl.abort();
  }, [token, tick]);

  useEffect(() => {
    if (!token) return;
    const ctl = new AbortController();
    setLoading(true);
    const timer = window.setTimeout(() => {
      const suffix = query ? `&q=${encodeURIComponent(query)}` : "";
      apiGetFresh<{ facts?: Fact[] }>(
        `/api/v2/memory/v2/facts?limit=80&include_superseded=true${suffix}`,
        token,
        ctl.signal,
      )
        .then((data) => setFacts(data?.facts || []))
        .catch(() => {
          if (!ctl.signal.aborted) setFacts([]);
        })
        .finally(() => {
          if (!ctl.signal.aborted) setLoading(false);
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      ctl.abort();
    };
  }, [token, query, tick]);

  useEffect(() => {
    if (!token) return;
    const ctl = new AbortController();
    setCandidatesLoading(true);
    apiGetFresh<CandidateResponse>(
      "/api/v2/memory/v2/candidates?status=pending&limit=100",
      token,
      ctl.signal,
    )
      .then((data) => {
        setCandidatesEnabled(Boolean(data.enabled));
        setCandidates(data.items || []);
        setCandidateStats(data.stats || {});
      })
      .catch(() => {
        if (!ctl.signal.aborted) {
          setCandidatesEnabled(false);
          setCandidates([]);
          setCandidateStats({});
        }
      })
      .finally(() => {
        if (!ctl.signal.aborted) setCandidatesLoading(false);
      });
    return () => ctl.abort();
  }, [token, tick]);

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-4">
      <div>
        <h2 className="text-base font-semibold">记忆</h2>
        <p className="text-xs text-mc-faint mt-0.5">
          结构化事实、候选审核、召回图谱和记忆健康状态。
        </p>
      </div>

      <div className="flex gap-3 flex-wrap">
        <StatCard label="事实总数" value={overview?.total ?? "-"} />
        <StatCard label="长期层" value={overview?.by_layer?.long_term ?? "-"} />
        <StatCard label="工作层" value={overview?.by_layer?.working ?? "-"} />
        <StatCard label="待审候选" value={candidateStats?.by_status?.pending ?? candidates.length} />
        <StatCard
          label="冲突"
          value={overview?.contradictions ?? "-"}
          tone={(overview?.contradictions || 0) > 0 ? "text-mc-warn" : undefined}
        />
        <StatCard label="过期" value={overview?.stale ?? "-"} />
        {overview?.healthy === false && (
          <StatCard label="状态" value="异常" tone="text-mc-err" />
        )}
      </div>

      {overview?.by_kind && (
        <div className="flex gap-1.5 flex-wrap">
          {Object.entries(overview.by_kind)
            .sort((a, b) => b[1] - a[1])
            .map(([kind, count]) => (
              <span
                key={kind}
                className="text-[11px] px-2 py-0.5 rounded-full border border-mc-border text-mc-muted"
              >
                {kindLabel(kind)} · {count}
              </span>
            ))}
        </div>
      )}

      <div className="flex items-center gap-2">
        {(["list", "graph", "candidates"] as const).map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setTab(item)}
            className={
              "text-xs px-3 py-1.5 rounded-md border cursor-pointer " +
              (tab === item
                ? "border-mc-accent/50 text-mc-accent bg-mc-accent/10"
                : "border-mc-border text-mc-faint hover:text-mc-muted")
            }
          >
            {tabLabel(item)}
          </button>
        ))}
        {tab === "list" && (
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索记忆..."
            className="flex-1 max-w-md text-[13px] px-3 py-1.5 rounded-md border border-mc-border bg-mc-panel2 outline-none focus:border-mc-accent"
          />
        )}
      </div>

      {tab === "graph" && (
        <Suspense fallback={<div className="text-xs text-mc-faint">图谱加载中...</div>}>
          <MemoryGraph />
        </Suspense>
      )}

      {tab === "candidates" && (
        <MemoryCandidatePanel
          enabled={candidatesEnabled}
          loading={candidatesLoading}
          items={candidates}
          token={token}
          onChanged={refetch}
        />
      )}

      <div className="space-y-1.5" style={tab !== "list" ? { display: "none" } : undefined}>
        {loading && <div className="text-xs text-mc-faint">搜索中...</div>}
        {!loading && facts.length === 0 && (
          <div className="text-xs text-mc-faint">没有匹配的记忆</div>
        )}
        {facts.map((fact) => (
          <FactRow key={fact.id} fact={fact} token={token} onChanged={refetch} />
        ))}
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: string;
}) {
  return (
    <div className="rounded-md bg-mc-panel2 px-4 py-3 min-w-28">
      <div className="text-[11px] text-mc-faint">{label}</div>
      <div className={"text-xl font-semibold mt-0.5 " + (tone || "text-mc-text")}>
        {value}
      </div>
    </div>
  );
}

function MemoryCandidatePanel({
  enabled,
  loading,
  items,
  token,
  onChanged,
}: {
  enabled: boolean;
  loading: boolean;
  items: MemoryCandidate[];
  token: string | null;
  onChanged: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">候选记忆审核</div>
          <div className="text-xs text-mc-faint mt-0.5">
            自动抽取只进入候选池。批准后才会成为可召回的长期事实。
          </div>
        </div>
        <button
          type="button"
          onClick={onChanged}
          className="text-xs px-3 py-1.5 rounded-md border border-mc-border text-mc-muted hover:text-mc-text cursor-pointer"
        >
          刷新
        </button>
      </div>

      {!enabled && (
        <div className="rounded-md border border-mc-border bg-mc-panel2/40 px-4 py-3 text-sm text-mc-faint">
          候选记忆审核接口不可用。
        </div>
      )}
      {enabled && loading && <div className="text-xs text-mc-faint">加载中...</div>}
      {enabled && !loading && items.length === 0 && (
        <div className="rounded-md border border-mc-border bg-mc-panel2/40 px-4 py-3 text-sm text-mc-faint">
          暂无待审核候选。
        </div>
      )}
      {enabled && !loading && items.map((item) => (
        <MemoryCandidateRow key={item.id} item={item} token={token} onChanged={onChanged} />
      ))}
    </div>
  );
}

function MemoryCandidateRow({
  item,
  token,
  onChanged,
}: {
  item: MemoryCandidate;
  token: string | null;
  onChanged: () => void;
}) {
  const showToast = useApp((s) => s.showToast);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const quality = Math.round((item.quality_score ?? 0) * 100);

  async function decide(kind: "approve" | "reject") {
    setBusy(kind);
    try {
      const reason = kind === "approve" ? "approved_from_memory_panel" : "rejected_from_memory_panel";
      const resp = await apiPost<{ candidate?: MemoryCandidate; fact?: Fact }>(
        `/api/v2/memory/v2/candidates/${encodeURIComponent(item.id)}/${kind}`,
        { reason },
        token,
      );
      if (resp.candidate) {
        showToast(kind === "approve" ? "已批准候选记忆" : "已拒绝候选记忆", "ok");
        onChanged();
      } else {
        showToast("操作失败", "err");
      }
    } catch {
      showToast("操作失败：daemon 未响应", "err");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="border border-mc-border rounded-md bg-mc-panel2/40 px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] leading-relaxed">{item.text}</div>
          <div className="flex gap-2 mt-2 text-[10.5px] text-mc-faint flex-wrap">
            <span className="text-mc-accent">{kindLabel(item.kind)}</span>
            <span>{scopeLabel(item.scope)}</span>
            {item.bucket && <span>{bucketLabel(item.bucket)}</span>}
            {item.source && <span>{sourceLabel(item.source)}</span>}
            <span>证据 x{item.evidence?.length || 0}</span>
            <span>相邻 x{item.neighbor_ids?.length || 0}</span>
            <span>置信 {Math.round((item.confidence || 0) * 100)}%</span>
            <span className={quality < 50 ? "text-mc-warn" : "text-mc-ok"}>
              质量 {quality}%
            </span>
            <span>{formatTime(item.created_at)}</span>
          </div>
          {(item.reason || item.quality_reasons?.length || item.source_event_id) && (
            <div className="mt-2 text-[11px] text-mc-faint space-y-1">
              {item.reason && <div>拦截原因：{candidateReasonLabel(item.reason)}</div>}
              {!!item.quality_reasons?.length && (
                <div>质量说明：{item.quality_reasons.map(qualityReasonLabel).join("、")}</div>
              )}
              {item.source_event_id && <div className="font-mono">{item.source_event_id}</div>}
            </div>
          )}
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            type="button"
            onClick={() => decide("approve")}
            disabled={busy !== null}
            className="text-[11px] px-2 py-1 rounded-md border border-mc-ok/30 text-mc-ok hover:bg-mc-ok/10 cursor-pointer disabled:opacity-50"
          >
            {busy === "approve" ? "批准中" : "批准"}
          </button>
          <button
            type="button"
            onClick={() => decide("reject")}
            disabled={busy !== null}
            className="text-[11px] px-2 py-1 rounded-md border border-mc-err/30 text-mc-err hover:bg-mc-err/10 cursor-pointer disabled:opacity-50"
          >
            {busy === "reject" ? "拒绝中" : "拒绝"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FactRow({
  fact,
  token,
  onChanged,
}: {
  fact: Fact;
  token: string | null;
  onChanged: () => void;
}) {
  const showToast = useApp((s) => s.showToast);
  const [busy, setBusy] = useState(false);

  async function update(action: "forget" | "restore") {
    setBusy(true);
    try {
      const resp = await apiPost<{ ok?: boolean }>(
        `/api/v2/memory/v2/facts/${encodeURIComponent(fact.id)}/${action}`,
        {},
        token,
      );
      if (resp.ok) {
        showToast(action === "forget" ? "已遗忘该记忆" : "已恢复该记忆", "ok");
        onChanged();
      } else {
        showToast("操作失败", "err");
      }
    } catch {
      showToast("操作失败：daemon 未响应", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={"border border-mc-border rounded-md px-3 py-2 bg-mc-panel2/40 group " + (fact.forgotten ? "opacity-50" : "")}>
      <div className="flex items-start gap-2">
        <div className="text-[13px] leading-relaxed flex-1 min-w-0">{fact.text}</div>
        <div className="flex gap-2 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
          {fact.forgotten ? (
            <button
              type="button"
              onClick={() => update("restore")}
              disabled={busy}
              className="text-[11px] text-mc-faint hover:text-mc-ok cursor-pointer disabled:opacity-50"
            >
              恢复
            </button>
          ) : (
            <button
              type="button"
              onClick={() => update("forget")}
              disabled={busy}
              className="text-[11px] text-mc-faint hover:text-mc-err cursor-pointer disabled:opacity-50"
              title="软删除：标记遗忘，可恢复"
            >
              遗忘
            </button>
          )}
        </div>
      </div>
      <div className="flex gap-2 mt-1 text-[10.5px] text-mc-faint flex-wrap">
        <span className="text-mc-accent">{kindLabel(fact.kind)}</span>
        <span>{scopeLabel(fact.scope)}</span>
        <span>{fact.layer === "long_term" ? "长期" : "工作"}</span>
        {fact.bucket && <span>{bucketLabel(fact.bucket)}</span>}
        <span>证据 x{fact.evidence_count}</span>
        <span>置信 {Math.round((fact.confidence || 0) * 100)}%</span>
        {fact.forgotten && <span className="text-mc-warn">已遗忘</span>}
        {fact.superseded_by && !fact.forgotten && <span>已被取代</span>}
      </div>
    </div>
  );
}

function tabLabel(tab: MemoryTab): string {
  if (tab === "list") return "列表";
  if (tab === "graph") return "图谱";
  return "候选";
}

function kindLabel(kind: string): string {
  return {
    identity: "身份",
    preference: "偏好",
    rule: "规则",
    project: "项目",
    episode: "经历",
    lesson: "经验",
    procedure: "流程",
    fact: "事实",
  }[kind] || kind;
}

function scopeLabel(scope: string): string {
  return {
    user: "用户",
    project: "项目",
    global: "全局",
    session: "会话",
  }[scope] || scope;
}

function bucketLabel(bucket: string): string {
  return {
    user_preference: "用户偏好",
    user_identity: "用户身份",
    failure_modes: "失败模式",
    tool_quirks: "工具细节",
    workflow: "工作流",
    project_fact: "项目事实",
    procedural: "流程经验",
    rules: "规则",
    values: "价值观",
    misc: "其他",
  }[bucket] || bucket;
}

function sourceLabel(source: string): string {
  return {
    manual: "手动",
    manual_ui: "手动录入",
    post_sampling: "对话后提取",
    cognition: "认知后台",
    tool_result: "工具结果",
    gateway: "记忆网关",
    memory_decision: "记忆决策",
  }[source] || source;
}

function candidateReasonLabel(reason: string): string {
  return {
    task_not_terminal: "任务尚未结束",
    failed_tool_result: "工具结果失败",
    tool_result_without_terminal_evidence: "工具结果缺少终态证据",
    unverified_extracted_lesson: "未验证的经验提取",
    speculative_low_confidence_memory: "低置信推测",
    policy_blocked: "写入策略拦截",
  }[reason] || reason;
}

function qualityReasonLabel(reason: string): string {
  return {
    too_short: "内容过短",
    low_information_density: "信息密度低",
    no_evidence: "缺少证据",
    low_confidence: "置信度低",
    speculative_or_unverified: "包含猜测或未验证信息",
    weak_source: "来源较弱",
    tool_failed: "来自失败工具",
    unverified_extracted_lesson: "未验证经验",
    task_in_progress: "任务未完成",
    high_quality: "质量较高",
  }[reason] || reason;
}

function formatTime(value: number): string {
  if (!value) return "";
  try {
    return new Date(value * 1000).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}
