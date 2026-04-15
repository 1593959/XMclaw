"""PySide6 desktop main window for XMclaw."""
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QListWidget,
    QSystemTrayIcon, QMenu, QApplication, QScrollArea, QFrame
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QAction

from xmclaw.desktop.ws_client import WSClientThread
from xmclaw.daemon.lifecycle import start_daemon, is_running
from xmclaw.utils.log import logger


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XMclaw")
        self.setMinimumSize(1100, 750)
        self.setStyleSheet("""
            QMainWindow { background-color: #0f0f0f; }
            QLineEdit { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #2a2a2a; border-radius: 10px; padding: 10px; font-size: 13px; }
            QPushButton { background-color: #00d4aa; color: #000000; border: none; border-radius: 10px; padding: 10px 22px; font-size: 13px; font-weight: 600; }
            QPushButton:hover { background-color: #00b894; }
            QPushButton:disabled { background-color: #2a2a2a; color: #666; }
            QListWidget { background-color: #141414; color: #e0e0e0; border: none; font-size: 13px; padding: 8px; }
            QListWidget::item { padding: 10px 12px; border-radius: 8px; }
            QListWidget::item:selected { background-color: #1f3a3a; }
            QListWidget::item:hover { background-color: #1f1f1f; }
            QScrollArea { border: none; background: #0f0f0f; }
        """)
        self.agent_id = "default"
        self.ws_thread = None
        self.current_agent_bubble = None
        self._build_ui()
        self._setup_tray()
        self._ensure_daemon()
        self._connect_ws()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(240)
        self.sidebar.addItem("Chat")
        self.sidebar.addItem("Settings")
        self.sidebar.addItem("Tools")
        self.sidebar.addItem("Memory")
        self.sidebar.addItem("Evolution")
        self.sidebar.setCurrentRow(0)
        layout.addWidget(self.sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(20, 16, 20, 16)
        main_layout.setSpacing(12)

        header = QLabel("XMclaw")
        header.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        header.setStyleSheet("color: #00d4aa; padding-bottom: 4px;")
        main_layout.addWidget(header)

        sub = QLabel("Local-first, self-evolving AI Agent")
        sub.setStyleSheet("color: #888; font-size: 12px; padding-bottom: 8px;")
        main_layout.addWidget(sub)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch()
        self.scroll.setWidget(self.chat_container)
        main_layout.addWidget(self.scroll)

        input_row = QHBoxLayout()
        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("Type a message and press Enter...")
        self.input_box.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_box)

        self.send_btn = QPushButton("Send")
        self.send_btn.setFixedWidth(90)
        self.send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self.send_btn)
        main_layout.addLayout(input_row)

        self.status_label = QLabel("Connecting...")
        self.status_label.setStyleSheet("color: #666; font-size: 11px;")
        main_layout.addWidget(self.status_label)
        layout.addWidget(main)

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setVisible(True)
        self.tray.activated.connect(self._on_tray_activated)
        menu = QMenu()
        show_act = QAction("Show", self)
        show_act.triggered.connect(self.showNormal)
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(show_act)
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)

    def _ensure_daemon(self):
        if not is_running():
            logger.info("desktop_starting_daemon")
            start_daemon()

    def _connect_ws(self):
        self.ws_thread = WSClientThread(self.agent_id)
        self.ws_thread.message_received.connect(self._on_ws_message)
        self.ws_thread.connection_changed.connect(self._on_connection_changed)
        self.ws_thread.start()

    def _on_send(self):
        text = self.input_box.text().strip()
        if not text:
            return
        self._add_user_message(text)
        self.input_box.clear()
        self.current_agent_bubble = None
        if self.ws_thread:
            self.ws_thread.send(text)

    def _add_user_message(self, text: str):
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignmentFlag.AlignRight)
        bubble = ChatBubble(text, True)
        bubble.setMaximumWidth(int(self.width() * 0.65))
        row.addWidget(bubble)
        self.chat_layout.insertLayout(self.chat_layout.count() - 1, row)
        self._scroll_to_bottom()

    def _add_agent_chunk(self, text: str):
        if self.current_agent_bubble:
            self.current_agent_bubble.append_text(text)
        else:
            row = QHBoxLayout()
            row.setAlignment(Qt.AlignmentFlag.AlignLeft)
            self.current_agent_bubble = ChatBubble(text, False)
            self.current_agent_bubble.setMaximumWidth(int(self.width() * 0.75))
            row.addWidget(self.current_agent_bubble)
            self.chat_layout.insertLayout(self.chat_layout.count() - 1, row)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        vsb = self.scroll.verticalScrollBar()
        vsb.setValue(vsb.maximum())

    def _on_ws_message(self, msg_type: str, content: str):
        if msg_type == "chunk":
            self._add_agent_chunk(content)
        elif msg_type == "done":
            self.current_agent_bubble = None
        elif msg_type == "error":
            self._add_agent_chunk(f"[Error: {content}]")
            self.current_agent_bubble = None

    def _on_connection_changed(self, connected: bool):
        if connected:
            self.status_label.setText("Connected")
            self.status_label.setStyleSheet("color: #00d4aa; font-size: 11px;")
        else:
            self.status_label.setText("Disconnected - retrying...")
            self.status_label.setStyleSheet("color: #ff6b6b; font-size: 11px;")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showNormal()

    def closeEvent(self, event):
        self.hide()
        event.ignore()

