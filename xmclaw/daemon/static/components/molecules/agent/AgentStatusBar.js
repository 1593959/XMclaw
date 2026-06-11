// XMclaw Agent Status Bar — live metrics strip
//
// Shows: active model · hop count · token usage · cost · elapsed time ·
// tool call count · WS connection status. Sits at top of main content area.

const { h } = window.__xmc.preact;
const { useEffect, useState, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

function fmtTokens(n) {
  if (n == null || n === 0) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function fmtDuration(sec) {
  if (!sec || sec <= 0) return "0s";
  if (sec < 60) return Math.floor(sec) + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m" + (sec % 60) + "s";
  return Math.floor(sec / 3600) + "h" + Math.floor((sec % 3600) / 60) + "m";
}

function fmtCost(usd) {
  if (!usd || usd <= 0) return "$0";
  if (usd < 0.01) return "<$0.01";
  return "$" + usd.toFixed(2);
}

export function AgentStatusBar({
  llmProfileId,
  tokenUsage,
  sessionElapsed,
  currentHop,
  toolCallCount,
  connectionStatus,
  maxHops = 40,
}) {
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef(null);

  useEffect(() => {
    if (sessionElapsed > 0) {
      setElapsed(sessionElapsed);
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
      return () => { if (timerRef.current) clearInterval(timerRef.current); };
    }
    return undefined;
  }, [sessionElapsed > 0 ? 1 : 0]);

  const profileLabel = llmProfileId || "default";
  const statusDot = connectionStatus === "connected" ? "●" : "○";
  const statusColor = connectionStatus === "connected"
    ? "var(--nb-success, #4caf50)"
    : "var(--nb-warning, #ff9800)";

  return html`
    <div class="agent-status-bar" role="status" aria-live="polite">
      <span class="agent-status-bar__item" title="Active model">
        🧠 <strong>${profileLabel}</strong>
      </span>
      <span class="agent-status-bar__sep">·</span>
      <span class="agent-status-bar__item" title="Hop ${currentHop || 0}/${maxHops}">
        ⚡ hop ${currentHop || 0}/${maxHops}
      </span>
      <span class="agent-status-bar__sep">·</span>
      <span class="agent-status-bar__item" title="Tokens: ${fmtTokens(tokenUsage?.totalTokens)} (prompt: ${fmtTokens(tokenUsage?.promptTokens)}, completion: ${fmtTokens(tokenUsage?.completionTokens)})">
        📊 ${fmtTokens(tokenUsage?.totalTokens)} tokens
      </span>
      <span class="agent-status-bar__sep">·</span>
      <span class="agent-status-bar__item" title="Estimated cost">
        💰 ${fmtCost(tokenUsage?.costUsd)}
      </span>
      <span class="agent-status-bar__sep">·</span>
      <span class="agent-status-bar__item" title="Session duration">
        ⏱ ${fmtDuration(elapsed)}
      </span>
      <span class="agent-status-bar__sep">·</span>
      <span class="agent-status-bar__item" title="Tool calls">
        🔧 ${toolCallCount || 0} calls
      </span>
      <span class="agent-status-bar__sep">·</span>
      <span class="agent-status-bar__item" style=${{ color: statusColor }}>
        ${statusDot} ${connectionStatus || "unknown"}
      </span>
    </div>
  `;
}
