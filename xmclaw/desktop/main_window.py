import sys
import os
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
from PySide6.QtWebEngineCore import QWebEngineSettings

DAEMON_MODULE = "xmclaw.daemon.server"  # Python module path for daemon
WEB_URL = "http://127.0.0.1:8765"

# Compute log path relative to project root (parent of xmclaw/)
_XMCLAW_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DAEMON_LOG = os.path.join(_XMCLAW_ROOT, "logs", "daemon_desktop.log")


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
    """Start daemon via python -m to work regardless of install location."""
    log_dir = os.path.dirname(DAEMON_LOG)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    log_file = open(DAEMON_LOG, "w")

    startup_info = None
    if sys.platform == "win32":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = 1  # SW_SHOWNORMAL — show console for debugging

    process = subprocess.Popen(
        [sys.executable, "-m", DAEMON_MODULE],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        startupinfo=startup_info,
    )
    return process


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XMclaw")
        self.setMinimumSize(1400, 900)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self.web_view = QWebEngineView()
        self.web_view.settings().setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        self.web_view.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        self.web_view.loadFinished.connect(self._on_load_finished)
        self.web_view.load(QUrl(WEB_URL))
        layout.addWidget(self.web_view)

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

        QTimer.singleShot(500, self._ensure_visible)

    def _ensure_visible(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_load_finished(self, ok: bool):
        if not ok:
            print(f"[XMclaw] WebView failed to load {WEB_URL}")
            print(f"[XMclaw] Check daemon log: {DAEMON_LOG}")

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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("XMclaw")
    app.setQuitOnLastWindowClosed(False)

    if not is_daemon_running():
        print("[XMclaw Desktop] Daemon not running. Starting daemon...")
        proc = start_daemon()
        print("[XMclaw Desktop] Waiting for daemon (up to 15s)...")
        if not wait_for_daemon(timeout=15):
            print("[XMclaw Desktop] ERROR: Daemon failed to start!")
            print(f"[XMclaw Desktop] See log: {DAEMON_LOG}")
            print("[XMclaw Desktop] Note: A debug console window should have opened.")
            print("[XMclaw Desktop] Press Enter to exit...")
            input()
            sys.exit(1)
        print("[XMclaw Desktop] Daemon is ready!")

    win = MainWindow()
    win.show()
    sys.exit(app.exec())
