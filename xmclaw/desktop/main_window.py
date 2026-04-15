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
        self.resize(1400, 900)
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
        QWidget { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
        QLabel { color: #e0e0e0; }
        QPushButton {
            background-color: #00d4aa; color: #000; border: none; border-radius: 8px; padding: 8px 16px;
            font-weight: 600;
        }
        QPushButton:hover { background-color: #00b894; }
        QPushButton:disabled { background-color: #333; color: #777; }
        QLineEdit, QTextEdit {
            background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #2a2a2a; border-radius: 8px; padding: 8px;
        }
        QListWidget { background-color: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; }
        QListWidget::item { padding: 8px; }
        QTreeWidget { background-color: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; }
        QTreeWidget::header { background-color: #222; padding: 6px; }
        QComboBox, QSpinBox {
            background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #2a2a2a; border-radius: 6px; padding: 6px;
        }
        QGroupBox { border: 1px solid #2a2a2a; border-radius: 8px; margin-top: 10px; padding-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #aaa; }
        QTabWidget::pane { border: 1px solid #2a2a2a; border-radius: 8px; background: #151515; }
        QTabBar::tab {
            background: #1a1a1a; color: #aaa; padding: 8px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px;
        }
        QTabBar::tab:selected { background: #1f3a3a; color: #00d4aa; }
        QScrollArea { border: none; }
        """

    def _build_dashboard(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

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
        layout.addLayout(topbar)

        # 计划模式横幅
        self.plan_banner = QLabel("计划模式已开启 — XMclaw 将先思考再行动")
        self.plan_banner.setStyleSheet("background-color: #1f3a3a; color: #00d4aa; padding: 8px 14px; border-radius: 8px; font-size: 13px;")
        self.plan_banner.setVisible(False)
        layout.addWidget(self.plan_banner)

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

        # 右侧信息面板
        right = QWidget()
        right.setMaximumWidth(380)
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
        todo_btn = QPushButton("刷新待办")
        todo_btn.clicked.connect(self._load_todos)
        right_layout.addWidget(todo_btn)

        # 任务面板
        task_title = QLabel("活跃任务")
        task_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        right_layout.addWidget(task_title)
        self.task_list = QListWidget()
        right_layout.addWidget(self.task_list)
        task_btn = QPushButton("刷新任务")
        task_btn.clicked.connect(self._load_tasks)
        right_layout.addWidget(task_btn)

        right_layout.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([900, 380])
        layout.addWidget(splitter, 1)
        self.stack.addWidget(page)

    def _build_workspace(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        title = QLabel("工作区文件")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #00d4aa;")
        layout.addWidget(title)

        desc = QLabel("浏览和编辑 Agent 工作目录中的文件。")
        desc.setStyleSheet("color: #888;")
        layout.addWidget(desc)

        # 工具栏
        toolbar = QHBoxLayout()
        self.ws_path_label = QLabel(str(get_agent_dir(self.agent_id)))
        self.ws_path_label.setStyleSheet("color: #aaa; font-size: 12px;")
        toolbar.addWidget(self.ws_path_label)
        toolbar.addStretch()

        git_status_btn = QPushButton("Git 状态")
        git_status_btn.clicked.connect(lambda: self._run_git_command("status"))
        toolbar.addWidget(git_status_btn)

        git_pull_btn = QPushButton("Git Pull")
        git_pull_btn.clicked.connect(lambda: self._run_git_command("pull"))
        toolbar.addWidget(git_pull_btn)

        git_commit_btn = QPushButton("Git Commit")
        git_commit_btn.clicked.connect(self._git_commit_dialog)
        toolbar.addWidget(git_commit_btn)

        git_push_btn = QPushButton("Git Push")
        git_push_btn.clicked.connect(lambda: self._run_git_command("push"))
        toolbar.addWidget(git_push_btn)

        import_btn = QPushButton("导入文件")
        import_btn.clicked.connect(self._import_file)
        toolbar.addWidget(import_btn)
        layout.addLayout(toolbar)

        # 文件树 + 编辑器
        hsplit = QSplitter(Qt.Orientation.Horizontal)

        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels(["文件", "大小"])
        self.file_tree.setColumnWidth(0, 220)
        self.file_tree.itemClicked.connect(self._on_file_selected)
        hsplit.addWidget(self.file_tree)

        self.file_editor = QTextEdit()
        self.file_editor.setPlaceholderText("选择一个文件进行编辑...")
        hsplit.addWidget(self.file_editor)
        hsplit.setSizes([300, 900])
        layout.addWidget(hsplit, 1)

        save_btn = QPushButton("保存文件")
        save_btn.clicked.connect(self._save_file)
        layout.addWidget(save_btn)
        self.stack.addWidget(page)

    def _build_evolution(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        title = QLabel("进化状态")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #00d4aa;")
        layout.addWidget(title)

        desc = QLabel("查看 XMclaw 自主进化系统产生的 Gene 和 Skill。")
        desc.setStyleSheet("color: #888;")
        layout.addWidget(desc)

        tabs = QTabWidget()

        # Genes
        gene_tab = QWidget()
        gene_layout = QVBoxLayout(gene_tab)
        self.gene_list = QListWidget()
        gene_layout.addWidget(self.gene_list)
        gene_btn = QPushButton("刷新 Gene 列表")
        gene_btn.clicked.connect(self._load_evolution)
        gene_layout.addWidget(gene_btn)
        tabs.addTab(gene_tab, "Genes")

        # Skills
        skill_tab = QWidget()
        skill_layout = QVBoxLayout(skill_tab)
        self.skill_list = QListWidget()
        skill_layout.addWidget(self.skill_list)
        skill_btn = QPushButton("刷新 Skill 列表")
        skill_btn.clicked.connect(self._load_evolution)
        skill_layout.addWidget(skill_btn)
        tabs.addTab(skill_tab, "Skills")

        # Insights
        insight_tab = QWidget()
        insight_layout = QVBoxLayout(insight_tab)
        self.insight_list = QListWidget()
        insight_layout.addWidget(self.insight_list)
        insight_btn = QPushButton("刷新洞察")
        insight_btn.clicked.connect(self._load_evolution)
        insight_layout.addWidget(insight_btn)
        tabs.addTab(insight_tab, "Insights")

        layout.addWidget(tabs, 1)
        self.stack.addWidget(page)

    def _build_memory(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        title = QLabel("记忆搜索")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #00d4aa;")
        layout.addWidget(title)

        desc = QLabel("搜索 Agent 的长期记忆（MEMORY.md、会话日志等）。")
        desc.setStyleSheet("color: #888;")
        layout.addWidget(desc)

        search_row = QHBoxLayout()
        self.memory_search_box = QLineEdit()
        self.memory_search_box.setPlaceholderText("输入关键词搜索记忆...")
        search_row.addWidget(self.memory_search_box)
        search_btn = QPushButton("搜索")
        search_btn.clicked.connect(self._search_memory)
        search_row.addWidget(search_btn)
        layout.addLayout(search_row)

        self.memory_result_list = QListWidget()
        layout.addWidget(self.memory_result_list)
        self.stack.addWidget(page)

    def _build_tools(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        title = QLabel("工具日志")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #00d4aa;")
        layout.addWidget(title)

        desc = QLabel("查看 Agent 执行过的所有工具调用记录。")
        desc.setStyleSheet("color: #888;")
        layout.addWidget(desc)

        self.tool_log_list = QListWidget()
        layout.addWidget(self.tool_log_list)
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self.tool_log_list.clear)
        layout.addWidget(clear_btn)
        self.stack.addWidget(page)

    def _build_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        title = QLabel("设置")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #00d4aa;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(12)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["anthropic", "openai"])
        form.addRow("默认 LLM 提供商:", self.provider_combo)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("例如: minimax-portal/MiniMax-M2.7-highspeed")
        form.addRow("模型名称:", self.model_input)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Key:", self.api_key_input)

        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("https://api.minimaxi.com/anthropic")
        form.addRow("Base URL:", self.base_url_input)

        self.evo_check = QCheckBox("启用自主进化")
        self.evo_check.setChecked(True)
        form.addRow(self.evo_check)

        self.evo_interval = QSpinBox()
        self.evo_interval.setRange(5, 1440)
        self.evo_interval.setValue(30)
        self.evo_interval.setSuffix(" 分钟")
        form.addRow("进化间隔:", self.evo_interval)

        layout.addLayout(form)

        save_btn = QPushButton("保存设置")
        save_btn.clicked.connect(self._save_settings)
        layout.addWidget(save_btn)
        layout.addStretch()
        self.stack.addWidget(page)

        # Load current config
        self._load_settings()

    # ===== Actions =====

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
        idx = ["dashboard", "workspace", "evolution", "memory", "tools", "settings"].index(key)
        self.stack.setCurrentIndex(idx)
        if key == "workspace":
            self._load_workspace()
        elif key == "evolution":
            self._load_evolution()
        elif key == "memory":
            pass

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

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        # Use a simple fallback icon to avoid "No Icon set" warning
        from PySide6.QtGui import QIcon, QPixmap, QColor
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor("#00d4aa"))
        self.tray.setIcon(QIcon(pixmap))
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
        msg_type = msg.get("type", "")
        if msg_type == "reflection":
            data = msg.get("data", {})
            summary = data.get("summary", "Reflection")
            problems = data.get("problems", [])
            lessons = data.get("lessons", [])
            improvements = data.get("improvements", [])
            lines = [f"[Reflection] {summary}"]
            if problems:
                lines.append("问题: " + "; ".join(problems))
            if lessons:
                lines.append("教训: " + "; ".join(lessons))
            if improvements:
                lines.append("改进: " + "; ".join(improvements))
            self._add_bubble("\n".join(lines), False)
            return

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

    def _on_tool_called(self, info: dict):
        name = info.get("name", "")
        result = info.get("result", "")
        text = f"{name}: {str(result)[:120]}"
        self.tool_list.addItem(text)
        self.tool_log_list.addItem(text)
        self.active_tool_label.setText(name)
        if name in ("file_read", "file_write", "file_edit"):
            self.active_file_label.setText(str(result)[:80])

    def _on_connection_change(self, connected: bool):
        if connected:
            self.conn_status.setText("● 已连接")
            self.conn_status.setStyleSheet("color: #00d4aa; font-size: 12px;")
        else:
            self.conn_status.setText("● 未连接")
            self.conn_status.setStyleSheet("color: #e74c3c; font-size: 12px;")

    # ===== Data Loading =====

    def _refresh_all_data(self):
        self._load_todos()
        self._load_tasks()

    def _load_todos(self):
        path = get_agent_dir(self.agent_id) / "workspace" / "todos.json"
        self.todo_list.clear()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data:
                text = f"{'[x]' if item.get('done') else '[ ]'} {item.get('title', '')}"
                self.todo_list.addItem(text)
        except Exception:
            pass

    def _load_tasks(self):
        path = get_agent_dir(self.agent_id) / "workspace" / "tasks.json"
        self.task_list.clear()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data:
                text = f"[{item.get('status', 'pending')}] {item.get('title', '')}"
                self.task_list.addItem(text)
        except Exception:
            pass

    def _load_workspace(self):
        self.file_tree.clear()
        agent_dir = get_agent_dir(self.agent_id)
        if not agent_dir.exists():
            return
        nodes = {}
        for root, _, filenames in os.walk(agent_dir):
            for fname in filenames:
                fpath = Path(root) / fname
                rel = fpath.relative_to(agent_dir)
                parts = rel.parts
                parent = self.file_tree
                for i, part in enumerate(parts[:-1]):
                    key = str(Path(*parts[:i+1]))
                    if key not in nodes:
                        item = QTreeWidgetItem(parent, [part])
                        nodes[key] = item
                    parent = nodes[key]
                item = QTreeWidgetItem(parent, [str(rel), str(fpath.stat().st_size)])
                item.setData(0, Qt.ItemDataRole.UserRole, str(rel))
        self.file_tree.expandAll()

    def _on_file_selected(self, item, col):
        rel = item.data(0, Qt.ItemDataRole.UserRole)
        if not rel:
            return
        self._current_file = rel
        agent_dir = get_agent_dir(self.agent_id)
        target = agent_dir / rel
        try:
            text = target.read_text(encoding="utf-8")
            self.file_editor.setText(text)
        except Exception as e:
            self.file_editor.setText(f"[无法读取: {e}]")

    def _save_file(self):
        if not hasattr(self, "_current_file") or not self._current_file:
            QMessageBox.warning(self, "保存失败", "请先选择一个文件")
            return
        agent_dir = get_agent_dir(self.agent_id)
        target = agent_dir / self._current_file
        try:
            target.write_text(self.file_editor.toPlainText(), encoding="utf-8")
            QMessageBox.information(self, "保存成功", f"已保存: {self._current_file}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入文件")
        if not path:
            return
        agent_dir = get_agent_dir(self.agent_id)
        target = agent_dir / Path(path).name
        try:
            target.write_bytes(Path(path).read_bytes())
            self._load_workspace()
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))

    def _run_git_command(self, command: str):
        agent_dir = get_agent_dir(self.agent_id)
        try:
            result = subprocess.run(
                ["git", "-C", str(agent_dir), *command.split()],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            output = result.stdout + "\n" + result.stderr
            QMessageBox.information(self, f"Git {command}", output.strip() or "完成")
        except Exception as e:
            QMessageBox.warning(self, f"Git {command} 失败", str(e))

    def _git_commit_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Git Commit")
        dialog.setMinimumWidth(400)
        dialog.setStyleSheet(self._stylesheet())
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("提交信息:"))
        msg_input = QLineEdit()
        msg_input.setPlaceholderText("例如: 修复 bug、更新配置...")
        layout.addWidget(msg_input)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        layout.addWidget(btns)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        message = msg_input.text().strip()
        if not message:
            QMessageBox.warning(self, "提交失败", "提交信息不能为空")
            return
        agent_dir = get_agent_dir(self.agent_id)
        try:
            subprocess.run(["git", "-C", str(agent_dir), "add", "."], check=True, capture_output=True)
            result = subprocess.run(
                ["git", "-C", str(agent_dir), "commit", "-m", message],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            output = result.stdout + "\n" + result.stderr
            QMessageBox.information(self, "Git Commit", output.strip() or "提交成功")
        except Exception as e:
            QMessageBox.warning(self, "Git Commit 失败", str(e))

    def _load_evolution(self):
        self.gene_list.clear()
        self.skill_list.clear()
        self.insight_list.clear()

        genes_dir = BASE_DIR / "shared" / "genes"
        if genes_dir.exists():
            for f in sorted(genes_dir.glob("gene_*.py")):
                self.gene_list.addItem(f"{f.stem}  ({f.name})")

        skills_dir = BASE_DIR / "shared" / "skills"
        if skills_dir.exists():
            for f in sorted(skills_dir.glob("skill_*.py")):
                self.skill_list.addItem(f"{f.stem}  ({f.name})")

        # insights from SQLite via HTTP worker would be better; fallback to simple listing
        db_path = BASE_DIR / "shared" / "memory.db"
        if db_path.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                cursor = conn.execute("SELECT title, description, source, created_at FROM insights ORDER BY created_at DESC LIMIT 50")
                for row in cursor.fetchall():
                    self.insight_list.addItem(f"[{row[3]}] {row[0]} ({row[2]}): {row[1]}")
                conn.close()
            except Exception:
                pass

    def _search_memory(self):
        query = self.memory_search_box.text().strip()
        self.memory_result_list.clear()
        if not query:
            return
        agent_dir = get_agent_dir(self.agent_id)
        results = []
        for root, _, filenames in os.walk(agent_dir):
            for fname in filenames:
                if fname.endswith(".md") or fname.endswith(".jsonl"):
                    fpath = Path(root) / fname
                    try:
                        text = fpath.read_text(encoding="utf-8")
                        if query.lower() in text.lower():
                            lines = text.splitlines()
                            for i, line in enumerate(lines):
                                if query.lower() in line.lower():
                                    start = max(0, i - 1)
                                    end = min(len(lines), i + 2)
                                    snippet = "\n".join(lines[start:end])
                                    break
                            rel = fpath.relative_to(agent_dir).as_posix()
                            results.append(f"[{rel}]\n{snippet}")
                    except Exception:
                        pass
        for r in results[:20]:
            self.memory_result_list.addItem(r)

    def _load_settings(self):
        path = get_agent_dir(self.agent_id) / "agent.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            llm = data.get("llm", {})
            provider = llm.get("default_provider", "anthropic")
            self.provider_combo.setCurrentText(provider)
            cfg = llm.get(provider, {})
            self.model_input.setText(cfg.get("default_model", ""))
            self.api_key_input.setText(cfg.get("api_key", ""))
            self.base_url_input.setText(cfg.get("base_url", ""))
            evo = data.get("evolution", {})
            self.evo_check.setChecked(evo.get("enabled", True))
            self.evo_interval.setValue(evo.get("interval_minutes", 30))
        except Exception:
            pass

    def _save_settings(self):
        path = get_agent_dir(self.agent_id) / "agent.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        provider = self.provider_combo.currentText()
        data["llm"] = data.get("llm", {})
        data["llm"]["default_provider"] = provider
        data["llm"][provider] = {
            "default_model": self.model_input.text(),
            "api_key": self.api_key_input.text(),
            "base_url": self.base_url_input.text(),
        }
        data["evolution"] = {
            "enabled": self.evo_check.isChecked(),
            "interval_minutes": self.evo_interval.value(),
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        QMessageBox.information(self, "保存成功", "设置已保存，部分选项需要重启 Daemon 生效。")
