# Hooks

XMclaw's hook engine lets you wire arbitrary logic into the agent's
lifecycle without editing source code. Configure hooks in
`daemon/config.json` under the `hooks` array; they fire whenever the
matching `event` happens.

Borrows the Claude Code mental model — same 25 event names, same
JSON output protocol — adapted to XMclaw's loop shape.

---

## Quick start

1. **Trust the workspace** (one-time, per directory):

   ```bash
   cd ~/my-project
   xmclaw trust
   ```

   Creates `~/my-project/.xmclaw-trust`. Without this, `command` and
   `function` hooks refuse to run (they have RCE surface).

2. **Add a hook** in `daemon/config.json`:

   ```json
   {
     "hooks": [
       {
         "id": "log-prompts",
         "event": "UserPromptSubmit",
         "runner": "command",
         "command": "tee -a ~/xmclaw-prompts.log",
         "timeout_s": 2
       }
     ]
   }
   ```

3. **Restart** the daemon:

   ```bash
   xmclaw restart
   ```

4. Send a chat message — `~/xmclaw-prompts.log` now grows with each
   user prompt (the hook receives the context JSON on stdin).

---

## Events

The 25 lifecycle moments that can carry a hook. Each one fires with
a distinct `payload` shape — your hook reads from it (the JSON the
runner gets on stdin / HTTP body / function arg).

| Event                  | When                                                | Payload keys                                |
|------------------------|-----------------------------------------------------|----------------------------------------------|
| `SessionStart`         | new chat session created                            | `session_id`                                 |
| `SessionEnd`           | session destroyed (e.g. cleared from UI)            | `session_id`                                 |
| `SessionResume`        | WS reconnect after disconnect                       | `session_id`                                 |
| `UserPromptSubmit`     | user message received, BEFORE the LLM call         | `content`, `images`, `correlation_id`        |
| `PreLLM`               | each LLM call about to start (per hop)             | `messages_count`, `hop`                      |
| `PostLLM`              | LLM call returned                                   | `prompt_tokens`, `completion_tokens`, `hop`  |
| `Stop`                 | turn about to end (last hop, no more tools)         | —                                            |
| `TurnFinished`         | turn cleanly closed                                 | `tools_used`, `hops`                         |
| `PreToolUse`           | tool about to invoke                                | `tool_name`, `args`, `call_id`               |
| `PostToolUse`          | tool returned                                       | `tool_name`, `call_id`, `ok`, `error`        |
| `ToolBlocked`          | ToolGuard / hook denied a tool call                 | `tool_name`, `reason`                        |
| `ToolFailed`           | tool errored at runtime                             | `tool_name`, `error`                         |
| `SubagentStart`        | sub-agent turn dispatched                           | `agent_id`, `task_id`                        |
| `SubagentStop`         | sub-agent turn completed                            | `agent_id`, `task_id`, `ok`                  |
| `MemoryWrite`          | persona file written / fact persisted               | `kind`, `bucket`, `target_file`              |
| `PersonaUpdated`       | `bump_prompt_freeze_generation` fired               | —                                            |
| `ChannelInbound`       | IM message arrived (Telegram / Feishu / …)          | `channel`, `chat_ref`, `content`             |
| `ChannelOutbound`      | adapter about to send                               | `channel`, `chat_ref`, `content`             |
| `PerceptObserved`      | cognitive daemon observed a new percept             | `source`, `kind`                             |
| `ReflectionRan`        | reflection cycle completed                          | `scope`, `n_reflections`                     |
| `GoalSpawned`          | goal generator added a goal                         | `goal`                                       |
| `Notification`         | outgoing user-visible note                          | `kind`, `content`                            |
| `ErrorRaised`          | error event downstream                              | `where`, `error`                             |
| `SkillInvoked`         | `skill_*` tool called                               | `skill_id`, `version`                        |
| `SkillPromoted`        | evolution promoted a skill version                  | `skill_id`, `from_version`, `to_version`     |

`payload` is always a JSON-serialisable dict — strings, numbers,
lists, nested dicts only. Use snake_case OR PascalCase for the
event field — both parse.

---

## Runners

How your hook actually executes. Pick one per hook.

### `command` — shell command

```json
{
  "id": "block-rm",
  "event": "PreToolUse",
  "runner": "command",
  "command": "python ~/scripts/guard-bash.py",
  "matchers": {"tool_name": "bash"},
  "timeout_s": 3
}
```

