# 🦞 XMclaw

<p align="center">
  <strong>A local-first AI agent runtime. Single Python daemon on your machine, remembers across sessions, drives your browser and desktop, speaks up on its own when it should, and reaches you on web / CLI / 飞书 / Telegram / Slack / 钉钉 / 企微 / Discord / 邮件 — all from the same agent loop. Your data, your tools, your shell. Nothing leaves the box unless you ask.</strong>
</p>

<p align="center">
  <a href="https://github.com/1593959/XMclaw/actions/workflows/python-ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/1593959/XMclaw/python-ci.yml?branch=main&style=for-the-badge" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge" alt="Python"></a>
  <img src="https://img.shields.io/badge/Platform-Win%20%7C%20macOS%20%7C%20Linux-blue?style=for-the-badge" alt="Cross-platform">
  <img src="https://img.shields.io/badge/Version-1.0.0-brightgreen?style=for-the-badge" alt="1.0.0">
</p>

XMclaw is **not a chatbot**. It is a runtime that thinks, acts, remembers across sessions, and — when you enable it — **speaks up on its own**: calendar reminders, idle check-ins, stale-project nudges, scheduled daily briefings. A single FastAPI daemon binds `127.0.0.1:8766`, hosts the **AgentLoop**, and composes pluggable **LLM / Tool / Memory / Channel** providers over a streaming `BehavioralEvent` bus.

Reach it however suits the moment:

- **Web UI** at `http://127.0.0.1:8766/ui/` — chat + dashboard + memory + settings (Preact+htm ESM, no Node build, PWA-installable)
- **CLI** — `xmclaw chat` for terminal-native; `xmclaw chat --plan` for approval-gated turns
- **8 channel adapters** — 飞书 / Telegram / Slack / 钉钉 / 企微 / Discord / 邮件 / 微信 (all in `xmclaw/providers/channel/`). Configure `app_id` + `app_secret` (or equivalent), restart, and the agent answers in chat with the **same memory, tools, and history** as the web UI — no public IP needed (WebSocket long-poll where the platform allows it)
- **Continuous voice** — one toggle in the web UI's "🔁 对话" mode and you're in a hands-free state machine: listen → submit → TTS reply → listen again
- **Any WebSocket client** — daemon speaks a typed event stream at `/agent/v2/{session_id}`

Use any model — Anthropic / OpenAI / OpenRouter / Moonshot Kimi / DeepSeek / MiniMax / DashScope / Qwen / 本地 Ollama — switch with one config change. Plus **tier-based routing**: cheap models for trivial turns, strong models for tools/complex work, automatic via regex classifier (no LLM call to decide what model to call).

