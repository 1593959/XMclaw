# Desktop Application

XMclaw uses a **Browser + System Tray** architecture for its desktop experience.

## Architecture

```
┌─────────────────────────────────────────┐
│           Desktop Entry                  │
│         (python -m xmclaw.desktop.app)  │
└─────────────────┬───────────────────────┘
                  │
        ┌─────────▼─────────┐
        │   TrayApp         │
        │   (pystray)       │
        │   - System tray    │
        │   - Menu actions   │
        └─────────┬─────────┘
                  │
        ┌─────────▼─────────┐
        │   Browser         │
        │   (default)       │
        │   → Web UI        │
        └───────────────────┘
                  │
        ┌─────────▼─────────┐
        │   Daemon          │
        │   (FastAPI)       │
        │   WebSocket/HTTP  │
        └───────────────────┘
```

## Why Browser + System Tray?

| PySide6 WebView | Browser + System Tray |
|-----------------|----------------------|
| ❌ Heavy dependencies | ✅ Lightweight |
| ❌ Complex packaging | ✅ No special packaging |
| ❌ Crashes on some systems | ✅ Stable |
| ❌ Limited browser features | ✅ Full browser capabilities |
| ✅ Native look | ✅ Web UI consistency |

## Requirements

```bash
pip install pystray Pillow
```

## Usage

### Start Desktop App

```bash
python -m xmclaw.desktop.app
```

### System Tray Menu

- **Open Browser** - Open the web UI in your default browser
- **Check Status** - Check if daemon is running
- **Start Daemon** - Start the daemon if not running
- **Stop Daemon** - Stop the daemon
- **Exit** - Exit the desktop app

## Files

| File | Description |
|------|-------------|
| `xmclaw/desktop/app.py` | Entry point, daemon lifecycle management |
| `xmclaw/desktop/tray.py` | System tray with pystray |
| `xmclaw/desktop/ws_client.py` | WebSocket client (optional) |
| `xmclaw/desktop/http_client.py` | HTTP client for daemon communication |

## Configuration

The desktop app reads from `daemon/config.json`:

```json
{
  "gateway": {
    "host": "127.0.0.1",
    "port": 8765
  }
}
```

## Flow

1. Desktop app starts
2. Checks if daemon is running (via `/health` endpoint)
3. If not running, starts daemon automatically
4. Opens default browser to web UI
5. Shows system tray icon
6. User interacts via browser
7. Exit via tray menu or window close

## Troubleshooting

### Browser doesn't open

- Check if daemon is accessible: `curl http://127.0.0.1:8765/health`
- Check logs: `logs/daemon_desktop.log`

### Tray icon not visible

- On Linux, may need `sudo` for system tray
- Check if pystray dependencies installed: `pip install pystray Pillow`

### Daemon fails to start

- Check port availability: port 8765
- Check logs: `logs/daemon_desktop.log`
- Verify Python path: `python -c "import xmclaw.daemon.server"`
