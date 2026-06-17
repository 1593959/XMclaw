// 共享二级菜单：分段控件（2026-06-17 用户点名「二级菜单还没优化」）。
// 替代各域里松散的 bordered-pill 子标签 —— 容器 + accent 实心选中态，统一精致。
import type { ReactNode } from "react";

export interface SegTab<T extends string> {
  id: T;
  label: ReactNode;
}

export default function SegTabs<T extends string>({
  tabs,
  cur,
  onPick,
  className = "",
}: {
  tabs: ReadonlyArray<SegTab<T>>;
  cur: T;
  onPick: (v: T) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={
        "inline-flex gap-0.5 p-0.5 rounded-lg bg-mc-panel2 border border-mc-border " +
        className
      }
    >
      {tabs.map((t) => {
        const active = cur === t.id;
        return (
          <button
            key={t.id}
            role="tab"
            aria-selected={active}
            onClick={() => onPick(t.id)}
            className={
              "text-xs px-3 py-1.5 rounded-md transition-colors cursor-pointer whitespace-nowrap " +
              (active
                ? "bg-mc-accent text-white shadow-sm"
                : "text-mc-faint hover:text-mc-text hover:bg-mc-border/50")
            }
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
