// XMclaw — NotificationPanel (dropdown notification center)
//
// Worker F (2026-06-05): Nebula prototype port.
// Fixed-position panel, top-right below header.
// Unread notifications highlighted with left border accent.

import { apiGet } from "../../lib/api.js";

const { h } = window.__xmc.preact;
const { useState, useCallback, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const ICON_MAP = {
  proactive_proposal: "🤖",
  reflection_cycle_ran: "🧠",
  memory_consolidated: "💾",
  goals_groomed: "🎯",
  metacognition_proposal: "💡",
  task_state_changed: "⚡",
  evolution_promoted: "🚀",
};

function formatRelativeTime(ts) {
  const now = Date.now() / 1000;
  const diff = now - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function NotificationPanel({ onClose, token }) {
  const [notifs, setNotifs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiGet("/api/v2/dashboard/overview", token)
      .then((data) => {
        if (cancelled) return;
        const events = data.recent_events || [];
        const mapped = events.map((event) => ({
          icon: ICON_MAP[event.type] || "📋",
          title: event.type,
          desc: event.summary,
          time: formatRelativeTime(event.ts),
          unread: true,
        }));
        setNotifs(mapped);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err.tokenNotReady) {
          setLoading(true);
          return;
        }
        setError(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const markAllRead = useCallback(() => {
    setNotifs((prev) => prev.map((n) => ({ ...n, unread: false })));
  }, []);

  return html`
    <div class="nb-notif-panel show" role="dialog" aria-label="通知中心">
      <div class="nb-notif-header">
        <h3>Notifications</h3>
        <button type="button" onClick=${markAllRead}>Mark all read</button>
      </div>
      <div class="nb-notif-list">
        ${loading &&
        html`
          <div class="nb-notif-item">
            <div class="nb-notif-content">
              <div class="nb-notif-desc">Loading...</div>
            </div>
          </div>
        `}
        ${error &&
        html`
          <div class="nb-notif-item">
            <div class="nb-notif-content">
              <div class="nb-notif-desc">Failed to load notifications</div>
            </div>
          </div>
        `}
        ${!loading &&
        !error &&
        notifs.map(
          (n, idx) => html`
            <div
              class=${"nb-notif-item " + (n.unread ? "unread" : "")}
              key=${idx}
            >
              <div class="nb-notif-icon">${n.icon}</div>
              <div class="nb-notif-content">
                <div class="nb-notif-title">${n.title}</div>
                <div class="nb-notif-desc">${n.desc}</div>
                <div class="nb-notif-time">${n.time}</div>
              </div>
            </div>
          `
        )}
      </div>
    </div>
  `;
}