* The context JSON arrives on the process's **stdin**.
* Whatever the process writes to **stdout** is parsed as a
  `HookResult` JSON (or treated as plain text output when not
  parseable).
* Non-zero exit code = `continue=false` with stderr as `reason`.
* Working directory = your workspace root.
* Env vars: `XMCLAW_HOOK_EVENT` carries the event name.
* **Requires workspace trust** (`.xmclaw-trust` marker).

### `function` — Python entry point

```json
{
  "id": "redact-secrets",
  "event": "UserPromptSubmit",
  "runner": "function",
  "entry": "myhooks:redact_aws_keys",
  "timeout_s": 1
}
```

* `entry` is `"module:function"`. Module must be importable from the
  daemon's Python path (`~/.xmclaw/hooks/` is a good place — add it
  to `PYTHONPATH` or drop a `.pth` file).
* The function receives a `HookContext` and returns a `HookResult`
  (or a dict matching the JSON protocol). Async callables awaited;
  sync ones run on the default executor.
* Faster than `command` (no subprocess), best for sub-ms hooks.
* **Requires workspace trust** (same RCE surface as shell).

### `http` — webhook POST

```json
{
  "id": "remote-policy-check",
  "event": "PreToolUse",
  "runner": "http",
  "url": "https://policy.internal/agent-pretool",
  "headers": {"X-Secret": "${secret:POLICY_TOKEN}"},
  "timeout_s": 4
}
```

* Posts the context JSON to `url`, parses the JSON response as
  `HookResult`.
* Trust is NOT required — your config already named the URL.
* `${secret:NAME}` placeholders in `headers` are resolved via the
  same secrets store the LLM keys use.

### `prompt` — one-shot LLM question

```json
{
  "id": "llm-classifier",
  "event": "UserPromptSubmit",
  "runner": "prompt",
  "prompt": "Decide if this user message is on-topic for a coding agent. Reply ONLY with JSON: {\"decision\": \"allow\"} or {\"decision\": \"deny\", \"reason\": \"...\"}. Message: {payload}",
  "timeout_s": 30
}
```

* Substitutes context fields into the template:
  `{event}`, `{session_id}`, `{payload}` (JSON), `{workspace_root}`,
  `{hop}`.
* Calls the daemon's primary LLM (the one in `default_profile_id`).
* Slow + expensive — use sparingly, with a tight `matchers` clause
  if you can.

### `agent` — fire-and-forget sub-agent

```json
{
  "id": "research-side-quest",
  "event": "PostToolUse",
  "runner": "agent",
  "agent_id": "research",
  "prompt": "User just ran {payload}. Anything noteworthy I should remember about that tool?",
  "matchers": {"tool_name": "web_search"}
}
```

* Spins off a turn on another registered agent (must exist in your
  multi-agent setup).
* **Never blocks** — returns immediately with a `task_id` reference.
  Inspect later via `GET /api/v2/agent_tasks` or
  `check_agent_task(task_id)`.
* Won't vote on permission decisions (decision is always None).

---

## JSON output protocol

Whether your hook is `command`, `function`, or `http`, the output
must parse as a `HookResult` dict. All fields optional; missing →
defaults. Both camelCase and snake_case keys work.

```json
{
  "continue": true,
  "decision": "allow" | "deny" | "ask",
  "systemMessage": "added to next LLM call (PreLLM only)",
  "updatedInput": "rewrite the event payload (event-specific)",
  "output": "informational text logged + emitted on the event bus",
  "reason": "shown to operator + the model when continue is false"
}
```

