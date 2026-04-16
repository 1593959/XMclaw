import sys
import os
import subprocess
import time

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QLineEdit, QSplitter,
    QFileDialog, QMessageBox, QSystemTrayIcon, QMenu, QFrame,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QTableWidget, QTableWidgetItem, QCheckBox, QComboBox,
    QPlainTextEdit, QDialog, QDialogButtonBox, QFormLayout,
    QSpinBox, QDoubleSpinBox, QTabWidget, QListWidget, QListWidgetItem,
    QInputDialog
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QSize
from PySide6.QtGui import QIcon, QAction, QColor, QPalette
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

DAEMON_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "daemon", "server.py"))
WEB_URL = "http://127.0.0.1:8080"


def is_daemon_running():
    try:
        import urllib.request
        with urllib.request.urlopen(WEB_URL, timeout=1):
            return True
    except Exception:
        return False


def start_daemon():
    if sys.platform == "win32":
        subprocess.Popen(
            [sys.executable, DAEMON_PATH],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            [sys.executable, DAEMON_PATH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XMclaw")
        self.setMinimumSize(1400, 900)
        self.setStyleSheet(self._build_stylesheet())

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Web view
        self.web_view = QWebEngineView()
        self.web_view.settings().setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        self.web_view.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        self.web_view.load(QUrl(WEB_URL))
        layout.addWidget(self.web_view)

        # System tray
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.windowIcon())
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

        # Timer to ensure window is visible
        QTimer.singleShot(500, self._ensure_visible)

    def _build_stylesheet(self):
        return """
        QMainWindow { background: #FDF8F3; }
        """

    def _ensure_visible(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

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
        start_daemon()
        time.sleep(2)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
