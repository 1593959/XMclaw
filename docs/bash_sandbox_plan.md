# bash tool sandbox — design proposal

**Status:** proposal, 2026-05-26 audit F3.
**Current state:** `xmclaw/providers/tool/builtin_shell.py` runs `subprocess.run(command, shell=True, ...)` with the daemon's full process privileges. The user already implicitly trusts the agent with their machine, but the audit (rightly) flagged that one prompt-injection → one `bash` call away from "delete the user's home directory".

## Why this needs careful design

XMclaw runs **on the user's machine** by design — that's the whole pitch (local-first, the agent has real filesystem / shell / browser). The bash tool exists because users want it to:

- read system logs
- run `git status` / `pytest` / build commands
- launch other programs

These all need access to **the user's real filesystem and user-installed tools**. A sandbox that breaks any of those breaks the product.

So the design constraint is: **filter the dangerous edges, not the everyday surface**. Specifically:

| keep allowed (everyday) | must reject (dangerous) |
|---|---|
| `git status`, `ls`, `find`, `grep` | `rm -rf /` style mass-deletion |
| `pip install ...`, `npm install` | direct kernel / system-config writes (`mkfs`, `dd of=/dev/...`) |
| `python script.py`, `pytest` | persistent backdoors (cron entries, login items, registry writes for autostart) |
| read any file the user can read | overwriting credentials files (`~/.ssh/`, `~/.aws/credentials`, browser cookie DBs) without explicit confirmation |
| local network (localhost) | outbound to unusual ports (mining, C2 callback) |

## Three-layer approach

### Layer 1 — command-pattern allow/deny list (cheap, immediate)

Before spawning the subprocess, scan `command` against:

- **deny patterns** that always block: `rm -rf /`, `:(){:|:&};:`, `> /dev/sda`, `dd if=/dev/zero`, anything piping curl into bash/sh, `mkfs`, `chmod -R 777 /`, `chown -R root`.
- **confirm patterns** that require explicit user confirmation (via `ask_user_question` tool) before running: writes to `~/.ssh/`, `~/.aws/`, `~/.gnupg/`, `~/.config/google-chrome/Cookies`, `~/.mozilla/firefox/*/cookies.sqlite`; `crontab -e` / `crontab <`; `reg add` for autostart keys; `schtasks /create`; `launchctl load`; anything starting with `sudo` on POSIX.

Implementation: pure regex against the command string before `subprocess.run`. Adds ~1 ms latency, zero new dependencies. Effort: 1 day including tests.

### Layer 2 — OS-level sandbox (medium effort, platform-specific)

Where the OS already ships a sandbox, use it:

- **Windows**: [Windows Sandbox](https://learn.microsoft.com/windows/security/threat-protection/windows-sandbox/windows-sandbox-overview) is built into Windows 10/11 Pro+. Containers are lightweight, no licensing cost. Limitation: not available on Windows Home. Fallback for Home users: Layer 1 only.
- **macOS**: `sandbox-exec` (deprecated by Apple but still functional) or a minimal SIP-style profile.
- **Linux**: `firejail` (apt-installable on Debian/Ubuntu/Arch) or `bubblewrap` (more modern, used by Flatpak). Both unprivileged-user sandboxes.

The design: when the user enables `tools.bash.sandbox = "auto"` in config, the daemon detects available sandboxes at boot. Each bash invocation gets wrapped:

```
firejail --read-only=/etc --read-only=/usr --net=none --no3d \
    --quiet bash -c "<command>"
```

Net: deny-by-default on the dangerous edges (system dirs read-only, network off, no 3D acceleration), allow everything else. The user can override per-tool-call via a `network: true` / `writable_dirs: [...]` arg — but those overrides land in the doctor log so the operator sees the audit trail.

Effort: ~2 days per platform.

### Layer 3 — container-based sandbox (high effort, opt-in)

For users who want the strictest isolation, ship a `tools.bash.sandbox = "docker"` mode that spawns each bash call inside a Docker container with the user's project directory bind-mounted read/write and nothing else. Network restricted to docker's bridge.

This is the strongest isolation but breaks all the "run a tool the user installed locally" workflows — `git`, `pytest`, `npm` would have to be inside the container. So this is opt-in for security-paranoid users, not the default.

Effort: ~1 week including docs and the "tool unavailable in sandbox" error UX.

## Decision matrix for the user

| Layer | Effort | Breaks workflows? | Coverage |
|---|---|---|---|
| 1 (patterns) | 1 day | No | ~70% of attack surface |
| 2 (OS sandbox) | 2-3 days | Mostly no (network off by default) | ~95% |
| 3 (Docker) | ~1 week | Yes (tool availability) | ~99% |

## Recommendation

Ship **Layer 1 immediately** as part of this audit. Layers 2 and 3 are larger projects that need their own sprints. Layer 1 alone catches the most common attack patterns (the things a regex obviously matches) without breaking any workflow.

## Layer 1 — concrete plan

1. New module `xmclaw/providers/tool/bash_guardrails.py` with `classify_command(command) -> Verdict` returning one of:
   - `allow` — pass through to subprocess
   - `deny(reason)` — return failed ToolResult with explanation
   - `confirm(prompt)` — call `ask_user_question` (already exposed); proceed only on YES
2. Pattern list lives in the module — short and readable, similar to `toxic_facts.py` shape. Each pattern has a short id for the audit log.
3. Wire into `builtin_shell.py` between argument parsing and `_run()`. Guard short-circuits on the first match.
4. Config flag `tools.bash.guardrails.enabled` (default true). When false, restores legacy behavior.
5. Tests: every deny pattern + a sample of allow patterns + the confirm flow.

Estimated effort: 4-6 hours of work, ~30 new tests.

## Open questions

- Should `confirm` also fire for `pip install` of unknown packages? (Per-package allow-list is a 10x bigger project — defer.)
- Multi-shell support: PowerShell `Remove-Item -Recurse -Force C:\` is the equivalent of `rm -rf /`. Pattern list must cover both. Done in scope.
- Path interpolation: `rm -rf "$HOME"` is the same attack as `rm -rf ~`. Patterns must match interpolated forms too.

## Conclusion

Build Layer 1 now. Layers 2-3 are real projects that need user sign-off on the trade-offs (Win Home users excluded from Layer 2; Docker users lose host-tool access in Layer 3).
