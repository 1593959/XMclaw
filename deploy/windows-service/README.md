# XMclaw as a Windows Service

This directory contains two ways to run the XMclaw daemon as a
background service on Windows. Pick one:

## Option 1: `nssm` (recommended — no Python service wrapper)

[NSSM](https://nssm.cc/) (Non-Sucking Service Manager) wraps any console
program as a Windows service without needing `pywin32` or a per-version
service host.

Install once, configure, start:

```powershell
# as Administrator
choco install nssm   # or: scoop install nssm
nssm install XMclaw "C:\Path\To\Python\Scripts\xmclaw.exe" "serve --host 127.0.0.1 --port 8765"
nssm set XMclaw AppEnvironmentExtra XMC_DATA_DIR=C:\ProgramData\XMclaw
nssm set XMclaw AppStdout C:\ProgramData\XMclaw\logs\stdout.log
nssm set XMclaw AppStderr C:\ProgramData\XMclaw\logs\stderr.log
nssm start XMclaw
```

Uninstall: `nssm remove XMclaw confirm`.

## Option 2: `pywin32` service wrapper

For deployments that already vendor `pywin32` (enterprise images that
disallow third-party tools like NSSM), the adjacent
`xmclaw_service.py` implements `win32serviceutil.ServiceFramework`.

Register it:

```powershell
# as Administrator — pywin32 must be installed system-wide
python xmclaw_service.py install
python xmclaw_service.py start
```

Remove: `python xmclaw_service.py remove`.

## Secrets

Do NOT embed API keys in service definitions — they land in the
registry in plaintext and are readable by any admin. Put them in
`%XMC_DATA_DIR%\config.json` with NTFS ACLs restricted to the service
account, or use `xmclaw config set-secret` (Epic #16 secrets store).
