// 记忆域（10.M3 收编旧 Memory 页）— 驾驶舱仪表式：
// 读数条（/memory/v2/overview）+ facts 检索列表（/facts?q=）。

import { useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet } from "../lib/api";

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

  useEffect(() => {
    if (!token) return;
    apiGet<Overview>("/api/v2/memory/v2/overview", token).then(setOv).catch(() => setOv(null));
  }, [token]);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    const t = setTimeout(() => {
      apiGet<{ facts?: Fact[] }>(
        `/api/v2/memory/v2/facts?limit=80${q ? `&q=${encodeURIComponent(q)}` : ""}`,
        token,
      )
        .then((d) => setFacts(d?.facts || []))
        .catch(() => setFacts([]))
        .finally(() => setLoading(false));
    }, 250);
    return () => clearTimeout(t);
  }, [token, q]);

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

      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="检索记忆（关键词子串匹配）…"
        className="w-full max-w-md text-[13px] px-3 py-2 rounded-md border border-mc-border bg-mc-panel2 outline-none focus:border-mc-accent"
      />

      <div className="space-y-1.5">
        {loading && <div className="text-xs text-mc-faint">检索中…</div>}
        {!loading && facts.length === 0 && <div className="text-xs text-mc-faint">没有匹配的记忆</div>}
        {facts.map((f) => (
          <div key={f.id} className="border border-mc-border rounded-md px-3 py-2 bg-mc-panel2/40">
            <div className="text-[13px] leading-relaxed">{f.text}</div>
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
        ))}
      </div>
    </div>
  );
}
