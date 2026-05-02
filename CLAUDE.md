# CLAUDE.md

Top-level navigation for Claude Code (and other AI coding assistants).
**Per-directory contracts live in `xmclaw/<subdir>/AGENTS.md`** — read
those before editing code in that subdir.

## ★ 开发纪律（硬约束，2026-04-22 起）

任何涉及 Epic / Milestone 的代码改动，**必须**同时更新 [docs/DEV_ROADMAP.md](docs/DEV_ROADMAP.md)：

1. **开工前**：在对应 Epic §4 把状态 ⬜→🟡，填负责人 + 起始日期
2. **子步骤完成**：勾 checkbox + **进度日志**追加一行 `YYYY-MM-DD: <摘要> (commit <sha7>)`
3. **遇阻塞**：状态 🟡→🔴，进度日志写 reason + 等什么
4. **Epic 收尾**：状态 ✅ + 完成日期 + §7 对应 Milestone 的退出标准同步打勾
5. **Commit 消息**必须引用 Epic 号：`Epic #6: <动作>` / `Epic #3 partial: <动作>` / `Epic #14 blocked: <原因>`

**不遵守 = PR 不合格。** 详见 [DEV_ROADMAP.md §3.6 执行协议](docs/DEV_ROADMAP.md#36-执行协议execution-protocol-每次开发必读)。

配套策略背景见 [docs/archive/COMPETITIVE_GAP_ANALYSIS.archived.md](docs/archive/COMPETITIVE_GAP_ANALYSIS.archived.md)（为什么做这些 Epic）。

## Project

**XMclaw** is a local-first, self-evolving AI agent runtime written in Python. A single FastAPI daemon hosts the AgentLoop and composes LLM / Tool / Memory / Channel providers over a streaming `BehavioralEvent` bus; the Honest Grader + SkillScheduler + EvolutionController pipeline drives evidence-based skill promotion. Clients (Web UI, CLI, future desktop tray) connect to the daemon over WebSocket at `/agent/v2/{session_id}`. See [README.md](README.md) for the user-facing overview and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the definitive system design.

## Repository Layout

```
xmclaw/              Python package — see per-subdir AGENTS.md for contracts
├── core/            Bus, IR, grader, evolution, scheduler  → xmclaw/core/AGENTS.md
├── daemon/          FastAPI + WS + AgentLoop + lifecycle   → xmclaw/daemon/AGENTS.md
├── providers/       LLM / tool / memory / runtime / channel adapters
│                    Each subdir has its own AGENTS.md.    → xmclaw/providers/AGENTS.md
├── security/        Prompt-injection scanner + redactor    → xmclaw/security/AGENTS.md
├── skills/          SkillBase + registry + demo skills     → xmclaw/skills/AGENTS.md
├── cli/             `xmclaw` entry points + doctor         → xmclaw/cli/AGENTS.md
├── utils/           Path / log / redact / cost helpers     → xmclaw/utils/AGENTS.md
└── plugins/         Third-party plugins (Epic #2 WIP)

xmclaw/daemon/static/ Web UI (Preact + htm via ESM, no build step) — served at
                     `/ui/` via FastAPI `StaticFiles`. No Node.js required.
daemon/              Runtime config (config.json gitignored; config.example.json is the template)
docs/                ARCHITECTURE, DEV_ROADMAP, EVENTS, DOCTOR, WORKSPACE, V2_DEVELOPMENT, …
                     AGENTS_TEMPLATE.md is the template per-subdir AGENTS.md files follow.
tests/               pytest suites — see scripts/test_lanes.yaml for the smart-gate mapping
scripts/             Dev/ops — setup.{ps1,bat}, test_changed.py, check_import_direction.py, …
.github/workflows/   python-ci.yml (lint + smart-gate tests), release.yml, python-publish.yml
```

Runtime data (events.db, memory.db, pairing_token.txt, daemon.pid, …) lives under `~/.xmclaw/v2/`, *not* inside the repo — see [docs/WORKSPACE.md](docs/WORKSPACE.md). Anything not in the tree above is either gitignored dev scratch or legacy scaffolding — check `.gitignore` before assuming a root-level file belongs in git.

## Common Commands

```bash
# Install
pip install -e .                 # runtime (resolves from pyproject.toml)
pip install -e ".[dev]"          # + pytest, ruff, mypy, pip-tools

# Reproducible installs (pin exact versions)
pip install -r requirements-lock.txt          # prod
pip install -r requirements-dev-lock.txt      # prod + dev

# Regenerate lockfiles after editing pyproject.toml dependencies
pip-compile pyproject.toml --output-file requirements-lock.txt --strip-extras
pip-compile pyproject.toml --extra dev --output-file requirements-dev-lock.txt --strip-extras

# Run
xmclaw start                     # launch daemon + open web UI (http://127.0.0.1:8765)
xmclaw stop
xmclaw chat                      # interactive CLI
xmclaw chat --plan               # plan mode (approve steps first)
xmclaw config init               # interactive config
xmclaw doctor                    # diagnostics (see docs/DOCTOR.md)
xmclaw doctor --fix              # auto-remediate fixable check failures

# Test & lint
python -m pytest tests/ -v                    # full suite (slow)
python scripts/test_changed.py --dry-run      # smart-gate: only affected lanes (Epic #11)
python scripts/test_changed.py --all          # forced full suite via selector
python -m pytest tests/ --cov=xmclaw --cov-report=html
ruff check xmclaw/ --fix
mypy xmclaw/

# Build desktop installer (Windows)
python scripts/build_exe_fast.py
# then scripts/xmclaw_setup.iss produces the installer via InnoSetup
```

Dev env is Windows-first; scripts use `.bat` / `.ps1`. Use `bash` syntax on Git Bash / WSL — forward-slash paths work.

## Key Conventions

- **Per-subdir contracts.** Every `xmclaw/<subdir>/AGENTS.md` states that directory's responsibility, dependency rules, test entry points, hard no's, and key files. Before editing `xmclaw/foo/bar.py`, read `xmclaw/foo/AGENTS.md`.
- **Import direction is enforced.** `scripts/check_import_direction.py` blocks upward edges in the DAG (`core/` cannot import `providers/`, etc). Rules live in each subdir's AGENTS.md.
- **Config with secrets is gitignored.** `daemon/config.json` holds API keys — never commit. Use `daemon/config.example.json` or env vars prefixed with `XMC__` (e.g. `XMC__llm__anthropic__api_key`).
- **Events are the contract.** The daemon emits a typed event stream — see [docs/EVENTS.md](docs/EVENTS.md). Clients must not assume fields outside that schema.
- **Tool additions go to `xmclaw/providers/tool/`.** Register via `ToolProvider` ABC; update [docs/TOOLS.md](docs/TOOLS.md) + the `tools` lane in `scripts/test_lanes.yaml`.
- **Skill evolution is in-memory.** `xmclaw/skills/` + `xmclaw/core/scheduler/` + `xmclaw/core/evolution/` run the Honest-Grader-driven promotion pipeline; versions live in `SkillRegistry`, not in `shared/skill_*.py` files. See [docs/V2_DEVELOPMENT.md](docs/V2_DEVELOPMENT.md) §1–§3 for the controller / grader / scheduler contract.

## Git Workflow

- Main branch is `main`. **Direct push to `main` is the default for this repo** (single-author project; the user has explicitly opted out of PR review for routine work as of 2026-04-26). Push as soon as a commit is ready — do NOT spin up a feature branch + `gh pr create` for normal changes.
- The exception is when the user explicitly says "open a PR" / "use a branch" / "需要 review" — then create `feat/...` / `fix/...` / `docs/...` and run `gh pr create`.
- Push-to-main runs the full smart-gate via CI; that's the safety net, not pre-merge review.
- Keep commit messages in English (or Chinese — both fine; user is bilingual). Conventional Commits encouraged (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`).
- Epic-touching commits must cite the Epic number (`Epic #11:`, `Epic #14 partial:`, etc). See the 开发纪律 section above.

## Releasing

Releases are fully driven by semver tags. To cut a release:

1. Bump `version = "..."` in `pyproject.toml` on `main` (via PR).
2. After the bump lands, tag and push:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
3. `.github/workflows/release.yml` fires on the tag, builds the Windows `XMclaw.exe` + InnoSetup installer, verifies the tag matches `pyproject.toml`, and opens a **draft** GitHub release with both artifacts attached.
4. Review the draft on GitHub, edit the notes, then click **Publish** — this triggers `python-publish.yml` to upload to PyPI.

The workflow also accepts `workflow_dispatch` for re-builds against an existing tag.

## Prerequisites

- Python 3.10+ (see `pyproject.toml`).
- Optional: `playwright install chromium` for browser tools; `pyautogui` + `mss` for computer-use.
- No Node.js required. The web UI under `xmclaw/daemon/static/` is plain HTML/CSS/JS served by FastAPI — edit the files directly and refresh the browser.
