// 侧栏拖拽缩放 hook（2026-06-12 用户点名）。宽度持久化 localStorage，
// 拖动期间禁用文本选择，双击把手恢复默认宽。

import { useCallback, useRef, useState } from "react";

export function useResizable(opts: {
  key: string;
  defaultWidth: number;
  min: number;
  max: number;
  // 右侧栏从左缘拖：鼠标向左移动 = 变宽 → invert。
  invert?: boolean;
}) {
  const { key, defaultWidth, min, max, invert = false } = opts;
  const storageKey = `mc.panelw.${key}`;
  const [width, setWidth] = useState<number>(() => {
    try {
      const v = Number(localStorage.getItem(storageKey));
      if (v >= min && v <= max) return v;
    } catch {
      /* ignore */
    }
    return defaultWidth;
  });
  const widthRef = useRef(width);
  widthRef.current = width;

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = widthRef.current;
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - startX;
        setWidth(Math.min(max, Math.max(min, startW + (invert ? -dx : dx))));
      };
      const onUp = () => {
        try {
          localStorage.setItem(storageKey, String(widthRef.current));
        } catch {
          /* ignore */
        }
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [min, max, invert, storageKey],
  );

  const reset = useCallback(() => {
    setWidth(defaultWidth);
    try {
      localStorage.setItem(storageKey, String(defaultWidth));
    } catch {
      /* ignore */
    }
  }, [defaultWidth, storageKey]);

  return { width, onMouseDown, reset };
}

export function ResizeHandle({
  onMouseDown,
  onDoubleClick,
}: {
  onMouseDown: (e: React.MouseEvent) => void;
  onDoubleClick: () => void;
}) {
  return (
    <div
      onMouseDown={onMouseDown}
      onDoubleClick={onDoubleClick}
      title="拖动调整宽度 · 双击恢复默认"
      className="w-1 shrink-0 cursor-col-resize group relative z-10"
    >
      <div className="absolute inset-y-0 -left-0.5 -right-0.5 group-hover:bg-mc-accent/30 group-active:bg-mc-accent/50 transition-colors" />
    </div>
  );
}
