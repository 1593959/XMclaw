// XMclaw — ClawHUD: Heartbeat Telemetry Status Bar
//
// Signature "alive" HUD for the claw theme. Displays real-time daemon
// vitals: heartbeat, memory facts count, skills count, evolution sparkline,
// and autonomy score.
//
// Mirrors the mockup G "变体1 状态栏" design.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

// ClawMark icon (same path as AppShell ICONS.ClawMark)
const CLAW_SVG_PATH = "M6 21 C7 13 7 7 5 3 M12 22 C13 13 13 6 11 2 M18 21 C19 13 19 7 17 3";

function ClawIcon({ size = 16, className = "" }) {
  return html`
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2.4"
      stroke-linecap="round"
      class=${"claw-mark " + className}
      aria-hidden="true"
    >
      <path d=${CLAW_SVG_PATH} />
    </svg>
  `;
}

function Sparkline() {
  // 6 animated bars mimicking an audio waveform / activity sparkline
  return html`
    <span class="xmc-claw-spark" aria-hidden="true">
      <i style="height:5px"></i>
      <i style="height:9px"></i>
      <i style="height:6px"></i>
      <i style="height:12px"></i>
      <i style="height:8px"></i>
      <i style="height:13px"></i>
    </span>
  `;
}

/**
 * @param {Object} props
 * @param {string} [props.status]      - Daemon status: "alive" | "thinking" | "idle" | "error"
 * @param {number} [props.memoryFacts] - Number of memory facts
 * @param {number} [props.skillCount]  - Number of loaded skills
 * @param {number} [props.skillPending]- Number of pending skill candidates
 * @param {number} [props.autonomy]    - Autonomy score 0-100
 * @param {boolean} [props.compact]    - Compact mode (fewer segments)
 */
export function ClawHUD({
  status = "alive",
  memoryFacts = 0,
  skillCount = 0,
  skillPending = 0,
  autonomy = 50,
  compact = false,
}) {
  const orbClass = status === "thinking" ? "xmc-claw-orb think" : "xmc-claw-orb";
  const statusLabel = status === "alive" ? "alive" : status;
  const statusColor = status === "error" ? "cy" : "k";

  return html`
    <div class="xmc-claw-hud" role="status" aria-label="daemon telemetry">
      <div class="xmc-claw-hud__seg">
        <span class=${orbClass}></span>
        <b>HEARTBEAT</b>
        <span class=${statusColor}>${statusLabel}</span>
      </div>

      ${!compact && html`
        <div class="xmc-claw-hud__seg">
          <span class="l">记忆</span>
          <b>${memoryFacts.toLocaleString()}</b>
          <span class="k">facts</span>
        </div>
      `}

      <div class="xmc-claw-hud__seg">
        <span class="l">技能</span>
        <b>${skillCount}</b>
        ${skillPending > 0 && html`<span class="k">+${skillPending} 候选</span>`}
      </div>

      ${!compact && html`
        <div class="xmc-claw-hud__seg">
          <span class="l">进化</span>
          <${Sparkline} />
        </div>
      `}

      <div class="xmc-claw-hud__seg xmc-claw-hud__seg--right">
        <span class="l">自主</span>
        <b>${autonomy}</b>
        <span class="l">/100</span>
      </div>
    </div>
  `;
}

/**
 * Minimal HUD for collapsed sidebar or compact contexts.
 * Shows only heartbeat + autonomy score.
 */
export function ClawHUDMini({ status = "alive", autonomy = 50 }) {
  const orbClass = status === "thinking" ? "xmc-claw-orb think" : "xmc-claw-orb";
  return html`
    <div class="xmc-claw-hud" role="status" style="margin:8px 14px 0;padding:6px 12px;font-size:10px;">
      <div class="xmc-claw-hud__seg" style="padding:6px 10px;gap:6px;">
        <span class=${orbClass} style="width:6px;height:6px;"></span>
        <span class="k">${status}</span>
      </div>
      <div class="xmc-claw-hud__seg xmc-claw-hud__seg--right" style="padding:6px 10px;">
        <span class="l">A</span><b>${autonomy}</b>
      </div>
    </div>
  `;
}

/**
 * Thinking indicator with animated dots.
 * Used inside message bubbles when assistant is processing.
 */
export function ThinkingIndicator({ label = "thinking" }) {
  return html`
    <span class="xmc-thinking" aria-live="polite">
      · ${label}
      <span class="dot">.</span>
      <span class="dot">.</span>
      <span class="dot">.</span>
    </span>
  `;
}

/**
 * Claw separator — visual divider between conversation turns.
 * Features the claw mark motif + repeating line pattern.
 */
export function ClawSeparator({ label = "CLAW" }) {
  return html`
    <div class="xmc-claw-sep" aria-hidden="true">
      <span>${label}</span>
      <div class="xmc-claw-sep__scr"></div>
      <span>本轮</span>
    </div>
  `;
}
