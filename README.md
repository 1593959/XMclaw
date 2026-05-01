# 🦞 XMclaw

<p align="center">
  <strong>A local-first AI agent that lives on your machine, remembers across sessions, and evolves its own skills based on evidence — not on what the model thinks of itself.</strong>
</p>

<p align="center">
  <a href="https://github.com/1593959/XMclaw/actions/workflows/python-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/1593959/XMclaw/python-ci.yml?branch=main&style=for-the-badge" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge" alt="Python"></a>
  <img src="https://img.shields.io/badge/Platform-Win%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge" alt="Cross-platform">
  <img src="https://img.shields.io/badge/Status-1.0%20stable-brightgreen?style=for-the-badge" alt="1.0 stable">
</p>

XMclaw is **not a chatbot**. It is a runtime that thinks, acts, remembers, and **measures its own skill versions against hard evidence to decide what to keep**. It runs in a single Python daemon on `127.0.0.1:8765` — your data, your tools, your shell, your filesystem. Nothing leaves the box unless you ask.

Talk to it from a built-in web UI, an interactive CLI, or your own WebSocket client. Use any model — Anthropic / OpenAI / MiniMax / Moonshot / DashScope / Qwen / 本地 Ollama — switch with one config change.

[Quick Start](#-quick-start) · [What's different](#-whats-different) · [Docs](./docs) · [Architecture](./docs/ARCHITECTURE.md) · [Roadmap](./docs/DEV_ROADMAP.md) · [Changelog](./CHANGELOG.md)

---

## 🚀 Quick Start

```bash
git clone https://github.com/1593959/XMclaw.git && cd XMclaw
pip install -e .
xmclaw start                  # daemon up on 127.0.0.1:8765
```

Then open `http://127.0.0.1:8765/ui/` — the web UI shows a **first-run setup banner** with three inline forms (LLM key · persona · embedding). Fill them in, restart, and you're talking to an agent that owns your machine.

Prefer a wizard? `xmclaw onboard` walks the same three steps in the terminal.

```bash
xmclaw chat                   # interactive REPL
xmclaw chat --plan            # plan mode — agent proposes steps, you approve
xmclaw doctor                 # 21 health checks, 5 auto-fixable
xmclaw stop
```

Python ≥ 3.10. Cross-platform (Windows is a first-class target). The web UI is plain ESM served by FastAPI — no Node.js build step or runtime needed.

---

## ✨ What's different

| | |
|---|---|
| **Self-improvement on evidence, not vibes** | Every LLM call, tool result, and skill execution becomes a typed `BehavioralEvent`. After each tool call the **HonestGrader** runs four ground-truth checks (did it actually run? did it return? does the type match? is the side-effect observable?) and emits a `GRADER_VERDICT` event; LLM self-rating is hard-capped at 0.20. The `EvolutionAgent` observer aggregates verdicts per `(skill_id, version)` and proposes promotions through `SKILL_CANDIDATE_PROPOSED`. The controller (default `auto_apply=False`) lets you review with `xmclaw evolve review` and approve via `xmclaw evolve approve <id>` — every promotion goes through evidence-gated `SkillRegistry.promote(evidence=…)` (anti-req #12 enforced at the registry door). |
| **Local-first, all of it** | Events, vector memory, pairing token, persona files, daily logs all live in `~/.xmclaw/v2/` (SQLite WAL + sqlite-vec). `XMC_DATA_DIR` moves the whole workspace in one lever. No cloud, no telemetry, no upload. |
| **Cross-session memory that compacts itself** | An always-on file provider (MEMORY.md / USER.md / daily journal) plus an embedded vector index. A nightly **Auto-Dream** pass uses an LLM to dedupe, crystallize, and evict stale bullets — so memory stays useful instead of bloating into noise. |
| **Replayable everything** | Reconnect a WebSocket and the daemon re-emits the session's events so the UI rehydrates without re-hitting the LLM. `GET /api/v2/events` supports session/since/types filters + FTS5 search across payloads. Audit any decision months later. |
| **MCP + provider model** | Tools compose from `ToolProvider` backends: `builtin` (file / bash / web / vector recall), `browser` (Playwright), `mcp_bridge` (stdio / SSE / WS). Drop in your own with `list_tools()` + `invoke()`. |
| **Chinese-first by design** | Web UI is Chinese. Built-in prompt-injection scanner covers Chinese patterns (instruction overrides, role forgery, jailbreaks, exfiltration) alongside English. Default config snippet ships pointing at local Ollama (`qwen3-embedding:0.6b`) so 国产模型 just works. |
| **Secure by default** | Pairing-token auth on both WebSocket AND every `/api/v2/*` HTTP route. 10 MB request body cap. Atomic file writes (tmp + os.replace) — daemon crash mid-write can't truncate your SOUL.md. Filesystem sandbox via `tools.allowed_dirs`. Full prompt-injection scan on tool output, recalled memory, AND persona files. |

---

## 🧭 First-Run Onboarding

The web UI greets every fresh install with a **Setup Banner** — a contextual checklist of what's missing. Each row has an inline form so you don't dig through Config:

| Missing | What it means | Fix it from the UI |
|---|---|---|
| **LLM API key** | Agent runs in echo mode (just mirrors your messages) | Click "立即配置" — opens a 4-field form (provider / key / base_url / default_model) right there. Submit, restart daemon. |
| **Persona files** | No SOUL.md / IDENTITY.md — agent has no identity or working goal | Click "复制命令" to grab `xmclaw onboard`, paste in terminal, follow the wizard. |
| **Vector embedding** | `memory_search` falls back to keyword scan, no semantic recall | Memory page → Providers → "配置 embedding". Defaults pre-filled for local Ollama (`qwen3-embedding:0.6b @ 1024`). |

The banner auto-disappears once everything checks out and re-surfaces if state regresses. Per-item dismiss is per-browser (localStorage). Backed by `GET /api/v2/setup`.

---

## 🛡️ Security posture

XMclaw treats anything it didn't generate as untrusted. Defenses in depth:

- **Pairing-token auth** on WS + every `/api/v2/*` HTTP route. Constant-time compare. Allowlist: `/health`, `/api/v2/pair`.
- **Body size cap** at 10 MB — stops a 1 GB POST from OOM-killing the daemon.
- **Atomic file writes** — every persona / notes / journal / config write goes through `tmp + os.replace`. A crash mid-write can never truncate your state files.
- **Filesystem sandbox** — `tools.allowed_dirs` gates every `file_read` / `file_write` / `list_dir` / `file_delete`. Sandbox-root deletion refused.
- **No shell metacharacter parsing** — `bash` tool uses `subprocess.run(argv, shell=False)`.
- **Prompt-injection scanner** on tool output, recalled memory chunks, AND persona files. ~90 patterns covering English + Chinese instruction overrides, role forgery, jailbreaks, exfiltration, indirect injection, tool hijack. HIGH severity findings get redacted in place.
- **XSS-safe markdown** — chat panel falls back to escape-only rendering if the DOMPurify CDN fails to load.
- **Secret redaction** before events / logs / UI rendering.

`xmclaw doctor` audits the lot. `xmclaw doctor --fix` auto-remediates 5 of them. Full disclosure policy in [SECURITY.md](SECURITY.md).

---

## 🧱 Architecture, briefly

A single FastAPI daemon hosts an **AgentLoop** that composes pluggable providers: LLM (Anthropic / OpenAI / OpenAI-compatible), Tool (`builtin` / `browser` / `mcp_bridge` / composite), Memory (`builtin_file` + `sqlite_vec` + optional Hindsight / Mem0 / Supermemory), Channel (WS today, channel adapters next). A streaming **EventBus** (in-process + SQLite WAL + FTS5) connects everything; the **HonestGrader → EvolutionAgent observer → EvolutionController → EvolutionOrchestrator → SkillRegistry** pipeline rides the same bus to drive skill version promotion. Every step is evidence-gated; nothing reaches the agent's prompt or tool list without passing through it.

Authoritative design — including data flows, wire protocol, and event contract — in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Per-directory contracts under `xmclaw/<subdir>/AGENTS.md`.

---

## 📚 Documentation

| | |
|---|---|
| [Architecture](./docs/ARCHITECTURE.md) | System design, data flows, wire protocol |
| [Tools](./docs/TOOLS.md) | Built-in tools (file / bash / web / vector / journal / 等) |
| [Events](./docs/EVENTS.md) | Typed event stream contract |
| [Config](./docs/CONFIG.md) | `daemon/config.json` fields + `XMC__` env overrides |
| [Doctor](./docs/DOCTOR.md) | Diagnostic checks + `--fix` runner + plugin API |
| [Workspace](./docs/WORKSPACE.md) | `~/.xmclaw/` layout + `XMC_DATA_DIR` |
| [Dev Roadmap](./docs/DEV_ROADMAP.md) | Epics, milestones, execution protocol |
| [Changelog](./CHANGELOG.md) | What shipped per version |

---

## 🤝 Contributing

```bash
pip install -e ".[dev]"           # ruff, mypy, pytest, pip-tools
python -m pytest tests/ -v        # full suite
ruff check xmclaw/ --fix
mypy xmclaw/
```

- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, lint / type / test gates, commit conventions
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- [SECURITY.md](SECURITY.md) — vulnerability disclosure (private advisories preferred)
- [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) — anti-req checklist + roadmap discipline reminders
- [CLAUDE.md](CLAUDE.md) — onboarding notes for AI coding assistants

Epic-touching PRs must cite the Epic number (`Epic #11:`, `Epic #14 partial:`, …) and update [docs/DEV_ROADMAP.md](docs/DEV_ROADMAP.md) per the [execution protocol](docs/DEV_ROADMAP.md#36-执行协议execution-protocol-每次开发必读).

---

## 📄 License

MIT — see [LICENSE](LICENSE).

Built for developers who want a personal, self-improving AI agent they fully own — code, data, and decisions.
