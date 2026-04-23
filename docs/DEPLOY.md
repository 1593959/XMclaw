# Deploying XMclaw

XMclaw is primarily designed as a local-first daemon. This doc covers
the packaged paths for running it somewhere more durable than an
interactive shell: as a system service, in a container, or on a small
VPS / PaaS.

**All paths assume default state lives under `$XMC_DATA_DIR` (defaults
to `~/.xmclaw`).** That one env var relocates the entire workspace —
events.db, memory.db, skills, workspaces, pairing token. If a deploy
target doesn't let you bind-mount a persistent directory, don't use it.

> **Auth warning**: XMclaw's only built-in client auth is the pairing
> token written to `$XMC_DATA_DIR/pairing_token.txt` on first boot. It
> is NOT sufficient protection for the open internet. Every remote
> deployment below binds to `127.0.0.1` by default; if you need LAN or
> WAN access, put the daemon behind a reverse proxy that handles auth
> (Cloudflare Access, Tailscale serve, basic-auth nginx, etc.).

---

## 1. One-shot user install (Linux / macOS / Windows)

For a single-user workstation that should keep the CLI handy:

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.sh | bash
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.ps1 | iex
```

Both create an isolated venv at `~/.xmclaw-venv` and add `xmclaw` to
PATH. Re-run to upgrade in place. Follow with `xmclaw config init` and
`xmclaw start`.

---

## 2. System service (stays up across reboots)

### Linux (systemd)

```bash
sudo cp deploy/systemd/xmclaw.service /etc/systemd/system/
# edit User=, paths, secrets — see comments in the unit file
sudo systemctl daemon-reload
sudo systemctl enable --now xmclaw.service
journalctl -u xmclaw -f
```

### macOS (launchd)

```bash
cp deploy/launchd/com.xmclaw.daemon.plist ~/Library/LaunchAgents/
# edit EnvironmentVariables — see comments in the plist
launchctl load -w ~/Library/LaunchAgents/com.xmclaw.daemon.plist
tail -f /tmp/xmclaw.out.log
```

### Windows

Two options, both documented in [`deploy/windows-service/README.md`](../deploy/windows-service/README.md):

- **nssm** — wraps `xmclaw.exe` as a Windows service without pywin32.
- **pywin32** — `deploy/windows-service/xmclaw_service.py` implements
  the native service-framework shape.

---

## 3. Container (Docker / Compose)

Single container:

```bash
docker build -t xmclaw/xmclaw:latest .
docker run -d \
  --name xmclaw \
  -p 127.0.0.1:8765:8765 \
  -v $HOME/.xmclaw:/data \
  -e XMC__llm__anthropic__api_key=sk-ant-... \
  xmclaw/xmclaw:latest
```

Compose (includes healthcheck, named volume, `.env` support):

```bash
cp .env.example .env  # fill ANTHROPIC_API_KEY=
docker compose up -d
docker compose logs -f xmclaw
```

The compose file has a commented-out Playwright sidecar — uncomment if
you want the browser tools under `xmclaw/providers/tool/browser.py` to
have a remote Chromium to drive.

---

## 4. Cloud (Fly.io)

`deploy/fly/fly.toml` is a working template for a single-machine Fly
app. The machine mounts a persistent volume at `/data` so redeploys
keep memory + events history intact.

```bash
cd deploy/fly
flyctl apps create xmclaw-<yourname>     # edit app = "..." in fly.toml
flyctl volumes create xmclaw_data --region iad --size 1
flyctl secrets set ANTHROPIC_API_KEY=sk-ant-...
flyctl deploy
```

Because the event bus is SQLite, **do not scale above 1 machine** —
two instances would race on the same volume. Horizontal scaling is on
the roadmap and not ready.

Other clouds (AWS ECS, Railway, Render) work with the same Dockerfile;
the only things to configure are a persistent volume, a secret for the
LLM API key, and a health-check path of `/health`. Templates for those
are not in the repo yet — if you build one that's generally useful,
open a PR.

---

## Upgrading

- **Venv install** (options 1 + 2): re-run the installer, or
  `xmclaw-venv/bin/pip install --upgrade xmclaw`. systemd / launchd
  keep running; restart the unit to pick up the new version.
- **Container**: pull / rebuild the image and restart the container.
  The `/data` volume carries state across image versions.
- **Schema migrations**: there is no Alembic-style migration layer
  today — new columns are added in-place and old databases stay
  readable. Before a major version bump, always take a backup with
  `xmclaw backup create`.

## Troubleshooting

- `xmclaw doctor` runs ~14 environment checks and prints actionable
  advice. `xmclaw doctor --fix` auto-remediates the fixable ones.
- `journalctl -u xmclaw` / `docker compose logs xmclaw` /
  `/tmp/xmclaw.err.log` — the daemon writes structured logs (Epic
  #10). Tail them during a hang.
- Port 8765 already in use? Override with `--port` on the CLI or
  `XMCLAW_PORT` in the Windows service / `--host 127.0.0.1 --port
  <n>` in the systemd/launchd ExecStart / ProgramArguments.
