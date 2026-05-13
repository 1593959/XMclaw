# 🦞 XMclaw

<p align="center">
  <strong>A local-first AI agent that lives on your machine, remembers across sessions, speaks up on its own when it should, and reaches you on whatever device you're holding — web UI, CLI, or 飞书. Skill self-evolution architecture is in place but human-gated by default until benchmark numbers land.</strong>
</p>

<p align="center">
  <a href="https://github.com/1593959/XMclaw/actions/workflows/python-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/1593959/XMclaw/python-ci.yml?branch=main&style=for-the-badge" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge" alt="Python"></a>
  <img src="https://img.shields.io/badge/Platform-Win%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge" alt="Cross-platform">
  <img src="https://img.shields.io/badge/Status-1.0%20stable-brightgreen?style=for-the-badge" alt="1.0 stable">
</p>

XMclaw is **not a chatbot**. It is a runtime that thinks, acts, remembers, and — when you enable it — **speaks up on its own**: calendar reminders, idle check-ins, stale-project nudges, scheduled daily briefings. It runs in a single Python daemon on `127.0.0.1:8765` — your data, your tools, your shell, your filesystem. Nothing leaves the box unless you ask.

Reach it however suits the moment:
- **Web UI** at `http://127.0.0.1:8765/ui/` — full chat + Dashboard + Settings (PWA-installable, mobile-responsive)
- **CLI** — `xmclaw chat` for terminal-native, `xmclaw chat --plan` for approval-gated turns
- **Feishu / Lark** — `enabled: true` + `app_id` in config and the daemon's bot relays everything through 飞书's WebSocket long-poll (no public IP needed). Group + DM + image inbound + slash commands (`/订阅` / `/状态` / `/日程` / `/任务`) ride the same AgentLoop as the web UI. Phone notifications come for free via 飞书's native push.
- **Continuous voice** — one toggle in the web UI's "🔁 对话" mode and you're in a hands-free state machine: listen → submit → TTS reply → listen again. Energy-based VAD ships as a lib for noisy environments.
- **Any WebSocket client** — daemon speaks a typed event stream at `/agent/v2/{session}`.

Use any model — Anthropic / OpenAI / MiniMax / Moonshot / DashScope / Qwen / 本地 Ollama — switch with one config change. Plus tier-based routing (Sprint 0): cheap models for trivial turns, strong models for tools/complex work, automatic.

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
xmclaw skill list-marketplace # browse the curated skill catalog (B-390)
xmclaw skill install <id>     # clone + scan + register a community skill
xmclaw stop

# Mobile / outside-the-LAN access:
scripts/tunnel.ps1            # Windows — wraps cloudflared quick tunnel
scripts/tunnel.sh             # Linux / macOS / WSL
                              # Gives a *.trycloudflare.com URL valid until Ctrl+C.
                              # Pairing-token auth still gates every /api/v2/* request.
