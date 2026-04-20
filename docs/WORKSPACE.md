# Workspace

The agent's **workspace** is a per-agent directory where the agent keeps
user-visible working files — plans, notes, decision logs, todo lists.
It is *not* where the agent stores identity, daemon config, session
history, or evolution artifacts; those live elsewhere (see
[ARCHITECTURE.md](ARCHITECTURE.md)).

## Location

```
agents/<agent_id>/workspace/
```

For the default single-agent install, that's
`agents/default/workspace/`. The Web UI's 工作区 view is rooted here so
daemon internals never leak into the user's view (regression test in
`tests/test_integration.py::test_workspace_files_api_excludes_daemon_internals`).

## Canonical files

These are the files the agent is instructed to create on demand. None
of them are pre-created — an empty workspace is the correct default for
a fresh agent, and the UI shows a rich empty-state card explaining
this.

| File | Purpose | Writer | Lifetime |
|---|---|---|---|
| `plan.md` | Current multi-step plan. The agent writes this *before* executing a medium/high-complexity task so the user can redirect before work starts. | `file_write` | Overwritten per task |
| `notes.md` | Freeform scratchpad. Open-ended thinking the agent wants to hold across turns without polluting the chat. | `file_write` / `file_edit` | Append-only; user may prune |
| `todos.json` | Checklist. Managed by the built-in `todo` tool, not hand-written. | `todo` tool | Persistent |
| `tasks.json` | Longer-running task tracker. Managed by the built-in `task` tool. | `task` tool | Persistent |
| `decisions.md` | Append-only log of non-obvious choices ("chose X over Y because Z"). Leaves breadcrumbs for future sessions so they don't re-litigate the same call. | `file_write` / `file_edit` | Append-only |

Anything else the user creates is also fine — the workspace is theirs.
The canonical list is what the agent is *taught* to use, not a
whitelist.

## What does NOT go in workspace

- `agent.json` — identity + API keys. Lives one level up at
  `agents/<id>/agent.json`. Gitignored.
- `memory/` — SQLite + vector store + session transcripts. Owned by the
  daemon; never hand-edit.
- `SOUL.md` / `PROFILE.md` — agent's persistent self-description.
  Committed to git; edited out-of-band by the user, not by the agent
  mid-conversation.
- Generated genes / skills — live under `shared/` at the repo root, not
  under any agent's workspace.

## Agent-facing contract

The system prompt (see [`xmclaw/core/prompt_builder.py`](../xmclaw/core/prompt_builder.py))
includes a workspace section that tells the agent:

1. Where the workspace is.
2. What the canonical files are for.
3. **When** to write to each (on plan creation, on non-obvious
   decision, etc.).

Editing the prompt's workspace section is the correct way to change
agent behavior around persistence — do not add ad-hoc file-writing
hints elsewhere.

## Frontend editing

The Web UI lets the user read every file in the workspace. Write-back
from the frontend is gated by a save action; the endpoint is
`POST /api/agent/<id>/file` (body: `{path, content}`). See
[EVENTS.md](EVENTS.md) for the file-mutation event stream that the
daemon emits when either the agent or the frontend writes.
