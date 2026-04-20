# Workspace

The **workspace** is the user's window onto *who this agent is* and
what it's working on. It spans the entire agent directory — identity
files (SOUL.md, PROFILE.md, AGENTS.md), agent-level state
(tasks.json), and the `workspace/` subfolder where the agent keeps
task-level scratch (plan.md, notes.md, decisions.md).

Files that would leak secrets or expose daemon internals are hidden
by a hard exclude list on the daemon — the frontend can neither list
nor read them.

## Layout

```
agents/<agent_id>/
├── SOUL.md            ← visible · personality / character
├── PROFILE.md         ← visible · who the user is
├── AGENTS.md          ← visible · multi-agent team config
├── tasks.json         ← visible · managed by `task` tool
├── agent.json         ← hidden  · carries API keys
├── agent.example.json ← hidden  · template, noise
├── memory/            ← hidden  · SQLite DB + session logs
├── __pycache__/       ← hidden  · python bytecode
└── workspace/
    ├── plan.md        ← current multi-step plan
    ├── notes.md       ← freeform scratchpad
    ├── decisions.md   ← append-only log of non-obvious choices
    ├── tasks.json     ← task-tool managed
    └── todos.json     ← todo-tool managed
```

Only `workspace/tasks.json` and `workspace/todos.json` are
pre-created by the agent tooling. The rest appear on demand — an
empty `workspace/` on a fresh agent is correct.

## Exclude list

Defined in [`xmclaw/daemon/server.py`](../xmclaw/daemon/server.py)
as `_WORKSPACE_EXCLUDE_NAMES`. Any name in the list, anywhere in the
path, is dropped from the tree *and* rejected by the file-CRUD
endpoints (`read_file`, `write_file`, `create_file`, `delete_file`,
`rename_file`). Dotfiles are excluded by the same mechanism.

| Name | Why hidden |
|---|---|
| `agent.json` | Carries the LLM API key. Leaking it via the web UI would be a credential disclosure. |
| `agent.example.json` | Template file, adds noise with no user value. |
| `memory/` | SQLite + vector store + session transcripts. Owned by the daemon; manual edits corrupt the DB. |
| `__pycache__/` | Python bytecode. |
| `.*` | Dotfiles (`.git`, `.venv`, `.DS_Store`). |

## File semantics

Files the agent is instructed to create and maintain, in its system
prompt (see `prompt_builder.py`):

| File | Owner | Purpose |
|---|---|---|
| `SOUL.md` | User + Agent | Personality, values, voice. Agent reads this every turn; user edits out-of-band to shape behavior. |
| `PROFILE.md` | User + Agent | User's context: name, preferences, domain knowledge. Edits are collaborative. |
| `AGENTS.md` | User | Multi-agent team roster + delegation rules. |
| `workspace/plan.md` | Agent | Current multi-step plan. Agent writes this *before* executing a medium/high-complexity task so the user can redirect before work starts. Overwritten per task. |
| `workspace/notes.md` | Agent | Freeform scratchpad for cross-turn thinking that shouldn't pollute the chat. |
| `workspace/decisions.md` | Agent | Append-only log of non-obvious choices ("picked X over Y because Z"). Breadcrumbs for future sessions. |
| `workspace/todos.json` | `todo` tool | Checklist. |
| `workspace/tasks.json` | `task` tool | Longer-running task tracker. |

## Frontend

The 工作区 view in the Web UI reads / writes via:

- `GET  /api/agent/<id>/files` — flat list, already filtered through the exclude list
- `GET  /api/agent/<id>/file?path=...` — read one file (rejected if excluded)
- `POST /api/agent/<id>/file?path=...` — write one file (rejected if excluded)
- `POST /api/agent/<id>/file/create`, `DELETE /api/agent/<id>/file`, `POST /api/agent/<id>/file/rename`

All endpoints apply the same `_is_excluded` check *before* the
path-traversal guard, so even a valid in-tree path to `agent.json`
or `memory/sessions/*.json` returns 403.

Regression tested in
[`tests/test_integration.py::test_workspace_files_api_shows_identity_hides_secrets`](../tests/test_integration.py).