```

Python ≥ 3.10. Cross-platform (Windows is a first-class target). The web UI is plain ESM served by FastAPI — no Node.js build step or runtime needed.

**Want it on your phone right now?** Easiest path: set `channels.feishu.enabled = true` in `daemon/config.json` with your app_id / app_secret (飞书 open platform → 创建应用 → 启用机器人 + 订阅 `im.message.receive_v1` over WebSocket long-poll). After restart, `@` the bot in any 飞书 chat and you're talking to the same AgentLoop the web UI hits. Drop `/订阅` in that chat to register it as the proactive-push target — calendar reminders + idle check-ins land on 飞书's native push, lock screen and all.

---

## ✨ What's different

| | |
|---|---|
| **Proactive cognition (Sprint 2)** | `ProactiveAgent` ticks every 30 s and lets registered triggers speak unprompted. Built-ins: `idle_check_in` (gentle ping when you go quiet mid-task), `system_health` (low disk / runaway process), `calendar_reminder` (reads your `.ics` export, fires 5 min before any event), `stale_project` (your autobiographical memory says you said you'd ship X 10 days ago), `cron` (arbitrary `0 8 * * *`-style schedules from `daemon/config.json`), `daily_digest` (22:00 markdown summary of today's autonomous activity). Each respects per-trigger cooldowns + global quiet hours (default 23:00–07:00). Web UI bubbles + (when configured) push straight to your 飞书 chat as a native phone notification. |
| **Channel adapters: 飞书 / Telegram / Slack / DingTalk / WeCom** | First-class bidirectional IM bridges. Configure `app_id` + `app_secret` (or equivalent), restart, and the agent answers in chat with the same memory, tools, and history as the web UI — no public IP required (WebSocket long-poll). 飞书 today supports text + image inbound (auto-routes through the multimodal pipeline), markdown-auto-card outbound (`**bold**` / lists / tables render natively), prompt-injection scanning on every inbound, an `allowed_user_refs` allowlist for group safety, optional per-sender session partitioning (`session_per_user`) so张三李四 in the same group keep independent conversations, and slash-command short-circuits that don't burn LLM tokens. |
| **Dashboard "control tower"** | `/ui/dashboard` aggregates 9 cards in one fetch: daemon uptime + version, proactive trigger registry + last fire, autobiographical memory snapshot (people / projects / facts), cognitive state (goals + attention focus + fatigue), pending suggestions, task queue, local storage footprint, today's LLM spend (per-model + cache hit-rate), and a 25-item activity timeline (📢 主动发声 / 🪞 反思 / 🧠 记忆整理 / 🎯 目标梳理 / 💡 元认知 / 🔄 任务状态 / ⬆ 技能晋升). Auto-refreshes every 10 s. Each card best-effort: a failing subsystem renders a placeholder, never 500s the page. |
| **Continuous voice + read-write calendar** | Web UI's "🔁 对话" toggle enters a 4-state machine (listening → submitting → speaking → listening) — say a sentence, agent answers via TTS, recognizer auto-restarts. Energy-based VAD lib (`lib/vad.js`) ready for noisy environments. Calendar is bidirectional: `CalendarReminderTrigger` reads your ICS for upcoming events, the `calendar_create_event` tool writes new VEVENTs back to the same file (atomic append, RFC-5545-compliant), so "帮我加个周三晚 7 点的提醒" lands and the reminder pipeline picks it up on the next 60 s cache miss. |
| **Skill evolution (experimental, opt-in until Sprint 4 benchmark)** | Architecture is in place; **default `evolution.enabled = false`** until LongMemEval / TerminalBench / SWE-bench A/B numbers prove the loop helps more than it costs. Today the observers run as **passive instrumentation** when enabled: **HonestGrader** scores tool results on evidence (ran / returned / type-matched / side-effect-observable, summed at 0.80; LLM self-rating capped at 0.20). **JournalWriter** logs one row per session under `~/.xmclaw/v2/journal/<YYYY-MM>/`. **ExtractFactsHook** routes turn-end facts to AGENTS.md / TOOLS.md / MEMORY.md / SOUL.md / LEARNING.md / USER.md. `recall_user_preferences` + `journal_recall` tools let the agent read its own memory. **SkillDreamCycle / RealtimeEvolutionTrigger / ProposalMaterializer** can draft new v1 skills from journal patterns, but **promotion stays human-gated** (`xmclaw evolve review` / `approve <id>`). A new `skill_pattern_detector` (Wave 19) cross-session-counts repeated tool-call n-grams as additional skill-draft candidates. Honest disclosure: "self-evolving" is the **goal of the architecture**, not a current verified property — see [docs/EVOLUTION_HONEST_STATE.md](docs/EVOLUTION_HONEST_STATE.md) for what works, what's a stub, and the Sprint 3/4 plan. |
| **Local-first, all of it** | Events, vector memory, pairing token, persona files, daily logs, autobiographical memory, calendar all live in `~/.xmclaw/v2/` (SQLite WAL + sqlite-vec + JSON). `XMC_DATA_DIR` moves the whole workspace in one lever. No cloud, no telemetry, no upload. |
| **Cross-session memory that compacts itself** | An always-on file provider (MEMORY.md / USER.md / daily journal) plus an embedded vector index plus an **autobiographical memory** (people / projects / facts extracted from your messages via regex + LLM — "我朋友小张" / "我喜欢咖啡" / "我在做 XMclaw" land as durable rows). A nightly **Auto-Dream** pass dedupes, crystallizes, and evicts stale bullets so memory stays useful instead of bloating into noise. |
| **Cross-device UI sync (Wave 13)** | `/api/v2/sync/ui-state` stores active session, model pick, theme, density, audio prefs server-side so picking up on phone where you left off on desktop just works. Last-write-wins, atomic JSON persistence, debounced client lib. |
| **Replayable everything** | Reconnect a WebSocket and the daemon re-emits the session's events so the UI rehydrates without re-hitting the LLM. `GET /api/v2/events` supports session/since/types filters + FTS5 search across payloads. Audit any decision months later. |
| **MCP + provider model + tier routing** | Tools compose from `ToolProvider` backends: `builtin` (file / bash / web / vector recall / calendar / undo cabinet), `browser` (Playwright), `mcp_bridge` (stdio / SSE / WS), `composio` (7000+ pre-integrated SaaS). Drop in your own with `list_tools()` + `invoke()`. **Tier router** picks `fast` / `balanced` / `strong` / `vision` per turn via a pure regex classifier — no LLM call to decide what model to call. |
| **Chinese-first by design** | Web UI is Chinese. Built-in prompt-injection scanner covers Chinese patterns (instruction overrides, role forgery, jailbreaks, exfiltration) alongside English. Default config snippet ships pointing at local Ollama (`qwen3-embedding:0.6b`) so 国产模型 just works. |
| **Secure by default** | Pairing-token auth on both WebSocket AND every `/api/v2/*` HTTP route. 10 MB request body cap. Atomic file writes (tmp + os.replace) — daemon crash mid-write can't truncate your SOUL.md. **Undo cabinet** (Sprint 0) auto-snapshots every destructive file op so an over-eager turn is reversible. Filesystem sandbox via `tools.allowed_dirs`. Full prompt-injection scan on tool output, recalled memory, AND persona files. |
| **Skill marketplace MVP (B-390, Sprint 2)** | A curated GitHub-backed catalog at `docs/skill_marketplace_index.json` lets you discover community skills. `xmclaw skill install <id>` clones into `~/.xmclaw/skills_user/<id>/`, runs the security scanner against every `*.py` (fail-closed on CRITICAL), and registers via the daemon's `UserSkillsLoader` on next boot. Browse + 1-click install from the web UI's "技能商店" page. Trust tiers: `verified` (XMclaw-vetted) / `community` (third-party). Not yet: ratings, reviews, signing — that's Epic #16 territory. |

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

A single FastAPI daemon hosts an **AgentLoop** that composes pluggable providers: LLM (Anthropic / OpenAI / OpenAI-compatible — including B-320 prompt-cache parity for Moonshot Kimi & Zhipu GLM via the Anthropic-style `cache_control` marker), Tool (`builtin` / `browser` / `mcp_bridge` / `composio` / `calendar` / composite), Memory (`builtin_file` + `sqlite_vec` + autobiographical SQLite + optional Hindsight / Mem0 / Supermemory), Channel (WebSocket + 飞书 / Telegram / Slack / DingTalk / WeCom / Discord adapters). A streaming **EventBus** (in-process + SQLite WAL + FTS5) connects everything.

Two loops ride that bus:

- **Reactive turn loop** — user message → `AgentLoop.run_turn` → LLM ↔ tool hops → assistant reply. Same path whether the message came from the web UI, CLI, or a channel adapter.
- **Proactive tick loop** — `ProactiveAgent` polls registered triggers every 30 s (idle / calendar / stale_project / cron / daily_digest / system_health). On fire, publishes `PROACTIVE_PROPOSAL`. Web UI subscribers render a bubble; `ProactiveChannelBridge` (Wave 9) fans it out to every channel that opted in via `proactive_chat_id` so your phone wakes up.

The **HonestGrader → EvolutionAgent → EvolutionEvaluationTrigger → EvolutionController → EvolutionOrchestrator → SkillRegistry** pipeline rides the same bus to drive skill version promotion (gated by default — see "Skill evolution" above), with **MutationOrchestrator** synthesising new versions and **SkillDreamCycle / RealtimeEvolutionTrigger / ProposalMaterializer / `skill_pattern_detector`** drafting brand-new skills from journal patterns + repeated tool-call n-grams. Every step is evidence-gated; nothing reaches the agent's prompt or tool list without passing through it.

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
