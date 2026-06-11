// XMclaw — 重型可视化库的共享懒加载器(Phase 9 M1)
//
// 原来 CanvasArtifact.js 和 cognition_task_dag.js 各自从 esm.sh 现拉
// mermaid / Chart.js,断网时 canvas 渲染半残,违反 local-first 原则。
// 现在收口到这里:vendor/ 本地 UMD 构建优先,加载失败才回退 esm.sh
// (镜像 bootstrap.js 的 Preact 双轨策略,只是方向相反——我们是
// 离线优先,bootstrap 是 CDN 优先)。
//
// UMD 构建挂 window 全局(window.mermaid / window.Chart),用 <script>
// 注入而非 ESM import——mermaid 的 esm.sh ESM 构建会继续从 esm.sh 拉
// 子依赖,vendor 它没意义;UMD 是真正的单文件。

const VENDOR_MERMAID = "./vendor/mermaid.min.js";
const VENDOR_CHARTJS = "./vendor/chart.umd.min.js";
const CDN_MERMAID = "https://esm.sh/mermaid@10/dist/mermaid.esm.min.mjs";
const CDN_CHARTJS = "https://esm.sh/chart.js@4/auto?standalone";

function injectScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = () => resolve();
    el.onerror = () => {
      el.remove();
      reject(new Error("script load failed: " + src));
    };
    document.head.appendChild(el);
  });
}

let _mermaidPromise = null;
export function loadMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = injectScript(VENDOR_MERMAID)
      .then(() => {
        if (!window.mermaid) throw new Error("vendor mermaid missing global");
        return window.mermaid;
      })
      .catch(() =>
        // 本地缺文件(老安装包未带 vendor)→ 回退 CDN ESM 构建。
        import(CDN_MERMAID).then((m) => m.default)
      )
      .then((mermaid) => {
        mermaid.initialize({ startOnLoad: false, theme: "dark" });
        return mermaid;
      })
      .catch((e) => {
        _mermaidPromise = null; // 失败不缓存,下次重试
        throw e;
      });
  }
  return _mermaidPromise;
}

let _chartPromise = null;
export function loadChartJs() {
  if (!_chartPromise) {
    _chartPromise = injectScript(VENDOR_CHARTJS)
      .then(() => {
        if (!window.Chart) throw new Error("vendor chart.js missing global");
        return window.Chart;
      })
      .catch(() => import(CDN_CHARTJS).then((m) => m.default))
      .catch((e) => {
        _chartPromise = null;
        throw e;
      });
  }
  return _chartPromise;
}
