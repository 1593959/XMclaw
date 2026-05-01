// XMclaw — Agent picker (B-133)
//
// Two helpers extracted from app.js to keep the main bundle under the
// 500-line budget:
//
//   fetchAgentsForPicker(store, token) — pull /api/v2/agents into
//     state.session.agents so the chat header dropdown can render the
//     ready non-evolution sub-agents.
//   switchAgentAction(store, agentId, persistActiveAgentId, connectFor)
//     — change the active sub-agent: write to store + localStorage,
//     reconnect the WS so the daemon routes turns to the chosen agent.

export async function fetchAgentsForPicker(store, token) {
  try {
    const url = "/api/v2/agents" + (token ? `?token=${encodeURIComponent(token)}` : "");
    const r = await fetch(url);
    if (!r.ok) return;
    const d = await r.json();
    const agents = (d?.agents || []).filter(
      (a) => a.kind !== "evolution" && a.ready !== false,
    );
    store.setState((s) => ({
      session: { ...s.session, agents },
    }));
  } catch (_) {
    /* picker will just stay empty — chat still works */
  }
}

export function switchAgentAction(store, agentId, persistActiveAgentId, connectFor) {
  const safe = agentId || "main";
  store.setState((s) => ({
    session: { ...s.session, activeAgentId: safe },
  }));
  persistActiveAgentId(safe);
  const sid = store.getState().session.activeSid;
  const token = store.getState().auth.token;
  if (sid) connectFor(sid, token, safe);
}
