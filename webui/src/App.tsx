import { lazy, Suspense, useEffect } from "react";
import { useApp } from "./store/app";
import Hud from "./components/Hud";
import TaskRail from "./components/TaskRail";
import PlanStrip from "./components/PlanStrip";
import Timeline from "./components/Timeline";
import Composer from "./components/Composer";
import WorkspacePanel from "./components/WorkspacePanel";
import { ResizeHandle, useResizable } from "./lib/useResizable";

// 域页懒加载：不打进主 bundle，切到对应域才拉。
const MemoryView = lazy(() => import("./views/MemoryView"));
const SkillsView = lazy(() => import("./views/SkillsView"));
const SystemView = lazy(() => import("./views/SystemView"));

function DomainFallback() {
  return <div className="flex-1 flex items-center justify-center text-mc-faint text-sm">加载中…</div>;
}

export default function App() {
  const boot = useApp((s) => s.boot);
  const authFetched = useApp((s) => s.authFetched);
  const view = useApp((s) => s.view);
  // 左右侧栏自由缩放（2026-06-12 用户点名），宽度持久化。
  const rail = useResizable({ key: "rail", defaultWidth: 224, min: 170, max: 420 });
  const ws = useResizable({ key: "workspace", defaultWidth: 320, min: 240, max: 640, invert: true });

  useEffect(() => {
    boot();
    // boot 自带 pair 重试；StrictMode 双调用由 connectFor 内部 close 旧连接兜底。
  }, [boot]);

  return (
    <div className="h-full flex flex-col">
      <Hud />
      <div className="flex-1 flex min-h-0">
        <TaskRail width={rail.width} />
        <ResizeHandle onMouseDown={rail.onMouseDown} onDoubleClick={rail.reset} />
        {view === "tasks" ? (
          <>
            <main className="flex-1 flex flex-col min-w-0">
              <PlanStrip />
              {authFetched ? (
                <Timeline />
              ) : (
                <div className="flex-1 flex items-center justify-center text-mc-faint text-sm">
                  正在初始化…
                </div>
              )}
              <Composer />
            </main>
            <ResizeHandle onMouseDown={ws.onMouseDown} onDoubleClick={ws.reset} />
            <WorkspacePanel width={ws.width} />
          </>
        ) : (
          <main className="flex-1 flex flex-col min-w-0">
            <Suspense fallback={<DomainFallback />}>
              {view === "memory" && <MemoryView />}
              {view === "skills" && <SkillsView />}
              {view === "system" && <SystemView />}
            </Suspense>
          </main>
        )}
      </div>
    </div>
  );
}
