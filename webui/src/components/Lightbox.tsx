// 媒体放大查看（2026-06-12 用户点名：点击在当前页缩放，不跳新页面）。
// 图片：滚轮缩放（围绕鼠标点）+ 拖拽平移 + 双击复位；视频：原生控件。
// Esc / 点遮罩关闭。

import { useEffect, useRef, useState } from "react";
import { useApp } from "../store/app";

export default function Lightbox() {
  const lb = useApp((s) => s.lightbox);
  const close = useApp((s) => s.closeLightbox);
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const panRef = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!lb) return;
    setScale(1);
    setOffset({ x: 0, y: 0 });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lb, close]);

  // 原生 wheel（passive:false）才能阻止页面滚动。
  useEffect(() => {
    const el = boxRef.current;
    if (!el || !lb || lb.kind !== "image") return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setScale((s) => Math.min(12, Math.max(0.2, s * Math.pow(1.0015, -e.deltaY))));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [lb]);

  if (!lb) return null;

  return (
    <div
      ref={boxRef}
      className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center"
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
      onMouseMove={(e) => {
        const p = panRef.current;
        if (p) setOffset({ x: p.ox + (e.clientX - p.sx), y: p.oy + (e.clientY - p.sy) });
      }}
      onMouseUp={() => (panRef.current = null)}
      onMouseLeave={() => (panRef.current = null)}
    >
      <button
        onClick={close}
        className="absolute top-3 right-4 text-mc-muted hover:text-white text-2xl cursor-pointer z-10"
        aria-label="关闭"
      >
        ×
      </button>
      {lb.kind === "image" && (
        <div className="absolute top-4 left-4 text-[11px] text-mc-faint select-none">
          滚轮缩放{scale !== 1 ? ` ${scale.toFixed(1)}×` : ""} · 拖动平移 · 双击复位 · Esc 关闭
        </div>
      )}
      {lb.kind === "video" ? (
        <video
          src={lb.url}
          controls
          autoPlay
          className="max-w-[92vw] max-h-[88vh] rounded-lg"
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <img
          src={lb.url}
          alt=""
          draggable={false}
          className="max-w-[92vw] max-h-[88vh] rounded-lg select-none"
          style={{
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
            cursor: panRef.current ? "grabbing" : "grab",
            transition: panRef.current ? "none" : "transform 0.08s ease-out",
          }}
          onMouseDown={(e) => {
            e.preventDefault();
            panRef.current = { sx: e.clientX, sy: e.clientY, ox: offset.x, oy: offset.y };
          }}
          onDoubleClick={() => {
            setScale(1);
            setOffset({ x: 0, y: 0 });
          }}
        />
      )}
    </div>
  );
}
