// 记忆域（10.M3 收编旧 Memory 页）— 驾驶舱仪表式：
// 读数条（/memory/v2/overview）+ facts 检索列表（/facts?q=）。

import { lazy, Suspense, useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet, apiPost } from "../lib/api";

const MemoryGraph = lazy(() => import("./MemoryGraph"));

interface Overview {
  enabled?: boolean;
  healthy?: boolean;
  total?: number;
  by_kind?: Record<string, number>;
  by_layer?: Record<string, number>;
  by_scope?: Record<string, number>;
  contradictions?: number;
  stale?: number;
  error?: string;
  [k: string]: unknown;
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

function StatCard({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded-md bg-mc-panel2 px-4 py-3 min-w-28">
      <div className="text-[11px] text-mc-faint">{label}</div>
      <div className={"text-xl font-semibold mt-0.5 " + (tone || "text-mc-text")}>{value}</div>
    </div>
  );
}

export default function MemoryView() {
  const token = useApp((s) => s.token);
  const [ov, setOv] = useState<Overview | null>(null);
  const [q, setQ] = useState("");
  const [facts, setFacts] = useState<Fact[]>([]);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<"list" | "graph">("list");
  const [tick, setTick] = useState(0);
  const refetch = () => setTick((t) => t + 1);

  useEffect(() => {
    if (!token) return;
    apiGet<Overview>("/api/v2/memory/v2/overview", token).then(setOv).catch(() => setOv(null));
  }, [token, tick]);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    const t = setTimeout(() => {
      apiGet<{ facts?: Fact[] }>(
        `/api/v2/memory/v2/facts?limit=80&include_superseded=true${q ? `&q=${encodeURIComponent(q)}` : ""}`,
        token,
      )
        .then((d) => setFacts(d?.facts || []))
        .catch(() => setFacts([]))
        .finally(() => setLoading(false));
    }, 250);
    return () => clearTimeout(t);
  }, [token, q, tick]);

  return (
    <div className="flex-1 overflow-y-auto p-5 space-y-4">
      <div>
        <h2 className="text-base font-semibold">记忆</h2>
        <p className="text-xs text-mc-faint mt-0.5">agent 记住了什么、记得多牢 — LanceDB V2</p>
      </div>

      <div className="flex gap-3 flex-wrap">
        <StatCard label="事实总数" value={ov?.total ?? "—"} />
        <StatCard label="长期层" value={ov?.by_layer?.long_term ?? "—"} />
        <StatCard label="工作层" value={ov?.by_layer?.working ?? "—"} />
        <StatCard
          label="矛盾"
          value={ov?.contradictions ?? "—"}
          tone={(ov?.contradictions || 0) > 0 ? "text-mc-warn" : undefined}
        />
        <StatCard label="陈旧" value={ov?.stale ?? "—"} />
        {ov?.healthy === false && <StatCard label="状态" value="异常" tone="text-mc-err" />}
      </div>

      {ov?.by_kind && (
        <div className="flex gap-1.5 flex-wrap">
          {Object.entries(ov.by_kind)
            .sort((a, b) => b[1] - a[1])
            .map(([k, n]) => (
              <span key={k} className="text-[11px] px-2 py-0.5 rounded-full border border-mc-border text-mc-muted">
                {k} · {n}
              </span>
            ))}
        </div>
      )}

      <div className="flex items-center gap-2">
        {(["list", "graph"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={
              "text-xs px-3 py-1.5 rounded-md border cursor-pointer " +
              (tab === t
                ? "border-mc-accent/50 text-mc-accent bg-mc-accent/10"
                : "border-mc-border text-mc-faint hover:text-mc-muted")
            }
          >
            {t === "list" ? "☰ 列表" : "◉ 图谱"}
          </button>
        ))}
        {tab === "list" && (
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="检索记忆（关键词子串匹配）…"
            className="flex-1 max-w-md text-[13px] px-3 py-1.5 rounded-md border border-mc-border bg-mc-panel2 outline-none focus:border-mc-accent"
          />
        )}
      </div>

      {tab === "graph" && (
        <Suspense fallback={<div className="text-xs text-mc-faint">图谱加载中…</div>}>
          <MemoryGraph />
        </Suspense>
      )}

      <div className="space-y-1.5" style={tab === "graph" ? { display: "none" } : undefined}>
        {loading && <div className="text-xs text-mc-faint">检索中…</div>}
        {!loading && facts.length === 0 && <div className="text-xs text-mc-faint">没有匹配的记忆</div>}
        {facts.map((f) => (
          <FactRow key={f.id} fact={f} token={token} onChanged={refetch} />
        ))}
      </div>
    </div>
  );
}

function FactRow({ fact: f, token, onChanged }: { fact: Fact; token: string | null; onChanged: () => void }) {
  const showToast = useApp((s) => s.showToast);
  const [busy, setBusy] = useState(false);

  async function act(kind: "forget" | "restore") {
    setBusy(true);
    try {
      const r = await apiPost<{ ok: boolean }>(
        `/api/v2/memory/v2/facts/${encodeURIComponent(f.id)}/${kind}`,
        {},
        token,
      );
      if (r.ok) {
        showToast(kind === "forget" ? "已遗忘该记忆" : "已恢复该记忆", "ok");
        onChanged();
      } else {
        showToast("操作失败", "err");
      }
    } catch {
      showToast("操作失败（daemon 未响应）", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className={
        "border border-mc-border rounded-md px-3 py-2 bg-mc-panel2/40 group " +
        (f.forgotten ? "opacity-50" : "")
      }
    >
      <div className="flex items-start gap-2">
        <div className="text-[13px] leading-relaxed flex-1 min-w-0">{f.text}</div>
        <div className="flex gap-2 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
          {f.forgotten ? (
            <button
              onClick={() => act("restore")}
              disabled={busy}
              className="text-[11px] text-mc-faint hover:text-mc-ok cursor-pointer disabled:opacity-50"
            >
              恢复
            </button>
          ) : (
            <button
              onClick={() => act("forget")}
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
        <span className="text-mc-accent">{f.kind}</span>
        <span>{f.scope}</span>
        <span>{f.layer === "long_term" ? "长期" : "工作"}</span>
        {f.bucket && <span>{f.bucket}</span>}
        <span>证据 ×{f.evidence_count}</span>
        <span>置信 {Math.round((f.confidence || 0) * 100)}%</span>
        {f.forgotten && <span className="text-mc-warn">已遗忘</span>}
        {f.superseded_by && !f.forgotten && <span className="text-mc-faint">已被取代</span>}
      </div>
    </div>
  );
}
