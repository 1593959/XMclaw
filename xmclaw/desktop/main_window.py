"""PySide6 desktop main window for XMclaw."""
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QListWidget,
    QSystemTrayIcon, QMenu, QApplication, QScrollArea, QFrame,
    QTextEdit, QSplitter, QDialog, QDialogButtonBox, QMessageBox,
    QCheckBox, QListWidgetItem
)
from PySide6.QtCore import Qt, QTimer
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
        self.setWindowTitle("XMclaw")
        self.setMinimumSize(1200, 800)
        self.setStyleSheet("""
            QMainWindow { background-color: #0f0f0f; }
            QLineEdit { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #2a2a2a; border-radius: 10px; padding: 10px; font-size: 13px; }
            QTextEdit { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #2a2a2a; border-radius: 10px; padding: 10px; font-size: 13px; }
            QPushButton { background-color: #00d4aa; color: #000000; border: none; border-radius: 10px; padding: 10px 22px; font-size: 13px; font-weight: 600; }
            QPushButton:hover { background-color: #00b894; }
            QPushButton:disabled { background-color: #2a2a2a; color: #666; }
            QPushButton#planBtn { background-color: #2a2a2a; color: #e0e0e0; }
            QPushButton#planBtn:checked { background-color: #1f3a3a; color: #00d4aa; }
            QListWidget { background-color: #141414; color: #e0e0e0; border: none; font-size: 13px; padding: 8px; }
            QListWidget::item { padding: 10px 12px; border-radius: 8px; }
            QListWidget::item:selected { background-color: #1f3a3a; }
            QListWidget::item:hover { background-color: #1f1f1f; }
            QScrollArea { border: none; background: #0f0f0f; }
            QLabel { color: #e0e0e0; }
            QSplitter::handle { background: #2a2a2a; }
        """)
        self.agent_id = "default"
        self.ws_thread = None
        self.current_agent_bubble = None
        self.plan_mode = False
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

        # 侧边栏
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(220)
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
        layout.addWidget(self.sidebar)

        # 主区域
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(20, 16, 20, 16)
        main_layout.setSpacing(12)

        # 顶部栏
        topbar = QHBoxLayout()
        self.topbar_title = QLabel("仪表盘")
        self.topbar_title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        self.topbar_title.setStyleSheet("color: #00d4aa;")
        topbar.addWidget(self.topbar_title)
        topbar.addStretch()
        self.state_badge = QLabel("空闲")
        self.state_badge.setStyleSheet("background-color: #2a2a2a; color: #e0e0e0; padding: 4px 12px; border-radius: 12px; font-size: 12px;")
        topbar.addWidget(self.state_badge)
        main_layout.addLayout(topbar)

        # 计划模式横幅
        self.plan_banner = QLabel("计划模式已开启 — XMclaw 将先思考再行动")
        self.plan_banner.setStyleSheet("background-color: #1f3a3a; color: #00d4aa; padding: 8px 14px; border-radius: 8px; font-size: 13px;")
        self.plan_banner.setVisible(False)
        main_layout.addWidget(self.plan_banner)

        # 内容区分割
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧聊天区
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_container)
        left_layout.addWidget(self.chat_scroll)

        input_row = QHBoxLayout()
        self.plan_btn = QPushButton("计划")
        self.plan_btn.setObjectName("planBtn")
        self.plan_btn.setCheckable(True)
        self.plan_btn.setFixedWidth(70)
        self.plan_btn.clicked.connect(self._toggle_plan)
        input_row.addWidget(self.plan_btn)
        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("向 XMclaw 发送指令...")
        self.input_box.setFixedHeight(60)
        self.input_box.textChanged.connect(self._auto_resize_input)
        input_row.addWidget(self.input_box, 1)
        send_btn = QPushButton("发送")
        send_btn.setFixedWidth(80)
        send_btn.clicked.connect(self._send_message)
        input_row.addWidget(send_btn)
        left_layout.addLayout(input_row)
        splitter.addWidget(left)

        # 右侧面板
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        # 状态面板
        state_panel = QVBoxLayout()
        state_title = QLabel("智能体状态")
        state_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        state_panel.addWidget(state_title)
        self.current_thought = QLabel("等待输入...")
        self.current_thought.setStyleSheet("color: #aaa; font-size: 12px;")
        state_panel.addWidget(QLabel("当前思考:"))
        state_panel.addWidget(self.current_thought)
        self.active_tool_label = QLabel("—")
        self.active_tool_label.setStyleSheet("color: #aaa; font-size: 12px;")
        state_panel.addWidget(QLabel("活跃工具:"))
        state_panel.addWidget(self.active_tool_label)
        self.active_file_label = QLabel("—")
        self.active_file_label.setStyleSheet("color: #aaa; font-size: 12px;")
        state_panel.addWidget(QLabel("文件操作:"))
        state_panel.addWidget(self.active_file_label)
        right_layout.addLayout(state_panel)

        # 待办面板
        todo_title = QLabel("待办事项")
        todo_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        right_layout.addWidget(todo_title)
        self.todo_list = QListWidget()
        right_layout.addWidget(self.todo_list)

        # 任务面板
        task_title = QLabel("任务列表")
        task_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        right_layout.addWidget(task_title)
        self.task_list = QListWidget()
        right_layout.addWidget(self.task_list)

        # 工具调用面板
        tool_title = QLabel("最近工具调用")
        tool_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        right_layout.addWidget(tool_title)
        self.tool_list = QListWidget()
        right_layout.addWidget(self.tool_list)

        right_layout.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([800, 360])
        main_layout.addWidget(splitter, 1)
        layout.addWidget(main, 1)

    def _auto_resize_input(self):
        doc = self.input_box.document()
        h = int(doc.size().height()) + 16
        self.input_box.setFixedHeight(min(max(h, 60), 160))

    def _toggle_plan(self):
        self.plan_mode = self.plan_btn.isChecked()
        self.plan_banner.setVisible(self.plan_mode)

    def _on_sidebar_clicked(self, item):
        key = item.data(Qt.ItemDataRole.UserRole)
        titles = {
            "dashboard": "仪表盘",
            "workspace": "工作区",
            "evolution": "进化",
            "memory": "记忆",
            "tools": "工具日志",
            "settings": "设置",
        }
        self.topbar_title.setText(titles.get(key, key))
        if key == "workspace":
            self._load_workspace()
        elif key == "evolution":
            self._load_evolution()

    def _add_bubble(self, text: str, is_user: bool):
        bubble = ChatBubble(text, is_user)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        self.current_agent_bubble = None if is_user else bubble
        QTimer.singleShot(50, lambda: self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))
        return bubble

    def _send_message(self):
        text = self.input_box.toPlainText().strip()
        if not text:
            return
        prefix = "[PLAN MODE] " if self.plan_mode else ""
        self._add_bubble(text, True)
        self.input_box.clear()
        if self.ws_thread and self.ws_thread.isRunning():
            self.ws_thread.send_message(prefix + text)
        self._set_state("思考中", "分析请求中...")

    def _set_state(self, badge: str, thought: str):
        self.state_badge.setText(badge)
        self.current_thought.setText(thought)

    def _load_workspace(self):
        pass

    def _load_evolution(self):
        pass

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setVisible(True)
        tray_menu = QMenu()
        show_action = QAction("显示", self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._on_tray_activated)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showNormal()

    def closeEvent(self, event):
        self.hide()
        event.ignore()

    def _ensure_daemon(self):
        if not is_running():
            logger.info("启动 XMclaw daemon...")
            start_daemon()

    def _connect_ws(self):
        self.ws_thread = WSClientThread(self.agent_id)
        self.ws_thread.message_received.connect(self._on_ws_message)
        self.ws_thread.chunk_received.connect(self._on_chunk)
        self.ws_thread.state_changed.connect(self._on_state_change)
        self.ws_thread.ask_user.connect(self._on_ask_user)
        self.ws_thread.tool_called.connect(self._on_tool_called)
        self.ws_thread.connection_changed.connect(self._on_connection_change)
        self.ws_thread.start()

    def _on_ws_message(self, msg: dict):
        role = msg.get("role", "agent")
        content = msg.get("content", "")
        if role == "agent":
            self._add_bubble(content, False)
        elif role == "tool":
            self._add_bubble(f"[工具结果] {content}", False)
        elif role == "system":
            self._add_bubble(f"[系统] {content}", False)

    def _on_chunk(self, text: str):
        if self.current_agent_bubble is None:
            self.current_agent_bubble = self._add_bubble("", False)
        self.current_agent_bubble.append_text(text)
        QTimer.singleShot(50, lambda: self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))

    def _on_state_change(self, state: str, detail: str):
        state_map = {
            "IDLE": "空闲",
            "THINKING": "思考中",
            "WAITING": "等待中",
            "TOOL_CALL": "调用工具",
            "SELF_MOD": "自修改",
        }
        self.state_badge.setText(state_map.get(state, state))
        self.current_thought.setText(detail or "—")

    def _on_ask_user(self, message: str):
        self._set_state("等待中", "等待用户确认...")
        dlg = AskUserDialog(message, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            answer = dlg.get_answer()
            if self.ws_thread and self.ws_thread.isRunning():
                self.ws_thread.send_message(f"[RESUME] {answer}")
        else:
            if self.ws_thread and self.ws_thread.isRunning():
                self.ws_thread.send_message("[RESUME] 用户取消了操作")

    def _on_tool_called(self, tool: dict):
        name = tool.get("name", "未知工具")
        result = tool.get("result", "")
        item = QListWidgetItem(f"{name}: {str(result)[:80]}")
        self.tool_list.addItem(item)
        self.tool_list.scrollToBottom()

    def _on_connection_change(self, connected: bool):
        if connected:
            self._set_state("空闲", "已连接")
        else:
            self._set_state("断开", "重新连接中...")
