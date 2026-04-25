# Security Policy

XMclaw is a local-first runtime that executes real tools (filesystem, shell, browser, MCP) on the user's machine. Security issues — especially anything that **lets a remote attacker influence the agent's tool calls** — are taken seriously.

## Supported versions

XMclaw is in **development preview** (`2.0.0.dev*`). Until a `1.0` release ships, only the **current `main`** branch and the **latest tagged release** receive security fixes. There is no LTS branch.

| Version          | Security fixes |
|------------------|----------------|
| `main`           | ✅              |
| Latest tag       | ✅              |
| Older `dev*` / pre-1.0 tags | ❌              |

## Reporting a vulnerability

Please **do not** open a public GitHub issue or discussion. Instead:

1. Use **GitHub's private vulnerability disclosure**:
   <https://github.com/1593959/XMclaw/security/advisories/new>
2. If that is not available to you, email a maintainer with `[XMclaw security]` in the subject (contact via the GitHub profile listed in `pyproject.toml`).

In your report, please include:

- A clear description of the issue and its impact (what an attacker can achieve).
- A proof-of-concept (minimal reproduction, redacted of any real secrets).
- Affected component(s): daemon / CLI / web UI / a specific tool provider / a specific event type.
- Affected XMclaw version and commit SHA.
- Your assessment of severity (informational / low / medium / high / critical).

We aim to:

- **Acknowledge** within 5 business days.
- **Triage** (confirm + severity) within 14 days.
- **Patch + advisory** within 90 days for high/critical issues. Lower-severity issues are bundled into the next regular release.

If a fix requires a coordinated disclosure (e.g. a third-party MCP server is also vulnerable), we'll discuss timelines with you before publishing.

## Out of scope

The following are **expected behaviour** and not security bugs in XMclaw itself — though we welcome reports if you can demonstrate amplification or escape:

- The model issuing destructive tool calls (`bash rm -rf …`, `file_write` overwriting user files) when **no sandbox is configured**. The default posture is **fully open**; users are expected to set `tools.allowed_dirs` or run with `tools.enable_bash: false` if untrusted prompts can reach the agent. `xmclaw doctor` flags this.
- Prompt injection in tool output altering the model's reply text. The `xmclaw.security.prompt_scanner` raises `PROMPT_INJECTION_DETECTED` events; the policy (`detect_only` / `redact` / `block`) is user-configurable in `daemon/config.json`.
- Resource exhaustion driven by the user's own prompts. The agent loop has a `cost_budget_usd` cap and per-call timeouts; choose responsible values.
- LLM provider data sharing. When using a cloud provider, your prompts leave the machine. Use local models or self-hosted endpoints if that is unacceptable.

In scope (please report):

- **Auth bypass** on `/api/v2/*` or `/agent/v2/*` (pairing-token check failure, timing leak, replay).
- **Path traversal** allowing tools to read/write outside `tools.allowed_dirs` when a sandbox **is** configured.
- **Prompt-injection scanner bypass** (an attack string the scanner misses but a trivial variant catches).
- **Secret leakage** in events, logs, or the UI — even after `xmclaw.utils.redact` runs.
- **Subprocess escape** from a SkillRuntime sandbox or MCP bridge worker into the daemon's heap.
- **Code execution** via crafted config / events.db / memory.db files from another user's workspace.
- **CSRF / cross-origin** abuse of the local daemon endpoints.

## Hardening tips for users

- Run `xmclaw doctor` on every install. It checks pairing token mode, port, secrets file mode, and config sanity.
- Set `tools.allowed_dirs` in `daemon/config.json` to restrict filesystem tools to a whitelist.
- Set `security.prompt_injection: "redact"` (or `"block"`) if your agent ingests untrusted tool output (web fetches, third-party MCP servers).
- Keep `daemon/config.json` mode `0600` (it is already gitignored).
- Use `xmclaw config set-secret <name>` to keep API keys out of the config file.
- For multi-agent / shared deployments, do **not** expose the daemon beyond `127.0.0.1`. Run a reverse proxy with proper auth in front of it.

## Acknowledgements

We will credit security reporters in the release notes unless you ask to remain anonymous. Thank you for helping keep XMclaw safe.
