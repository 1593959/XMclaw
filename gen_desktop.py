import textwrap

content = textwrap.dedent(r'''
"""PySide6 desktop main window for XMclaw."""
import json
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QListWidget,
    QSystemTrayIcon, QMenu, QApplication, QScrollArea, QFrame,
    QTextEdit, QSplitter, QDialog, QDialogButtonBox, QMessageBox,
    QCheckBox, QListWidgetItem, QStackedWidget, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QFileDialog, QFormLayout, QComboBox, QSpinBox,
    QGroupBox, QTabWidget
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QFont, QAction

from xmclaw.desktop.ws_client import WSClientThread
from xmclaw.daemon.lifecycle import start_daemon, is_running
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR, get_agent_dir


class HttpWorker(QThread):
    result = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            import asyncio
            result = asyncio.run(self.fn())
            self.result.emit({"ok": True, "data": result})
        except Exception as e:
            self.result.emit({"ok": False, "error": str(e)})


class ChatBubble(QFrame):
    def __init__(self, text: str, is_user: bool, parent=None):
        super().__init__(parent)
        self.is_user = is_user
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setFont(QFont("Segoe UI", 11))
        self.label.setContentsMargins(12, 8, 12, 8)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.update_style()

    def update_style(self):
        if self.is_user:
            self.setStyleSheet("""
                ChatBubble { background-color: #1f3a3a; border-radius: 12px; border-bottom-right-radius: 4px; }
                QLabel { color: #e0e0e0; background: transparent; }
            """)
        else:
            self.setStyleSheet("""
                ChatBubble { background-color: #2a2a2a; border-radius: 12px; border-bottom-left-radius: 4px; }
                QLabel { color: #e0e0e0; background: transparent; }
            """)

    def append_text(self, text: str):
        self.label.setText(self.label.text() + text)


class AskUserDialog(QDialog):
    def __init__(self, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("XMclaw 需要您的确认")
        self.setMinimumWidth(420)
        self.setStyleSheet("""
            QDialog { background-color: #1a1a1a; color: #e0e0e0; }
            QLabel { color: #e0e0e0; }
            QTextEdit { background-color: #0f0f0f; color: #e0e0e0; border: 1px solid #2a2a2a; border-radius: 8px; padding: 8px; }
            QPushButton { background-color: #00d4aa; color: #000; border: none; border-radius: 8px; padding: 8px 18px; font-weight: 600; }
            QPushButton:hover { background-color: #00b894; }
        """)
        layout = QVBoxLayout(self)
        msg = QLabel(message)
        msg.setWordWrap(True)
        layout.addWidget(msg)
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("输入回复...")
        self.text_edit.setMaximumHeight(120)
        layout.addWidget(self.text_edit)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_answer(self) -> str:
        return self.text_edit.toPlainText().strip()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.agent_id = "default"
        self.plan_mode = False
        self.current_agent_bubble = None
        self.ws_thread = None
        self._pending_http = []

        self.setWindowTitle("XMclaw")
        self.setMinimumSize(1280, 800)
        self.setStyleSheet(self._stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 侧边栏
        sidebar = QWidget()
        sidebar.setFixedWidth(180)
        sidebar.setStyleSheet("background-color: #111;")
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(12, 16, 12, 16)
        sb_layout.setSpacing(8)

        logo = QLabel("XMclaw")
        logo.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        logo.setStyleSheet("color: #00d4aa;")
        sb_layout.addWidget(logo)

        tagline = QLabel("自进化智能体 OS")
        tagline.setStyleSheet("color: #666; font-size: 11px;")
        sb_layout.addWidget(tagline)
        sb_layout.addSpacing(20)

        self.sidebar = QListWidget()
        self.sidebar.setStyleSheet("""
            QListWidget { background: transparent; border: none; outline: none; }
            QListWidget::item { color: #aaa; padding: 10px 8px; border-radius: 6px; }
            QListWidget::item:selected { background-color: #1f3a3a; color: #00d4aa; }
            QListWidget::item:hover { background-color: #1a1a1a; }
        """)
        items = [
            ("仪表盘", "dashboard"),
            ("工作区", "workspace"),
            ("进化", "evolution"),
            ("记忆", "memory"),
            ("工具日志", "tools"),
            ("设置", "settings"),
        ]
        self._sidebar_map = {}
        for label, key in items:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.sidebar.addItem(item)
            self._sidebar_map[key] = item
        self.sidebar.setCurrentRow(0)
        self.sidebar.itemClicked.connect(self._on_sidebar_clicked)
        sb_layout.addWidget(self.sidebar)
        sb_layout.addStretch()

        self.conn_status = QLabel("● 未连接")
        self.conn_status.setStyleSheet("color: #e74c3c; font-size: 12px;")
        sb_layout.addWidget(self.conn_status)
        layout.addWidget(sidebar)

        # 主区域
        self.stack = QStackedWidget()
        self._build_dashboard()
        self._build_workspace()
        self._build_evolution()
        self._build_memory()
        self._build_tools()
        self._build_settings()
        layout.addWidget(self.stack, 1)

        self._setup_tray()
        self._ensure_daemon()
        QTimer.singleShot(500, self._connect_ws)
        QTimer.singleShot(1000, self._refresh_all_data)

    def _stylesheet(self):
        return """
        QMainWindow { background-color: #0f0f0f; }
        QWidget { font-family: "Segoe UI", "Microsoft YaHe