import sys
import os

# Bypass system proxy for localhost connections (e.g. Clash proxy can't reach 127.0.0.1)
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

import subprocess
import time
import urllib.request

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QSystemTrayIcon, QMenu, QMessageBox
)
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage

DAEMON_MODULE = "xmclaw.daemon.server"  # Python module path for daemon
WEB_URL = "http://127.0.0.1:8765"

# Compute log path relative to project root (parent of xmclaw/)
_XMCLAW_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DAEMON_LOG = os.path.join(_XMCLAW_ROOT, "logs", "daemon_desktop.log")


class _DebugWebEnginePage(QWebEnginePage):
    """Capture JS console messages for debugging."""
    def javaScriptConsoleMessage(self, level, message, line, source):
        import sys
        prefix = {0: "[JS ERROR]", 1: "[JS WARN]", 2: "[JS INFO]"}.get(level, "[JS]")
        print(f"{prefix} {source}:{line} {message}", file=sys.stderr)


def wait_for_daemon(timeout=15, interval=0.5):
    """Poll /health until daemon is ready."""
    health_url = WEB_URL.rstrip("/") + "/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2):
                return True
        except Exception:
            time.sleep(interval)
    return False


def is_daemon_running():
    try:
        with urllib.request.urlopen(WEB_URL.rstrip("/") + "/health", timeout=2):
            return True
    except Exception:
        return False


def start_daemon():
    """Start daemon as detached process with log file."""
    log_dir = os.path.dirname(DAEMON_LOG)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    open(DAEMON_LOG, "w").close()
    log_file = open(DAEMON_LOG, "a")

    startup_info = None
    creation_flags = 0
    if sys.platform == "win32":
        startup_info = subprocess.STARTUPINFO()
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(
        [sys.executable, "-m", DAEMON_MODULE],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(_XMCLAW_ROOT),  # ensure daemon finds config relative to project root
        startupinfo=startup_info,
        creationflags=creation_flags,
    )
    return process


class MainWindow(QMainWindow):
    def __init__(self):
        import traceback
        try:
            super().__init__()
            self.setWindowTitle("XMclaw")
            self.setMinimumSize(1400, 900)

            central = QWidget()
            self.setCentralWidget(central)
            layout = QVBoxLayout(central)
            layout.setContentsMargins(0, 0, 0, 0)

            print("[Desktop] Creating QWebEngineView...", file=sys.stderr)
            self.web_view = QWebEngineView()
            print("[Desktop] QWebEngineView created, configuring settings...", file=sys.stderr)
            self.web_view.settings().setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
            self.web_view.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            self.web_view.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
            self.web_view.page().setBackgroundColor(Qt.white)
            debug_page = _DebugWebEnginePage(self.web_view.page())
            self.web_view.setPage(debug_page)
            self.web_view.loadFinished.connect(self._on_load_finished)
            print("[Desktop] WebView configured, adding to layout...", file=sys.stderr)
            layout.addWidget(self.web_view)
            # NOTE: do NOT call load() here — QWebEngine crashes if load() is called
            # inside __init__ before the event loop starts. Load it after window is shown.
            print("[Desktop] WebView added to layout (load deferred until shown)", file=sys.stderr)

            # System tray
            self.tray_icon = QSystemTrayIcon(self)
            tray_menu = QMenu()
            show_action = QAction("显示", self)
            show_action.triggered.connect(self.showNormal)
            quit_action = QAction("退出", self)
            quit_action.triggered.connect(self._on_quit)
            tray_menu.addAction(show_action)
            tray_menu.addAction(quit_action)
            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.show()

            print("[Desktop] Tray icon set up, calling _ensure_visible...", file=sys.stderr)
            QTimer.singleShot(500, self._ensure_visible)
            print("[Desktop] MainWindow.__init__ complete", file=sys.stderr)
        except Exception as e:
            print(f"[MainWindow FATAL] __init__ crashed: {e}", file=sys.stderr)
            traceback.print_exc()
            raise

    def _load_url(self):
        """Deferred URL load — must be called AFTER event loop starts."""
        print("[Desktop] Loading WebView URL: " + WEB_URL, file=sys.stderr)
        self.web_view.load(QUrl(WEB_URL))

    def _ensure_visible(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        # Defer load until after event loop is running
        QTimer.singleShot(100, self._load_url)

    def _on_load_finished(self, ok: bool):
        if not ok:
            try:
                log = open(DAEMON_LOG).read()
            except Exception:
                log = "(no log)"
            QMessageBox.warning(
                self,
                "XMclaw - Web UI Error",
                f"WebView failed to load {WEB_URL}\n\n"
                f"Recent daemon log:\n{log[-300:]}"
            )

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _on_quit(self):
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class _DebugWebEnginePage(QWebEnginePage):
    """Capture JS console messages for debugging."""
    def javaScriptConsoleMessage(self, level, message, line, source):
        import sys
        prefix = {0: "[JS ERROR]", 1: "[JS WARN]", 2: "[JS INFO]"}.get(level, "[JS]")
        print(f"{prefix} {source}:{line} {message}", file=sys.stderr)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("XMclaw")
    app.setQuitOnLastWindowClosed(False)

    if not is_daemon_running():
        print("[XMclaw Desktop] Daemon not running. Starting daemon...")
        proc = start_daemon()
        time.sleep(1)  # give daemon a moment to start or crash
        if proc.poll() is not None:
            # Daemon exited immediately — read log for error
            try:
                log = open(DAEMON_LOG).read()
            except Exception:
                log = "(no log)"
            msg = f"Daemon exited immediately (code {proc.returncode}).\n\nLog:\n{log[-500:]}"
            QMessageBox.critical(None, "XMclaw - Daemon Error", msg)
            sys.exit(1)
        print("[XMclaw Desktop] Waiting for daemon (up to 15s)...")
        if not wait_for_daemon(timeout=15):
            try:
                log = open(DAEMON_LOG).read()
            except Exception:
                log = "(no log)"
            msg = f"Daemon did not respond within 15s.\n\nRecent log:\n{log[-500:]}"
            QMessageBox.critical(None, "XMclaw - Daemon Timeout", msg)
            sys.exit(1)
        print("[XMclaw Desktop] Daemon is ready!")

    win = MainWindow()
    win.show()
    sys.exit(app.exec())