[Quick Start](#quick-start) · [What's different](#whats-different) · [Architecture](#architecture-briefly) · [Memory v3](#memory-v3) · [Tools](#tools) · [Docs](./docs/JARVIS_IMPLEMENTATION_PLAN_2026.md) · [Changelog](./CHANGELOG.md)

---

## Quick Start

Install from GitHub in one command. The installer creates an isolated virtualenv,
installs `xmclaw[all]`, installs Playwright Chromium for browser automation, and
adds the `xmclaw` launcher to your user PATH.

**Windows PowerShell**

```powershell
irm https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.ps1 | iex
```

**macOS / Linux / WSL**

```bash
curl -fsSL https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.sh | bash
```

Then start XMclaw:

```bash
xmclaw config init
xmclaw start                  # daemon up on 127.0.0.1:8766
```

Open `http://127.0.0.1:8766/ui/`. The web UI's first-run setup banner walks
through LLM key, persona, and embedding setup. Prefer a wizard?
`xmclaw onboard` walks the same steps in the terminal.

Install a specific branch, tag, or commit by setting `XMCLAW_REF`:

```powershell
$env:XMCLAW_REF="main"; irm https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.ps1 | iex
```

```bash
XMCLAW_REF=main curl -fsSL https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.sh | bash
```

Developer checkout:

```bash
git clone https://github.com/1593959/XMclaw.git && cd XMclaw
pip install -e ".[dev,all]"
python -m playwright install chromium
```

```bash
xmclaw chat                   # interactive REPL
xmclaw chat --plan            # plan mode: agent proposes steps, you approve
xmclaw doctor                 # health checks and auto-fixable diagnostics
xmclaw skill list-marketplace # browse the curated skill catalog
xmclaw skill install <id>     # clone, scan, and register a community skill
xmclaw stop
```

**Isolated dev sandbox** (recommended for hacking on XMclaw itself):

```powershell
./start-dev.ps1   # XMC_DATA_DIR=./.data, port 8766, no prod ~/.xmclaw collision
```

**Mobile / outside-the-LAN access**:

```bash
scripts/tunnel.ps1            # Windows: wraps cloudflared quick tunnel
scripts/tunnel.sh             # Linux / macOS / WSL
                              # *.trycloudflare.com URL until Ctrl+C
                              # Pairing-token auth still gates every /api/v2/* request
```

Python >= 3.10. Cross-platform; Windows is a first-class target.

---

## ✨ What's different

| | |
|---|---|
| **Memory v3 — bucket-routed facts with bidirectional .md projection** | Every fact lands in LanceDB with a registered `bucket` tag. The renderer projects each bucket into a specific `.md` section (USER.md ## Auto-extracted preferences, MEMORY.md ## Failure Modes, AGENTS.md ## Workflows, …) so the agent reads its memory verbatim in every system prompt — no semantic recall guesswork for durable facts. The `<!-- fid:abc123 -->` markers on every rendered bullet make .md and LanceDB **bidirectionally addressable**: the agent can pull a fid off the file and call `memory(action='replace', old_fid=...)` in one shot. Per-turn **similarity recall** rides the user message as a `<recalled>` block (NOT the system prompt — preserves prompt-cache hits), time-boxed to 1s so it can never wedge a turn. See [Memory v3](#-memory-v3) below. |
| **Proactive cognition** | `ProactiveAgent` ticks every 30 s, lets registered triggers speak unprompted: `idle_check_in` (gentle ping when you go quiet mid-task), `system_health` (low disk / runaway process), `calendar_reminder` (reads your `.ics`, fires 5 min before any event), `stale_project` (autobio memory says you said you'd ship X 10 days ago), `cron` (arbitrary `0 8 * * *`-style schedules), `daily_digest` (22:00 markdown summary of today's autonomous activity). Per-trigger cooldowns + global quiet hours (default 23:00–07:00). Web UI bubbles + (when configured) push straight to your 飞书 chat as native phone notification. |
| **8 channel adapters, one agent loop** | First-class bidirectional IM bridges in `xmclaw/providers/channel/`: 飞书 / Telegram / Slack / 钉钉 / 企微 / Discord / 邮件 / 微信. Each adapter shares the same `AgentLoop`, same memory, same tools, same session store — switching surface mid-conversation just works. Text + image inbound (auto-routes through multimodal), markdown-auto-card outbound (`**bold**` / lists / tables render natively where the platform allows), prompt-injection scanning on every inbound, `allowed_user_refs` allowlist for group safety, optional per-sender session partitioning so张三李四 in the same group keep independent conversations, slash-command short-circuits that don't burn LLM tokens. |
| **Browser automation, 30 tools, default-on** | Playwright-backed Chromium with `browser_open` / `click` / `fill` / `screenshot` / `snapshot` / `eval` / `close` / `hover` / `scroll` / `select_option` / `upload` / `wait_for` / `back` / `forward` / `reload` / `tabs` / `tab_switch` / `tab_close` / `save_state` / `import_cookies` / `get_console` / `download_next` / **`use_my_browser`** (CDP-attach to your real Chrome/Edge/Brave so login-walled sites just work) / **`click_ref`** / **`type_ref`** (ref-by-number after snapshot — no CSS selector reasoning) / **`dialog`** + **`dialog_arm`** (handle JS alert/confirm/prompt without hanging) / **`network_log`** (read XHR/fetch responses) / **`screenshot(annotate=true)`** (overlay `[N]` labels on elements). Per-session BrowserContext (open → fill → click → screenshot keeps state). `allowed_hosts` whitelist. `download_dir` off by default. **One-time setup**: `pip install 'xmclaw[browser]'` + `playwright install chromium` (~150 MB). Skip it and the tools just don't list — daemon boots clean. |
| **OCR + vision routing for text-only models** | `xmclaw/daemon/image_routing.py` decides per-turn: if the active LLM profile is vision-capable (Kimi K2.6, Claude, GPT-4o), pass image attachments as raw blocks. If it's text-only (DeepSeek V3/V4, smaller open-source), run **local OCR** (RapidOCR singleton, ~1s warm, fallback PaddleOCR → Tesseract) and fold the extracted text into the user message as `[image OCR \| name.png]`. The model "sees" the image content as text without you having to think about it. Config: `llm.profiles[].supports_vision: true/false`. |
| **Chinese-first web stack** | `web_search` defaults to DDG → **automatic fallback to Bing CN HTML scrape** (no key needed, works from CN networks where DDG is blocked). `web_fetch` ships **real Chrome 145 headers** + retry-with-backoff + clear "this looks bot-blocked, try browser_open" hints on 403 Cloudflare pages. |
| **Skill evolution architecture (opt-in)** | Architecture is in place; **default `evolution.enabled = false`** until LongMemEval / TerminalBench / SWE-bench A/B numbers prove the loop helps more than it costs. Observers run as passive instrumentation when enabled: **HonestGrader** scores tool results on evidence (ran / returned / type-matched / side-effect-observable, summed at 0.80; LLM self-rating capped at 0.20). **JournalWriter** logs one row per session under `~/.xmclaw/v2/journal/<YYYY-MM>/`. **SkillDreamCycle / RealtimeEvolutionTrigger / ProposalMaterializer / skill_pattern_detector** draft new skills from journal patterns + repeated tool-call n-grams, but **promotion stays human-gated** (`xmclaw evolve review` / `approve <id>`). Honest disclosure: "self-evolving" is the **goal of the architecture**, not a current verified property — see [docs/EVOLUTION_HONEST_STATE.md](docs/EVOLUTION_HONEST_STATE.md). |
| **Skill marketplace MVP** | Curated GitHub-backed catalog at `docs/skill_marketplace_index.json`. `xmclaw skill install <id>` clones into `~/.xmclaw/skills_user/<id>/`, runs the security scanner against every `*.py` (fail-closed on CRITICAL), registers via `UserSkillsLoader` on next boot. Browse + 1-click install from the web UI's "技能商店". Trust tiers: `verified` (XMclaw-vetted) / `community` (third-party). |
| **Local-first, all of it** | Events DB, fact store, pairing token, persona files, daily logs, autobio memory, calendar all under `~/.xmclaw/v2/`. **LanceDB** powers the vector store; SQLite WAL + FTS5 power the event log. `XMC_DATA_DIR` moves the whole workspace in one lever. No cloud, no telemetry, no upload. |
| **Replayable everything** | Reconnect a WebSocket and the daemon re-emits the session's events so the UI rehydrates without re-hitting the LLM. `GET /api/v2/events` supports session/since/types filters + FTS5 search across payloads. Audit any decision months later. |
| **Cross-device UI sync** | `/api/v2/sync/ui-state` stores active session, model pick, theme, density, audio prefs server-side so picking up on phone where you left off on desktop just works. Last-write-wins, atomic JSON persistence, debounced client lib. |
| **Tier router, MCP, Composio, computer use** | Tools compose from `ToolProvider` backends: **builtin** (37 tools — file / bash / web / persona / memory / calendar / cron / undo cabinet / canvas / subagent), **browser** (30 tools, Playwright), **computer_use** (22 tools — pyautogui drives ANY desktop app via OCR + pixels, opt-in), **automation** (8 tools — cron CRUD / code_python / process inspection), **media** (5 tools — mic / TTS / camera), **mcp_bridge** (stdio + SSE + streamableHttp), **composio** (7000+ pre-integrated SaaS). Drop in your own with `list_tools()` + `invoke()`. **Tier router** picks `fast` / `balanced` / `strong` / `vision` per turn via a pure regex classifier — no LLM call to decide what model to call. |
| **Secure by default** | Pairing-token auth on WS + every `/api/v2/*` HTTP route (constant-time compare; allowlist: `/health`, `/api/v2/pair`). 10 MB request body cap. Atomic file writes (tmp + `os.replace`) — daemon crash mid-write can't truncate your SOUL.md. Filesystem sandbox via `tools.allowed_dirs`. **Undo cabinet** auto-snapshots every destructive file op so an over-eager turn is reversible. Full **prompt-injection scan** on tool output, recalled memory, AND persona files (~90 patterns covering English + Chinese instruction overrides, role forgery, jailbreaks, exfiltration, indirect injection, tool hijack). `xmclaw doctor` audits the lot; `xmclaw doctor --fix` auto-remediates 5 checks. |

---

## 🧠 Memory v3

The fact layer is **two complementary tracks**, both writing through one store:

### Structural axis (cache-friendly, always-on)

Every fact lands in LanceDB with a **registered `bucket` tag** from `xmclaw/memory/v2/buckets.py::BUCKETS` (11 buckets covering `agent_identity` / `user_identity` / `user_preference` / `values` / `workflow` / `tool_quirks` / `rules` / `failure_modes` / `project_fact` / `commitment` / `misc`). The renderer projects each bucket into a specific section of a specific `.md` file:

```
user_preference   → USER.md     ## Auto-extracted preferences
project_fact      → MEMORY.md   ## Project facts
workflow          → AGENTS.md   ## Workflows
tool_quirks       → TOOLS.md    ## Tool quirks
failure_modes     → MEMORY.md   ## Failure Modes
commitment        → MEMORY.md   ## Active commitments
…                                                                   …
misc              → MEMORY.md   ## Other facts (recent)   ← catch-all (no "dark facts")
```

Each rendered bullet carries a `<!-- fid:abc123 -->` marker — the agent can pull a fid off `.md` and call `memory(action='replace', old_fid='abc123')` to fix it in one shot. `.md` files ride the **stable prefix** of the system prompt (Anthropic `cache_control` breakpoint inserted between stable and volatile sections), so persistent facts get cached on the LLM side.

### Similarity axis (per-turn, time-boxed)

`auto_recall` embeds the user's message, queries LanceDB, filters out facts already in the structural axis, and prepends a `<recalled>` block to the user message itself — **NOT** the system prompt. Hybrid retrieval available (vector + BM25 fusion, OpenClaw-style — requires `pip install 'xmclaw[memory-bm25]'`). Hard-bounded with `asyncio.wait_for(timeout_s=1.0)`; if recall stalls, the turn proceeds without it. Off by default (`cognition.auto_recall.enabled = false`); opt-in via config.

### The agent's 4-tool memory surface

| Tool | Purpose |
|---|---|
| `memory(action="add"\|"replace"\|"forget"\|"pin", ...)` | Multi-action CRUD. `add` requires a bucket; unknown bucket coerces to `misc`. `replace(old_fid=...)` flows through `service.correct` so the SUPERSEDES edge is properly recorded. `commitment` bucket requires `due_ts` and auto-schedules a one-shot cron to fire the reminder. |
| `memory_search(query, ...)` | Explicit semantic search (vector + optional BM25 hybrid). Filters by scope / kind / bucket / confidence / time range. |
| `memory_get(file, section?, lines?)` | Read a persona MD file (or section, or line range) verbatim. Preserves `<!-- fid:xxx -->` markers so the agent can grab fids and feed them straight back into `memory(action=...)`. |
| `memory_inspect(scope?, auto_dedup=false)` | Read-only health probe. Reports fact counts, per-(scope, kind) breakdown, suspected duplicate ratio, oldest / largest entries. Optionally runs dedup in-place when ratio > 15%. |

### Background maintenance

The retention loop (hourly) runs TTL / cap eviction; every 24 sweeps (daily by default) it also runs `dedup_scope` across `user` / `project` / `session` scopes. Plus the 03:00 Dream Compactor pass on MEMORY.md (LLM-driven dedup + crystallization, refuses any rewrite that shrinks the file > 70%). All gated by config; all skippable.

---

## 🔧 Tools

**102 tools** registered across 5 provider modules — every tool has a typed schema, a description, and a clean error envelope (`ToolResult(error=...)`).

| Provider | Count | What it covers |
|---|---|---|
| **builtin** | 37 | file_read / write / list / delete · bash · python_run · web_search · web_fetch · open_in_user_browser · todo_read/write · remember · learn_about_user · update_persona · memory (multi-action) · memory_search · memory_get · memory_inspect · memory_pin · memory_compact · memory_correct · memory_forget · memory_dedup · schedule_followup · sqlite_query · journal_recall · recall_user_preferences · propose_curriculum_edit · list_curriculum_proposals · think · 等 |
| **browser** | 30 | Playwright Chromium — `open` / `click` / `fill` / `screenshot` / `snapshot` / `eval` / `close` / `hover` / `scroll` / `select_option` / `upload` / `wait_for` / `back` / `forward` / `reload` / `tabs` / `tab_switch` / `tab_close` / `save_state` / `import_cookies` / `get_console` / `download_next` / `use_my_browser` (CDP-attach to real Chrome) / `click_ref` / `type_ref` (snapshot-numbered actions) / `dialog` / `dialog_arm` / `network_log` |
| **computer_use** | 22 | pyautogui — `screen_capture` / `screen_size` / `cursor_position` / `mouse_move` / `mouse_click` / `mouse_drag` / `mouse_scroll` / `keyboard_type` / `keyboard_press` / `window_list` / `window_focus` / `screen_ocr` / `find_on_screen` / `click_on_text` / `wait_for_text` / `screen_region_capture` / `find_image_on_screen` / `click_on_image` / `scroll_to_text` / `ui_inspect` / `ui_click` / `gui_send_chat`. Failsafe always on: drag mouse to (0,0) to abort any GUI action. |
| **automation** | 8 | cron CRUD (`cron_create` / `cron_list` / `cron_pause` / `cron_resume` / `cron_remove`) · `code_python` (subprocess + IPython kernel pool) · `process_list` · `process_kill` |
| **media** | 5 | `mic_record` · `voice_listen` · `speak` (TTS) · `camera_capture` · `camera_list` |

Plus dynamic backends: **MCP bridge** (stdio + SSE + streamableHttp transports) auto-discovers tools from any MCP server; **Composio** exposes 7000+ pre-integrated SaaS tools; **user skills** under `~/.xmclaw/skills_user/` register via `UserSkillsLoader` on boot.

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
- **Undo cabinet** auto-snapshots every destructive file op (Sprint 0) so an over-eager turn is reversible.

`xmclaw doctor` runs 28 checks today. `xmclaw doctor --fix` auto-remediates 5. Full disclosure policy in [SECURITY.md](SECURITY.md).

---

## 🧱 Architecture, briefly

A single FastAPI daemon hosts an **AgentLoop** that composes pluggable providers:

- **LLM** — Anthropic / OpenAI / OpenAI-compatible (Moonshot Kimi / DeepSeek / MiniMax / DashScope / Qwen / Ollama) / OpenRouter. Per-profile `supports_vision` flag drives the OCR-or-image-block decision in `image_routing.py`. Tier router picks `fast` / `balanced` / `strong` / `vision` per turn.
- **Tool** — `builtin` (37) / `browser` (30) / `computer_use` (22) / `automation` (8) / `media` (5) / `mcp_bridge` (stdio + SSE + streamableHttp) / `composio` (7000+ SaaS) / `composite` (union of any subset).
- **Memory** — LanceDB-backed `MemoryService` (V2; V1 SqliteVecMemory retired 2026-05-24). Bucket registry (`xmclaw/memory/v2/buckets.py`) + v2 renderer (`xmclaw/core/persona/v2_renderer.py`) project facts into `.md` files; optional BM25 hybrid recall (`xmclaw/memory/v2/bm25.py`); auto-recall (`xmclaw/daemon/auto_recall.py`) injects `<recalled>` block per turn; nightly Dream Compactor; hourly retention sweep + daily auto-dedup.
- **Channel** — WebSocket gateway + 8 IM adapters (`xmclaw/providers/channel/{feishu, telegram, slack, dingtalk, wecom, discord, email, weixin}/`).

A streaming **EventBus** (in-process + SQLite WAL + FTS5) connects everything. Two loops ride that bus:

- **Reactive turn loop** — user message → `AgentLoop.run_turn` → LLM ↔ tool hops → assistant reply. Same path whether the message came from web UI, CLI, or a channel adapter.
- **Proactive tick loop** — `ProactiveAgent` polls registered triggers every 30 s. On fire, publishes `PROACTIVE_PROPOSAL`. Web UI subscribers render a bubble; `ProactiveChannelBridge` fans it out to every channel that opted in via `proactive_chat_id` so your phone wakes up.

The **HonestGrader → EvolutionAgent → EvolutionEvaluationTrigger → EvolutionController → EvolutionOrchestrator → SkillRegistry** pipeline drives skill version promotion (off by default), with **MutationOrchestrator** synthesising new versions and **SkillDreamCycle / RealtimeEvolutionTrigger / ProposalMaterializer / `skill_pattern_detector`** drafting brand-new skills from journal patterns + repeated tool-call n-grams. Every step is evidence-gated; nothing reaches the agent's prompt or tool list without passing through it.

Authoritative design — including data flows, wire protocol, event contract, Phase roadmap, and module status matrix — in [docs/JARVIS_IMPLEMENTATION_PLAN_2026.md](docs/JARVIS_IMPLEMENTATION_PLAN_2026.md). Per-directory contracts under `xmclaw/<subdir>/AGENTS.md`.

---

## 📁 Repository layout

```
xmclaw/                     # Python package
├── core/                   # Bus, IR, grader, evolution, scheduler, persona renderer
├── daemon/                 # FastAPI + WS + AgentLoop + lifecycle + auto_recall + image_routing
├── providers/
│   ├── llm/                # Anthropic / OpenAI / OpenAI-compat translators + tier router
│   ├── tool/               # builtin / browser / computer_use / automation / media / mcp / composio
│   ├── memory/             # builtin_file + (legacy) sqlite_vec shim
│   ├── channel/            # feishu / telegram / slack / dingtalk / wecom / discord / email / weixin / ws
│   └── voice/              # STT (Whisper / Vosk) + TTS adapters
├── memory/
│   └── v2/                 # MemoryService + LanceDB backend + buckets + bm25 + llm_extractor
├── cognition/              # ProactiveAgent + triggers + autobio memory + cognitive_daemon + tier_router
├── security/               # Prompt-injection scanner + redactor
├── skills/                 # SkillBase + registry + marketplace + demo skills
├── cli/                    # `xmclaw` entry points + doctor (28 checks)
├── utils/                  # paths / log / redact / cost / fs_locks / tunnel
└── plugins/                # Third-party plugin loader (entry-point discovery)

daemon/                     # Runtime config (config.json gitignored; .example.json template)
docs/                       # JARVIS_IMPLEMENTATION_PLAN_2026.md = single source of truth
tests/                      # pytest suites — smart-gate lanes in scripts/test_lanes.yaml
scripts/                    # tunnel / migrate / probe / setup / test_changed
```

Runtime data (events.db, LanceDB facts, pairing token, daemon.pid, persona files, journal …) lives under `~/.xmclaw/v2/` — *not* inside the repo.

---

## 📚 Documentation

| | |
|---|---|
| [docs/JARVIS_IMPLEMENTATION_PLAN_2026.md](docs/JARVIS_IMPLEMENTATION_PLAN_2026.md) | **Single source of truth** — architecture, Phase roadmap, module status matrix, design decisions |
| [docs/COMPREHENSIVE_TRIPARTITE_AUDIT_2026.md](docs/COMPREHENSIVE_TRIPARTITE_AUDIT_2026.md) | Latest audit findings + remediation plan |
| [docs/REMEDIATION_PLAN_2026.md](docs/REMEDIATION_PLAN_2026.md) | Active gap-closing work tracking |
| [CHANGELOG.md](CHANGELOG.md) | What shipped per version |
| [CLAUDE.md](CLAUDE.md) | Onboarding notes for AI coding assistants |
| Per-directory `AGENTS.md` | Subdir contracts — read before editing `xmclaw/<subdir>/*` |

---

## 🤝 Contributing

```bash
pip install -e ".[dev]"                       # ruff, mypy, pytest, pip-tools
python -m pytest tests/ -v                    # full suite (slow)
python scripts/test_changed.py --dry-run      # smart-gate: only affected lanes
ruff check xmclaw/ --fix
mypy xmclaw/
```

- [CONTRIBUTING.md](CONTRIBUTING.md) — workflow, lint / type / test gates, commit conventions
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- [SECURITY.md](SECURITY.md) — vulnerability disclosure (private advisories preferred)
- [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) — anti-req checklist + roadmap discipline reminders

**Phase-touching PRs** must cite the Phase number (`Phase 1: ...`, `Phase 3 partial: ...`, ...) and update [docs/JARVIS_IMPLEMENTATION_PLAN_2026.md](docs/JARVIS_IMPLEMENTATION_PLAN_2026.md) per the execution protocol in `CLAUDE.md`. Direct push to `main` is the default workflow for this repo (single-author project; full smart-gate runs via CI as the safety net).

---

## 📄 License

MIT — see [LICENSE](LICENSE).

Built for developers who want a personal, self-improving AI agent they fully own — code, data, and decisions.
