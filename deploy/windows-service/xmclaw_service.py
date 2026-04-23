"""Windows Service wrapper for the XMclaw daemon (pywin32 path).

See README.md in this directory for installation. This module is only
imported when the service is registered — it does NOT ship in the
xmclaw wheel, so pywin32 stays an opt-in deployment dependency rather
than a hard requirement.

The wrapper boots uvicorn in-process (same shape as ``xmclaw serve``) so
the service lifecycle owns the port bind directly — no child process to
supervise, no pidfile to reconcile if the service host kills us.

Stop semantics: ``SvcStop`` sets ``_should_stop`` and asks uvicorn to
exit. uvicorn's ``shutdown`` path flushes any in-flight requests before
returning, which matches XMclaw's own v2 lifecycle (see
``xmclaw/daemon/lifecycle.py``).
"""
from __future__ import annotations

import os
import sys

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError as exc:  # pragma: no cover — Windows-only dep
    raise SystemExit(
        "pywin32 is not installed. Run: pip install pywin32"
    ) from exc


class XMclawService(win32serviceutil.ServiceFramework):
    _svc_name_ = "XMclaw"
    _svc_display_name_ = "XMclaw Agent Daemon"
    _svc_description_ = (
        "Local-first AI agent runtime. Serves WebSocket + HTTP on "
        "127.0.0.1:8765 by default."
    )

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._server = None

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        if self._server is not None:
            self._server.should_exit = True

    def SvcDoRun(self) -> None:
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._run_uvicorn()

    def _run_uvicorn(self) -> None:
        import uvicorn

        from xmclaw.daemon.factory import load_config
        from xmclaw.daemon.app import create_app

        config = load_config()
        app = create_app(config)

        host = os.environ.get("XMCLAW_HOST", "127.0.0.1")
        port = int(os.environ.get("XMCLAW_PORT", "8765"))

        # Running uvicorn's Server directly (rather than uvicorn.run)
        # lets SvcStop flip `should_exit` cleanly without hammering the
        # process with a signal that Windows services shouldn't receive.
        cfg = uvicorn.Config(app, host=host, port=port, log_level="info")
        self._server = uvicorn.Server(cfg)
        self._server.run()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(XMclawService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(XMclawService)
