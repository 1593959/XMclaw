// 顶层错误边界（对位旧 UI B-223）。没有它，任何渲染崩溃 → React 卸载
// 整棵树 → 用户只看到 #0b0e14 背景的"黑屏"（2026-06-12 用户实际撞上）。
// 崩溃时显示错误堆栈 + 恢复操作，并把 window error/unhandledrejection
// 也兜进来（lazy chunk 加载失败、boot 异步炸等渲染外错误）。

import { Component, type ReactNode } from "react";

interface State {
  err: Error | null;
}

// chunk 加载失败 = 部署替换了 hash 命名的 code-split 产物，但标签页还引用
// 旧 hash（已被删）→ 动态 import 404。不是真 bug，自动刷新一次即恢复。
function isChunkError(msg: string): boolean {
  return /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(
    msg,
  );
}

function reloadOnce(): boolean {
  // 10s 内只刷一次，防极端情况下的死循环刷新。daemon 给 index.html 设了
  // no-store（app.py:1883），所以普通 reload 必拿到新壳 + 新 chunk 引用，
  // 无需 cache-bust。
  const key = "mc.chunkReloadAt";
  const last = Number(sessionStorage.getItem(key) || 0);
  if (Date.now() - last > 10_000) {
    sessionStorage.setItem(key, String(Date.now()));
    window.location.reload();
    return true;
  }
  return false;
}

export default class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { err: null };

  static getDerivedStateFromError(err: Error): State {
    return { err };
  }

  componentDidCatch(err: Error, info: unknown) {
    // 渲染期动态 chunk 加载失败（如切到 TeamView 等懒加载视图，旧 hash 已被
    // 新构建删除）→ 之前直接弹崩溃页。改为自动刷新一次拿新产物。
    if (isChunkError(String(err?.message || err))) {
      if (reloadOnce()) return;
    }
    console.error("[mc] App-level crash:", err, info);
  }

  componentDidMount() {
    // 渲染树之外的致命错误（动态 chunk 404、boot 未捕获异常）同样兜住。
    window.addEventListener("unhandledrejection", this.onRejection);
  }

  componentWillUnmount() {
    window.removeEventListener("unhandledrejection", this.onRejection);
  }

  onRejection = (e: PromiseRejectionEvent) => {
    const msg = String(e.reason?.message || e.reason || "");
    if (isChunkError(msg)) reloadOnce();
  };

  render() {
    const { err } = this.state;
    if (!err) return this.props.children;
    // stale-chunk：componentDidCatch 已触发自动刷新，这里显示友好过渡页而非
    // 吓人的崩溃堆栈（刷新在飞，马上就好）。
    if (isChunkError(String(err.message || err))) {
      return (
        <div className="min-h-screen flex flex-col items-center justify-center gap-3 bg-mc-bg text-mc-muted">
          <div className="w-8 h-8 rounded-full border-2 border-mc-accent/30 border-t-mc-accent animate-spin" />
          <div className="text-sm">检测到新版本，正在自动刷新…</div>
          <button
            onClick={() => window.location.reload()}
            className="text-xs text-mc-faint hover:text-mc-accent cursor-pointer underline"
          >
            没反应？点这里手动刷新
          </button>
        </div>
      );
    }
    return (
      <div className="min-h-screen p-8 bg-mc-bg text-mc-text font-mono">
        <h1 className="text-mc-err text-lg font-semibold mb-2">XMclaw UI 渲染崩溃</h1>
        <p className="text-mc-muted text-sm mb-4">
          页面树抛出未捕获错误 — 这是前端 bug，不是 daemon 问题。把下面的堆栈截给开发者可精确定位。
        </p>
        <pre className="bg-mc-panel2 border border-mc-border rounded-md p-4 text-xs text-mc-err/90 whitespace-pre-wrap break-all max-h-[50vh] overflow-y-auto">
          {String(err.message || err)}
          {"\n\n"}
          {String(err.stack || "").slice(0, 4000)}
        </pre>
        <div className="flex gap-2 mt-4">
          <button
            onClick={() => window.location.reload()}
            className="px-4 py-2 rounded-md bg-mc-accent text-white text-sm cursor-pointer"
          >
            重新加载
          </button>
          <button
            onClick={() => {
              try {
                localStorage.clear();
              } catch {
                /* ignore */
              }
              window.location.reload();
            }}
            className="px-4 py-2 rounded-md border border-mc-err/50 text-mc-err text-sm cursor-pointer"
          >
            清空本地状态后加载
          </button>
        </div>
      </div>
    );
  }
}
