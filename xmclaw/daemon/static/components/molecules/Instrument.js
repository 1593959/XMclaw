// XMclaw — Instrument.js 「仪表台」组件 kit  (2026-06-05)
//
// 用户拍板的新表现形态：活体控制台。这些是可复用的真·仪表组件，
// 给数据页（Dashboard / Memory / Evolution / Cognition …）挂在顶部
// 作为「vitals strip」或面板内读数。样式在 styles/instrument.css。
//
// 导出：
//   Readout    — 等宽大读数（数字 + 单位 + 可选 delta）
//   Gauge      — SVG 环形仪表（0-100 或比例）
//   Sparkbar   — 迷你波形（活体脉动）
//   Meter      — 比例条（label ▓▓░░ value）
//   Vitals     — 顶部读数条容器（自动网格）
//   SecLabel   — 仪表面板内的小节刻度标题

const { h } = window.__xmc.preact;
const { useState, useEffect, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

function fmtNum(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (typeof n === "string") return n;
  return n.toLocaleString();
}

// 小巧思：读数从上一次的值「滚动」到新值（仪表开机/刷新的仪式感）。
// 仅对纯数字生效；字符串(embedder名/$花费)原样显示。尊重 reduced-motion。
const _reduceMotion = (() => {
  try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
  catch (_) { return false; }
})();
function useCountUp(target) {
  const isNum = typeof target === "number" && Number.isFinite(target);
  const [val, setVal] = useState(isNum ? (_reduceMotion ? target : 0) : target);
  const ref = useRef({ raf: 0, last: isNum ? 0 : target });
  useEffect(() => {
    if (!isNum || _reduceMotion) { setVal(target); ref.current.last = target; return undefined; }
    const from = typeof ref.current.last === "number" ? ref.current.last : 0;
    const to = target;
    if (from === to) { setVal(to); return undefined; }
    const dur = 600;
    const t0 = (typeof performance !== "undefined" ? performance.now() : Date.now());
    const tick = (now) => {
      const p = Math.min(1, ((now || Date.now()) - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      const cur = Math.round(from + (to - from) * eased);
      setVal(cur); ref.current.last = cur;
      if (p < 1) ref.current.raf = requestAnimationFrame(tick);
      else { setVal(to); ref.current.last = to; }
    };
    cancelAnimationFrame(ref.current.raf);
    ref.current.raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(ref.current.raf);
  }, [target, isNum]);
  return isNum ? val : target;
}

/** 等宽读数。props: { value, unit, label, delta, deltaDir } */
export function Readout({ value, unit, label, delta, deltaDir }) {
  const shown = useCountUp(value);
  return html`
    <div class="xi-readout">
      ${label ? html`<span class="xi-readout__label">${label}</span>` : null}
      <span class="xi-readout__row">
        <span class="xi-readout__num">${fmtNum(shown)}</span>
        ${unit ? html`<span class="xi-readout__unit">${unit}</span>` : null}
        ${delta != null
          ? html`<span class=${"xi-readout__delta " + (deltaDir || (delta >= 0 ? "up" : "down"))}>
              ${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta)}
            </span>`
          : null}
      </span>
    </div>
  `;
}

/**
 * SVG 环形仪表。props:
 *   value 0..max（默认 max=100）、size、stroke、label（圆心可省略显示百分数）
 */
export function Gauge({ value = 0, max = 100, size = 54, stroke = 5, showVal = true, suffix = "" }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(1, max ? value / max : 0));
  const offset = c * (1 - pct);
  const mid = size / 2;
  return html`
    <span class="xi-gauge" style=${`width:${size}px;height:${size}px`}>
      <svg width=${size} height=${size} viewBox=${`0 0 ${size} ${size}`} aria-hidden="true">
        <defs>
          <linearGradient id="xi-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="var(--nb-accent, #8B5CF6)" />
            <stop offset="100%" stop-color="var(--nb-cyan, #06B6D4)" />
          </linearGradient>
        </defs>
        <circle class="track" cx=${mid} cy=${mid} r=${r} fill="none" stroke-width=${stroke} />
        <circle
          class="fill"
          cx=${mid} cy=${mid} r=${r}
          fill="none"
          stroke-width=${stroke}
          stroke-dasharray=${c}
          stroke-dashoffset=${offset}
          transform=${`rotate(-90 ${mid} ${mid})`}
        />
      </svg>
      ${showVal ? html`<span class="xi-gauge__val">${Math.round(pct * 100)}${suffix || "%"}</span>` : null}
    </span>
  `;
}

/** 迷你波形。props: { bars=[h..], live, height } — bars 是 0..1 或像素高度数组 */
export function Sparkbar({ bars, live = false, height = 20 }) {
  const data = Array.isArray(bars) && bars.length
    ? bars
    : [0.4, 0.7, 0.5, 0.9, 0.6, 1, 0.5, 0.8];
  const max = Math.max(...data, 1);
  return html`
    <span class=${"xi-spark" + (live ? " live" : "")} style=${`height:${height}px`} aria-hidden="true">
      ${data.map((v) => html`<i style=${`height:${Math.max(2, (v / max) * height)}px`}></i>`)}
    </span>
  `;
}

/** 比例条。props: { label, value, max, display } */
export function Meter({ label, value = 0, max = 100, display }) {
  const pct = Math.max(0, Math.min(100, max ? (value / max) * 100 : 0));
  return html`
    <div class="xi-meter">
      ${label ? html`<span class="xi-meter__label">${label}</span>` : null}
      <span class="xi-meter__track"><span class="xi-meter__fill" style=${`width:${pct}%`}></span></span>
      <span class="xi-meter__val">${display != null ? display : value}</span>
    </div>
  `;
}

/** 顶部读数条容器。children = 一组 Vitals.Cell */
export function Vitals({ children }) {
  return html`<div class="xi-vitals">${children}</div>`;
}

/** 单格读数 + 可选图标/波形/仪表 slot */
export function VitalsCell({ icon, children }) {
  return html`
    <div class="xi-vitals__cell">
      ${icon ? html`<span class="xi-vitals__icon">${icon}</span>` : null}
      <div style="flex:1;min-width:0">${children}</div>
    </div>
  `;
}

/** 仪表面板内的小节刻度标题 */
export function SecLabel({ children }) {
  return html`<div class="xi-seclabel">${children}</div>`;
}