| Field            | Effect                                                                                              |
|------------------|-----------------------------------------------------------------------------------------------------|
| `continue`       | `false` → block the lifecycle here. For `PreToolUse`, the tool returns a structured error to LLM.   |
| `decision`       | Vote on gate events. Multiple hooks merge with priority `deny > ask > allow`. None = abstain.       |
| `systemMessage`  | PreLLM only — appended to the system prompt for THIS LLM call (won't be cached, by design).         |
| `updatedInput`   | Rewrite the event payload. For `UserPromptSubmit` → string (new user text). PreToolUse → dict (new args). PostToolUse → str/dict (new ToolResult content).  |
| `output`         | Always logged; surfaces on the chat UI as a system note. Use this for debugging.                    |
| `reason`         | Operator-facing explanation when `continue: false`. Becomes the LLM-visible error for `PreToolUse`. |

---

## Matchers

Filter when the hook fires. Tiny language by design.

```json
{ "matchers": {"tool_name": "bash"} }                       // exact
{ "matchers": {"tool_name": ["bash", "file_write"]} }       // any-of
{ "matchers": {"channel": "telegram", "kind": "command"} }  // AND of clauses
```

* Keys are payload top-level fields.
* Values: exact-match string OR sequence (any-of).
* Missing payload key → matcher fails.
* Empty `matchers: {}` → always matches (the default).

---

## Permissions / decisions

Gate events (`UserPromptSubmit`, `PreToolUse`) honour hook decisions.
When multiple hooks return a decision, they merge with this priority:

```
deny > ask > allow
```

* `deny` → lifecycle stops; operator + LLM see `reason`.
* `ask` → routes through the approval_service (Web UI prompts user;
  not yet a hard gate, hook output is recorded).
* `allow` → continue.
* `None` (default) → abstain. Other hooks decide.

For non-gate events (PostToolUse / SessionStart / etc.), `decision`
is recorded for telemetry but doesn't change flow.

---

## Trust model

`command` and `function` runners refuse to run when the workspace
isn't marked trusted, because both can execute arbitrary local
code. The trust marker is a single file:

```
<workspace_root>/.xmclaw-trust
```

* **Add**: `xmclaw trust` (in the workspace) or `xmclaw trust /path`
* **Revoke**: `xmclaw trust --revoke /path`
* **Check**: `xmclaw trust --status /path`

The file's contents are ignored — presence is the only signal.

`http`, `prompt`, `agent` runners ignore the trust marker (their
config is fixed in your config.json so the operator already vetted
them at edit time).

---

## Examples

### Block dangerous bash commands

```python
# ~/.xmclaw/hooks/guard_bash.py
import re
from xmclaw.core.hooks import HookContext, HookResult

DANGEROUS = re.compile(r"\brm\s+-rf\s+/|:\(\)\s*\{\s*:\|:&|chmod\s+777\s+/")

def guard(ctx: HookContext) -> HookResult:
    cmd = ctx.payload.get("args", {}).get("command", "")
    if DANGEROUS.search(cmd):
        return HookResult.deny(f"blocked dangerous bash: {cmd[:80]}")
    return HookResult.allow()
```

```json
{
  "id": "guard-bash",
  "event": "PreToolUse",
  "runner": "function",
  "entry": "guard_bash:guard",
  "matchers": {"tool_name": "bash"}
}
```

### Inject a system reminder on every LLM call

```json
{
  "id": "remind-current-task",
  "event": "PreLLM",
  "runner": "function",
  "entry": "myhooks:inject_focus"
}
```

```python
def inject_focus(ctx):
    return {"systemMessage": "Current focus: ship Wave-33 by Friday."}
```

### Log every tool call to a remote sink

```json
{
  "id": "remote-audit",
  "event": "PostToolUse",
  "runner": "http",
  "url": "https://audit.example.com/tool-calls",
  "timeout_s": 1
}
```

The endpoint receives the post-call context (tool_name, ok, error).
Returning `{}` (empty JSON) is fine — no behavioural effect, pure
observation.

### Sandbox auto-approve

```json
{
  "id": "auto-approve-readonly",
  "event": "PreToolUse",
  "runner": "function",
  "entry": "myhooks:auto_approve",
  "matchers": {"tool_name": ["file_read", "list_dir", "grep_files", "glob_files"]}
}
```

```python
def auto_approve(ctx):
    return {"decision": "allow"}
```

---

## Observability

Every hook fire emits a record into the daemon's event bus. Pull
recent firings via:

```
GET /api/v2/events?types=hook_fired&limit=50
```

(planned — currently you can grep `daemon.log` for
`hook.registered`, `hook.command_failed`, `hook.runner_raised`,
etc.)

---

## Limits

* **Per-hook timeout**: default 5s; configurable via `timeout_s`.
  Timed-out hooks return a no-decision result (lifecycle continues
  but the hook's output is just the timeout note).
* **Concurrent firing**: hooks for the same event run via
  `asyncio.gather`. Number of hooks is not capped.
* **Failure isolation**: one hook raising doesn't break others — the
  error is logged and that hook contributes a no-decision result.
* **Trust scope**: workspace = `cwd` at daemon-start time. There's
  one trust marker per workspace; multiple agents in the same
  workspace share it.
