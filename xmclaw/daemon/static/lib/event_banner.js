// XMclaw — global event-banner layer (Iteration 2)
//
// Polls /api/v2/events for system-level events and surfaces them as
// toasts so the user knows when things happen behind the scenes.
//
// Events consumed:
//   skill_promoted  → toast.success("🎉 技能已自动升级: X")
//   config_reloaded → toast.info("⚙️ 配置已热更新")
//
// Persistence: last_seen timestamp stored in localStorage so a page
// reload doesn't replay historical events.

import { apiGet } from "./api.js";
import { toast } from "./toast.js";

const EVENT_TYPES = ["skill_promoted", "config_reloaded"];
const POLL_MS = 6000;
const LS_KEY = "xmc.eb.last_ts";

function _readLastTs() {
  try {
    return parseFloat(localStorage.getItem(LS_KEY) || "0");
  } catch (_) {
    return 0;
  }
}

function _writeLastTs(ts) {
  try {
    localStorage.setItem(LS_KEY, String(ts));
  } catch (_) {
    /* private mode — skip */
  }
}

function _handle(ev) {
  switch (ev.type) {
    case "skill_promoted": {
      const skillId = ev.payload?.skill_id || ev.payload?.candidate_id || "unknown";
      toast.success(`🎉 技能已自动升级：${skillId}`);
      break;
    }
    case "config_reloaded":
      toast.info("⚙️ 配置已热更新");
      break;
  }
}

export function startEventBanner(token) {
  if (!token) return () => {};
  let cancelled = false;

  async function tick() {
    if (cancelled) return;
    try {
      const lastTs = _readLastTs();
      const since = lastTs > 0 ? lastTs + 0.001 : Date.now() / 1000 - 300;
      const types = encodeURIComponent(EVENT_TYPES.join(","));
      const data = await apiGet(`/api/v2/events?types=${types}&since=${since}&limit=20`, token);
      if (cancelled) return;
      const events = data.events || [];
      let maxTs = lastTs;
      for (const ev of events) {
        if (ev.ts <= lastTs) continue;
        _handle(ev);
        if (ev.ts > maxTs) maxTs = ev.ts;
      }
      if (maxTs > lastTs) {
        _writeLastTs(maxTs);
      }
    } catch (_) {
      // fail silent — event banner is best-effort
    }
  }

  tick();
  const id = setInterval(tick, POLL_MS);
  return () => {
    cancelled = true;
    clearInterval(id);
  };
}
