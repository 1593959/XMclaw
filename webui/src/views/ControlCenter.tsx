import { useCallback, useEffect, useMemo, useState } from "react";
import SegTabs from "../components/SegTabs";
import { apiGetFresh, apiPatch } from "../lib/api";
import { useApp } from "../store/app";

type FieldType = "boolean" | "integer" | "number" | "string" | "string_list" | "select";
type GroupId = "security" | "voice" | "models" | "memory" | "skills" | "runtime";
type DraftValue = string | boolean;

interface ControlField {
  path: string;
  group: GroupId;
  label: string;
  description?: string;
  type: FieldType;
  value: unknown;
  options?: string[];
  configured: boolean;
  restart_required: boolean;
}

interface ControlSnapshot {
  ok: boolean;
  config_path: string | null;
  groups: Partial<Record<GroupId, ControlField[]>>;
  fields: ControlField[];
}

const GROUPS: { id: GroupId; label: string }[] = [
  { id: "security", label: "安全" },
  { id: "voice", label: "语音" },
  { id: "models", label: "模型" },
  { id: "memory", label: "记忆" },
  { id: "skills", label: "技能" },
  { id: "runtime", label: "运行时" },
];

export default function ControlCenter() {
  const token = useApp((s) => s.token);
  const showToast = useApp((s) => s.showToast);
  const [snapshot, setSnapshot] = useState<ControlSnapshot | null>(null);
  const [drafts, setDrafts] = useState<Record<string, DraftValue>>({});
  const [tab, setTab] = useState<GroupId>("security");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [restartRequired, setRestartRequired] = useState(false);

  const load = useCallback(() => {
    if (!token) return undefined;
    const ctl = new AbortController();
    setLoading(true);
    apiGetFresh<ControlSnapshot>("/api/v2/config/control", token, ctl.signal)
      .then((data) => {
        setSnapshot(data);
        const next: Record<string, DraftValue> = {};
        for (const field of data.fields || []) next[field.path] = valueToDraft(field);
        setDrafts(next);
      })
      .catch(() => {
        if (!ctl.signal.aborted) {
          setSnapshot(null);
          setDrafts({});
        }
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false);
      });
    return () => ctl.abort();
  }, [token]);

  useEffect(load, [load]);

  const fields = snapshot?.groups?.[tab] || [];
  const dirtyPatch = useMemo(() => {
    const patch: Record<string, unknown> = {};
    for (const field of snapshot?.fields || []) {
      const draft = drafts[field.path];
      const next = draftToValue(field, draft ?? valueToDraft(field));
      if (JSON.stringify(next) !== JSON.stringify(normalizedCurrent(field))) {
        patch[field.path] = next;
      }
    }
    return patch;
  }, [snapshot, drafts]);
  const dirtyCount = Object.keys(dirtyPatch).length;

  async function save() {
    if (!token || dirtyCount === 0) return;
    setSaving(true);
    try {
      const result = await apiPatch<{ ok: boolean; restart_required?: boolean }>(
        "/api/v2/config/control",
        { patch: dirtyPatch },
        token,
      );
      setRestartRequired(Boolean(result.restart_required));
      showToast(
        result.restart_required ? "已保存，部分配置需要重启 daemon 生效" : "配置已保存",
        "ok",
      );
      load();
    } catch (err) {
      showToast(`保存失败：${String((err as Error)?.message || err)}`, "err");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="px-5 pt-4 pb-3 border-b border-mc-border shrink-0">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">控制中心</h2>
            <p className="text-xs text-mc-faint mt-0.5">
              安全、语音、模型、记忆、技能和 Agent 运行时配置。
            </p>
          </div>
          <div className="flex items-center gap-2">
            {restartRequired && (
              <span className="text-[11px] text-mc-warn border border-mc-warn/40 rounded px-2 py-1">
                需要重启
              </span>
            )}
            <button
              type="button"
              onClick={load}
              className="px-3 py-1.5 rounded-md border border-mc-border text-xs text-mc-muted hover:text-mc-text cursor-pointer"
            >
              刷新
            </button>
            <button
              type="button"
              onClick={save}
              disabled={saving || dirtyCount === 0}
              className={
                "px-3 py-1.5 rounded-md text-xs border cursor-pointer " +
                (dirtyCount === 0
                  ? "border-mc-border text-mc-faint cursor-not-allowed"
                  : "border-mc-accent/50 bg-mc-accent/15 text-mc-accent")
              }
            >
              {saving ? "保存中..." : dirtyCount ? `保存 ${dirtyCount} 项` : "已同步"}
            </button>
          </div>
        </div>
        <div className="mt-3">
          <SegTabs tabs={GROUPS} cur={tab} onPick={setTab} />
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto p-5">
        {loading ? (
          <div className="text-sm text-mc-faint">加载中...</div>
        ) : !snapshot ? (
          <div className="text-sm text-mc-err">控制中心配置接口不可用。</div>
        ) : (
          <div className="max-w-5xl space-y-3">
            <div className="text-[11px] text-mc-faint">
              配置文件：{snapshot.config_path || "当前 daemon 未暴露 config_path"}
            </div>
            <div className="border border-mc-border rounded-md overflow-hidden">
              {fields.map((field) => (
                <ConfigRow
                  key={field.path}
                  field={field}
                  value={drafts[field.path] ?? valueToDraft(field)}
                  onChange={(value) => setDrafts((d) => ({ ...d, [field.path]: value }))}
                />
              ))}
              {fields.length === 0 && (
                <div className="px-4 py-6 text-sm text-mc-faint">这个分组暂无可配置项。</div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ConfigRow({
  field,
  value,
  onChange,
}: {
  field: ControlField;
  value: DraftValue;
  onChange: (value: DraftValue) => void;
}) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-[minmax(220px,300px)_1fr_auto] gap-3 md:gap-4 md:items-center px-4 py-3 border-b border-mc-border/60 last:border-b-0 bg-mc-panel/30">
      <div className="min-w-0">
        <div className="text-sm font-medium">{field.label}</div>
        {field.description && (
          <div className="text-[11px] text-mc-muted mt-1 leading-relaxed">{field.description}</div>
        )}
        <div className="text-[11px] text-mc-faint font-mono truncate mt-1">{field.path}</div>
      </div>
      <FieldInput field={field} value={value} onChange={onChange} />
      <div className="text-[10px] md:text-right md:min-w-20">
        {field.restart_required ? (
          <span className="text-mc-warn">重启生效</span>
        ) : (
          <span className="text-mc-faint">运行时</span>
        )}
      </div>
    </div>
  );
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: ControlField;
  value: DraftValue;
  onChange: (value: DraftValue) => void;
}) {
  const base =
    "w-full rounded-md border border-mc-border bg-mc-panel2 px-2.5 py-1.5 text-sm outline-none focus:border-mc-accent";
  if (field.type === "boolean") {
    const on = Boolean(value);
    return (
      <button
        type="button"
        onClick={() => onChange(!on)}
        aria-pressed={on}
        className={
          "w-11 h-6 rounded-full relative transition-colors cursor-pointer " +
          (on ? "bg-mc-accent" : "bg-mc-border")
        }
      >
        <span
          className="absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform"
          style={{ transform: on ? "translateX(20px)" : "translateX(0)" }}
        />
      </button>
    );
  }
  if (field.type === "select") {
    return (
      <select className={base} value={String(value)} onChange={(e) => onChange(e.target.value)}>
        {(field.options || []).map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    );
  }
  if (field.type === "string_list") {
    return (
      <textarea
        className={base + " min-h-20 font-mono text-xs resize-y"}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <input
      className={base}
      type={field.type === "integer" || field.type === "number" ? "number" : "text"}
      value={String(value)}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function valueToDraft(field: ControlField): DraftValue {
  if (field.type === "boolean") return Boolean(field.value);
  if (field.type === "string_list") return Array.isArray(field.value) ? field.value.join("\n") : "";
  if (field.value === null || field.value === undefined) return "";
  return String(field.value);
}

function normalizedCurrent(field: ControlField): unknown {
  if (field.type === "string_list") return Array.isArray(field.value) ? field.value : [];
  return field.value;
}

function draftToValue(field: ControlField, draft: DraftValue): unknown {
  if (field.type === "boolean") return Boolean(draft);
  if (field.type === "integer") return Number.parseInt(String(draft || "0"), 10);
  if (field.type === "number") return Number(String(draft || "0"));
  if (field.type === "string_list") {
    return String(draft || "")
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return String(draft);
}
