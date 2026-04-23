# Epic Salvage Triage — 2026-04-24

One-off analysis: point-in-time assessment of how close the unshipped
work for Epic #1 / #8 / #17 is to today's `main` IR. Archived (not
living doc) because it will drift the moment any of those Epics starts
landing — re-scan don't re-read.

Context: a 99-commit backlog from a pre-merge dirty main worktree was
landed as PR #1 + #2. This triage asks: for in-flight / branched WIP
on the three still-⬜ Epics below, would reviving it be **adapt**
(surface-level renames, same shape) or **rewrite** (scope built
against an IR that's unrecognizable now)?

The question matters for planning — a "rewrite" verdict means "schedule
as if you have no head start"; an "adapt" verdict means "1-2 PRs of
renames then reuse the logic."

---

## Epic #1 — Channel SDK → REWRITE

**Landing zone today**:
- `xmclaw/providers/channel/base.py` defines `ChannelAdapter` ABC +
  frozen `InboundMessage` / `OutboundMessage` dataclasses.
- `xmclaw/providers/channel/ws.py` is the only production adapter.
- `xmclaw/daemon/agent_loop.py` has **zero channel integration** —
  connects to LLM + tool providers only.

**Friction**:

- Message contract is frozen-dataclass with explicit fields
  (`channel`, `ref`, `content`, `raw`, `reply_to`, `attachments`).
  Old WIP referencing `user_id` / `timestamp` fields breaks.
- No `ChannelManager` multiplex layer today — daemon wires exactly
  one channel (WS). Old code that bolted channels inline on
  `AgentLoop` has nowhere to land; a new manager abstraction has to
  exist first.
- No event ↔ channel bridge. `AgentLoop` publishes to
  `InProcessEventBus`; nothing translates `USER_MESSAGE` events into
  `OutboundMessage`. Salvage code predates the v2 IR here.
- No CLI surface (`xmclaw channels {list,enable,configure}`).

**Verdict**: rewrite. Adapter ABC exists but the downstream wiring a
real channel needs — manager, event bridge, CLI — is entirely absent.
Surface-level rename gets nowhere.

---

## Epic #8 — Skill Hub → ADAPT

**Landing zone today**:
- `xmclaw/skills/registry.py` — versioned in-memory store with
  promotion/rollback audit. Solid.
- `xmclaw/skills/base.py` — `Skill` ABC.
- `xmclaw/skills/manifest.py` — `SkillManifest` with permissions /
  resource limits.
- **Zero remote code** — no `skill_hub.py`, no HTTP client, no
  download / install / signature-verify path.

**Friction**:

- Remote search / install simply doesn't exist. Salvage code that
  called `hub.search()` / `registry.install_from_hub()` errors
  immediately, but the fix is "add the missing layer," not "rewrite
  the registry."
- `SkillManifest` needs a `signature: str | None` field (Epic #16
  pre-req). Additive — doesn't invalidate existing manifests.
- No YAML-frontmatter → SkillManifest parser. agentskills.io / Claude
  Code `SKILL.md` format is the target. Salvage code that hard-coded
  JSON or pickle paths needs rewrite, but it's a contained layer.
- Per-workspace scoping: registry is global to the `AgentLoop` today.
  When Epic #17 lands, scoping question resurfaces — but that's a
  future problem, not current friction.

**Verdict**: adapt. The registry + manifest + base ABC are compatible
with what salvage code would have targeted. The work is additive
(hub client + YAML parser + signature field), not foundational.

---

## Epic #17 — Multi-Agent HTTP-to-Self → REWRITE

**Landing zone today**:
- `xmclaw/daemon/app.py` — `/agent/v2/{session_id}` WebSocket
  endpoint, `app.state.agent` (singular).
- `xmclaw/daemon/agent_loop.py` — `agent_id` parameter already on
  `AgentLoop.__init__` (default `"agent"`). Events carry `agent_id`.
  Forward-compatible at the IR layer.
- **Zero multi-agent plumbing** — no `Workspace` class, no
  `MultiAgentManager`, no `X-Agent-Id` routing middleware.

**Friction**:

- Session keying is flat: `session_id: str` at WS level. Multi-agent
  needs `{agent_id}:{session_uuid}`. A `dict[str, AgentLoop]`
  side-registry won't drop in — current app is `app.state.agent`
  singular.
- `Workspace` class (bundles AgentLoop + MemoryManager +
  SkillRegistry + ChannelManager per agent) doesn't exist.
- No middleware for `X-Agent-Id` header routing. Anything that
  patched `app.state.agent` assignment is dead on arrival with
  multiple agents.
- `core/session/lifecycle.py` tracks sessions but not agent IDs.
  Extending it is fine (allowed within `core/`), but salvage code
  that wedged agent IDs into session-only APIs won't line up.
- No `chat_with_agent` / `submit_to_agent` inter-agent tools in
  `xmclaw/providers/tool/builtin.py`.

**Verdict**: rewrite. The IR is forward-compatible (`agent_id` on
events), but `daemon/app.py` + the lifecycle machinery assume exactly
one agent. Anything beyond the event schema needs to be built
fresh — `Workspace`, `MultiAgentManager`, middleware, inter-agent
tools. Call it a rewrite and budget accordingly.

---

## One-line planning advice

- **Schedule Epic #17 first** if multi-agent is genuinely wanted — it
  blocks any "per-workspace" scope decision for Epic #8 (skill
  registry) and Epic #1 (channel manager). Designing those without
  the Workspace shape locked in risks a second rewrite.
- **Epic #8 is the cheapest** of the three because the core data
  model already exists; it's the only one where "adapt" applies.
- **Epic #1 will want to wait for Epic #17's Workspace** — channels
  naturally live on a Workspace (each agent has its own channels)
  rather than globally. Building a global `ChannelManager` first and
  then shoehorning it per-workspace is the kind of churn the
  project's 开发纪律 explicitly tries to prevent.
