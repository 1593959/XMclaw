// 顶层错误边界（对位旧 UI B-223）。没有它，任何渲染崩溃 → React 卸载
// 整棵树 → 用户只看到 #0b0e14 背景的"黑屏"（2026-06-12 用户实际撞上）。
// 崩溃时显示错误堆栈 + 恢复操作，并把 window error/unhandledrejection
// 也兜进来（lazy chunk 加载失败、boot 异步炸等渲染外错误）。

import { Component, type ReactNode } from "react";

interface State {
  err: Error | null;
}

export default class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { err: null };

  static getDerivedStateFromError(err: Error): State {
    return { err };
  }

  componentDidCatch(err: Error, info: unknown) {
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
    // chunk 加载失败（构建替换/缓存错位）→ 自动整页刷新一次即恢复。
    if (/Failed to fetch dynamically imported module|Importing a module script failed/.test(msg)) {
      const key = "mc.chunkReloadAt";
      const last = Number(sessionStorage.getItem(key) || 0);
      if (Date.now() - last > 10_000) {
        sessionStorage.setItem(key, String(Date.now()));
        window.location.reload();
      }
    }
  };

  render() {
    const { err } = this.state;
    if (!err) return this.props.children;
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
