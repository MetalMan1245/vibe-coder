"""VibeCoder main window: chat UI with conversation management.

Phase 2 adds conversation persistence, switching, and deletion.
"""

from pathlib import Path
import re
import subprocess

import requests
import os, sys
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor, QFont, QMouseEvent, QTextOption
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QTextBrowser, QTextEdit,
    QVBoxLayout, QWidget, QSplitter, QMenu, QStyle, QInputDialog
)

from vibe_coder import __version__
from vibe_coder.config import load_config, save_config
from vibe_coder.memory import MemoryStore
from vibe_coder.ollama_client import OllamaClient
from vibe_coder.conversation_manager import ConversationManager

MAX_ATTACHMENT_CHARS = 200_000

if sys.platform.startswith('linux'):
    os.environ.setdefault('QT_QPA_PLATFORMTHEME', 'xdgdesktopportal')

class ChatWorker(QThread):
    """Streams a chat response off the GUI thread."""

    chunk_received = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, client: OllamaClient, model: str, messages: list):
        super().__init__()
        self._client = client
        self._model = model
        self._messages = messages

    def run(self):
        try:
            for delta in self._client.chat(self._model, self._messages):
                self.chunk_received.emit(delta)
            if not self._client.aborted:
                self.finished_ok.emit()
        except Exception as exc:
            if not self._client.aborted:
                self.failed.emit(str(exc))


class TitleWorker(QThread):
    """Generates a short conversation title in the background."""

    title_ready = pyqtSignal(str, str)  # conv_id, title
    failed = pyqtSignal(str)

    def __init__(self, client: OllamaClient, model: str, conv_id: str,
                 messages: list):
        super().__init__()
        self._client = client
        self._model = model
        self._conv_id = conv_id
        self._messages = messages

    def run(self):
        try:
            excerpt = "\n".join(
                f"{m['role']}: {m['content'][:400]}" for m in self._messages[:2]
            )
            prompt = (
                "Give this conversation a concise title.\n"
                "Rules:\n"
                "- Exactly 2 to 4 words\n"
                "- No more than 28 characters total\n"
                "- Lowercase except proper nouns\n"
                "- No quotes, no ending punctuation, no emoji\n"
                "Reply with only the title.\n\n"
                f"{excerpt}"
            )
            title = self._client.complete(
                self._model, [{"role": "user", "content": prompt}]
            )
            title = title.strip().strip('"').strip("'").splitlines()[0].strip()
            if not title:
                raise ValueError("empty title")
            if len(title) > 40:
                title = title[:40].rsplit(" ", 1)[0]
            self.title_ready.emit(self._conv_id, title)
        except Exception as exc:
            self.failed.emit(str(exc))

class ToolExecutionWorker(QThread):
    """Handles tool execution loop with tool support."""

    content_reset = pyqtSignal()          # new iteration: clear display buffer
    content_chunk = pyqtSignal(str)      # complete content for this iteration
    tool_executed = pyqtSignal(str)       # one-line tool log
    thinking = pyqtSignal(bool)           # request in flight indicator
    finished_all = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, client: OllamaClient, model: str, messages: list,
                 tools: list[dict], tool_executor_func):
        super().__init__()
        self._client = client
        self._model = model
        self._messages = messages
        self._tools = tools
        self._execute_tool = tool_executor_func
        self._aborted = False
        self._max_tool_iterations = 30

    def run(self):
        messages = list(self._messages)
        iteration = 0

        try:
            while iteration < self._max_tool_iterations:
                iteration += 1

                self.thinking.emit(True)
                content, tool_calls, message = self._client.complete_with_tools(
                    self._model, messages, self._tools
                )
                self.thinking.emit(False)

                # Append the FULL assistant message (tool_calls included)
                # so later iterations can see what was previously called.
                messages.append(message)

                if content:
                    self.content_reset.emit()
                    self.content_chunk.emit(content)

                if not tool_calls:
                    break

                for tc in tool_calls:
                    result = self._execute_tool(tc)
                    first_line = result.splitlines()[0] if result else ""
                    self.tool_executed.emit(
                        f"→ {tc['name']}: {first_line[:160]}"
                    )
                    messages.append({
                        "role": "tool",
                        "tool_name": tc["name"],
                        "content": result,
                    })

            self.finished_all.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

class ChatInput(QTextEdit):
    """Multi-line input. Enter sends, Shift+Enter inserts a newline."""

    send_requested = pyqtSignal()

    def keyPressEvent(self, event):
        enter_keys = (Qt.Key.Key_Return.value, Qt.Key.Key_Enter.value)
        shift = Qt.KeyboardModifier.ShiftModifier
        if event.key() in enter_keys and not (event.modifiers() & shift):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

class MemoryDialog(QDialog):
    """Manual memory editor."""

    def __init__(self, store: MemoryStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Manage memories")
        self.resize(520, 360)

        self.list_widget = QListWidget()
        for entry in store.entries:
            self.list_widget.addItem(f"[{entry['tag']}] {entry['text']}")

        self.new_text = QLineEdit()
        self.new_text.setPlaceholderText("New memory text...")
        self.new_tag = QLineEdit()
        self.new_tag.setPlaceholderText("Tag (optional)")
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove)

        input_row = QHBoxLayout()
        input_row.addWidget(self.new_text, stretch=3)
        input_row.addWidget(self.new_tag, stretch=1)
        input_row.addWidget(add_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        layout.addLayout(input_row)
        layout.addWidget(remove_btn)
        layout.addWidget(buttons)

    def _add(self):
        text = self.new_text.text().strip()
        if not text:
            return
        self.store.add(text, self.new_tag.text().strip() or "general")
        self.list_widget.addItem(
            f"[{self.store.entries[-1]['tag']}] {text}"
        )
        self.new_text.clear()
        self.new_tag.clear()

    def _remove(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        self.store.remove(row)
        self.list_widget.takeItem(row)

class SettingsDialog(QDialog):
    def __init__(self, config: dict, client: OllamaClient, conversation_mgr: ConversationManager, parent=None):
        super().__init__(parent)
        self.config = config
        self.conversation_mgr = conversation_mgr
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        self.url_edit = QLineEdit(config["server_url"])

        self.model_combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_combo, stretch=1)
        model_row.addWidget(self.refresh_btn)

        self.sys_edit = QTextEdit()
        self.sys_edit.setPlaceholderText("System message, e.g. 'My OS is CachyOS unless otherwise specified.'")
        self.sys_edit.setPlainText(config["system_message"])
        self.sys_edit.setMaximumHeight(100)

        self.mem_check = QCheckBox("Inject memories into the system message")
        self.mem_check.setChecked(config["memories_enabled"])
        mem_manage_btn = QPushButton("Manage memories...")
        mem_manage_btn.clicked.connect(
            lambda: MemoryDialog(MemoryStore(), self).exec()
        )
        mem_row = QHBoxLayout()
        mem_row.addWidget(self.mem_check, stretch=1)
        mem_row.addWidget(mem_manage_btn)

        storage_stats = self.conversation_mgr.calculate_storage_usage()
        self.storage_label = QLabel(f"Conversation storage: {storage_stats['formatted']} ({storage_stats['file_count']} conversations)")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form = QVBoxLayout(self)
        form.addWidget(QLabel("Ollama server URL:"))
        form.addWidget(self.url_edit)
        form.addWidget(QLabel("Model:"))
        form.addLayout(model_row)
        form.addWidget(QLabel("System message:"))
        form.addWidget(self.sys_edit)
        form.addLayout(mem_row)
        form.addWidget(self.storage_label)
        form.addWidget(buttons)

        self.refresh_models()

    def refresh_models(self):
        probe = OllamaClient(self.url_edit.text().strip())
        self.model_combo.clear()
        try:
            models = probe.list_models()
        except requests.RequestException:
            self.model_combo.addItem("(server unreachable)")
            return
        if not models:
            self.model_combo.addItem("(no models installed)")
            return
        for m in reversed(models):
            self.model_combo.addItem(m["name"])
        configured = self.config.get("model")
        if configured:
            index = self.model_combo.findText(configured)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)

    def values(self) -> dict:
        return {
            "server_url": self.url_edit.text().strip(),
            "model": self.model_combo.currentText(),
            "system_message": self.sys_edit.toPlainText(),
            "memories_enabled": self.mem_check.isChecked(),
        }

class ConversationListItem(QWidget):
    """Clickable list item with a delete button and copy/delete context menu."""

    clicked = pyqtSignal(str)
    deleted = pyqtSignal(str)
    rename_requested = pyqtSignal(str)

    def __init__(self, entry: dict, max_title_width: int = 170,
                 size_text: str = "", parent=None):
        super().__init__(parent)
        self.conv_id = entry["id"]
        self.entry = entry

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)
        layout.setSpacing(8)

        text_column = QVBoxLayout()
        text_column.setSpacing(0)

        self.title_label = QLabel()
        metrics = self.title_label.fontMetrics()
        self.title_label.setText(metrics.elidedText(
            entry["title"], Qt.TextElideMode.ElideRight, max_title_width
        ))
        self.title_label.setStyleSheet("font-weight: bold;")

        time_label = QLabel(entry["updated"][:16].replace("T", " "))
        time_label.setStyleSheet("font-size: 11px; color: gray;")

        meta_text = f"{entry['message_count']} msgs"
        if size_text:
            meta_text += f" · {size_text}"
        msg_count = QLabel(meta_text)
        msg_count.setStyleSheet("font-size: 10px; color: gray;")

        meta_row = QHBoxLayout()
        meta_row.addWidget(time_label)
        meta_row.addWidget(msg_count)
        meta_row.addStretch(1)

        text_column.addWidget(self.title_label)
        text_column.addLayout(meta_row)

        self.delete_btn = QPushButton()
        self.delete_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        )
        self.delete_btn.setFixedSize(28, 28)
        self.delete_btn.setToolTip("Delete conversation")
        self.delete_btn.setStyleSheet(
            "QPushButton { border: none; background-color: transparent; }"
            "QPushButton:hover { background-color: rgba(255, 0, 0, 0.15);"
            " border-radius: 4px; }"
        )
        self.delete_btn.clicked.connect(self._confirm_delete)

        layout.addLayout(text_column, stretch=1)
        layout.addWidget(self.delete_btn, 0, Qt.AlignmentFlag.AlignTop)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _confirm_delete(self):
        if QMessageBox.question(
            self,
            "Delete conversation?",
            f"Are you sure you want to delete \"{self.entry['title']}\"? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self.deleted.emit(self.conv_id)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.conv_id)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        rename_action = menu.addAction("Rename...")
        copy_action = menu.addAction("Copy")
        delete_action = menu.addAction("Delete")
        action = menu.exec(event.globalPos())
        if action == rename_action:
            self.rename_requested.emit(self.conv_id)
        elif action == copy_action:
            QApplication.clipboard().setText(self.entry["title"])
        elif action == delete_action:
            self._confirm_delete()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"VibeCoder v{__version__}")
        self.resize(1000, 720)

        self.config = load_config()
        self.memory_store = MemoryStore()
        self.client = OllamaClient(self.config["server_url"])
        self.conversation_mgr = ConversationManager()
        from vibe_coder.project_manager import ProjectManager
        self.project_mgr = ProjectManager()

        self.history: list[dict] = []
        self.transcript: list[tuple] = []
        self.pending_attachments: list[str] = []
        self.ai_buffer = ""
        self.worker: ChatWorker | None = None
        self.title_worker: TitleWorker | None = None
        self.model: str | None = None
        self.current_conv_id: str | None = None
        self.current_project_id: str | None = None
        self.files_cache: list[str] = []

        self.setAcceptDrops(True)

        self._build_ui()
        self._bootstrap()
        self._refresh_conversation_list()
        self._load_first_conversation_or_create()
        self._refresh_project_list()
        self._check_first_project_warning()

    def _build_ui(self):
        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Main split: sidebar (tabs) + content
        sidebar = QWidget()
        sidebar.setFixedWidth(280)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.setSpacing(6)

        # Tab buttons
        tab_buttons = QHBoxLayout()
        self.conversations_tab_btn = QPushButton("Conversations")
        self.projects_tab_btn = QPushButton("Projects")
        self.conversations_tab_btn.setCheckable(True)
        self.projects_tab_btn.setCheckable(True)
        self.conversations_tab_btn.setChecked(True)
        self.conversations_tab_btn.clicked.connect(self._switch_to_conversations)
        self.projects_tab_btn.clicked.connect(self._switch_to_projects)
        tab_buttons.addWidget(self.conversations_tab_btn)
        tab_buttons.addWidget(self.projects_tab_btn)
        sidebar_layout.addLayout(tab_buttons)

        sidebar_layout.addWidget(QLabel(""))  # spacer

        # Conversations section
        self.conversations_section = QWidget()
        conv_layout = QVBoxLayout(self.conversations_section)
        conv_layout.setContentsMargins(0, 0, 0, 0)
        conv_layout.setSpacing(8)

        conv_header = QHBoxLayout()
        conv_title = QLabel("Conversations")
        conv_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        new_conv_btn = QPushButton("+ New")
        new_conv_btn.setFixedHeight(30)
        new_conv_btn.clicked.connect(self._new_conversation)
        conv_header.addWidget(conv_title, stretch=1)
        conv_header.addWidget(new_conv_btn)
        conv_layout.addLayout(conv_header)

        self.conversation_list = QListWidget()
        conv_layout.addWidget(self.conversation_list, stretch=1)

        storage_stats = self.conversation_mgr.calculate_storage_usage()
        self.storage_footer = QLabel(f"Storage: {storage_stats['formatted']}")
        self.storage_footer.setStyleSheet("font-size: 10px; color: gray;")
        conv_layout.addWidget(self.storage_footer)

        sidebar_layout.addWidget(self.conversations_section, stretch=1)

        # Projects section (hidden initially)
        self.projects_section = QWidget()
        proj_layout = QVBoxLayout(self.projects_section)
        proj_layout.setContentsMargins(0, 0, 0, 0)
        proj_layout.setSpacing(8)

        proj_header = QHBoxLayout()
        proj_title = QLabel("Projects")
        proj_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        new_proj_btn = QPushButton("+ New")
        new_proj_btn.setFixedHeight(30)
        new_proj_btn.clicked.connect(self._new_project)
        proj_header.addWidget(proj_title, stretch=1)
        proj_header.addWidget(new_proj_btn)
        proj_layout.addLayout(proj_header)

        self.project_list = QListWidget()
        self.project_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(self._show_project_context_menu)
        self.project_list.itemSelectionChanged.connect(self._on_project_selected)
        proj_layout.addWidget(self.project_list, stretch=1)

        # Files in project (sub-list)
        self.files_list = QListWidget()
        self.files_list.itemDoubleClicked.connect(self._view_file)
        files_header = QLabel("Files")
        files_header.setStyleSheet("font-size: 12px; font-weight: bold; padding-top: 8px;")
        proj_layout.addWidget(files_header)
        proj_layout.addWidget(self.files_list, stretch=1)

        proj_footer = QHBoxLayout()
        self.git_status_label = QLabel("Git: -")
        self.git_status_label.setStyleSheet("font-size: 10px; color: gray;")
        self.refresh_git_btn = QPushButton("⟳")
        self.refresh_git_btn.setFixedSize(24, 24)
        self.refresh_git_btn.setToolTip("Refresh git status")
        self.refresh_git_btn.clicked.connect(self._refresh_git_status)
        proj_footer.addWidget(self.git_status_label)
        proj_footer.addWidget(self.refresh_git_btn)
        proj_layout.addLayout(proj_footer)

        sidebar_layout.addWidget(self.projects_section)
        self.projects_section.setVisible(False)  # Hidden until switched

        # Main chat area
        chat_panel = QWidget()
        chat_layout = QVBoxLayout(chat_panel)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        self.settings_btn = QPushButton("Settings ⚙")
        self.settings_btn.clicked.connect(self._open_settings)
        top = QHBoxLayout()
        top.addWidget(self.settings_btn)
        top.addStretch(1)
        chat_layout.addLayout(top)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.chat_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        chat_layout.addWidget(self.chat_view, stretch=1)

        # File viewer (shown when project active)
        self.file_viewer = QTextBrowser()
        self.file_viewer.setOpenExternalLinks(False)
        self.file_viewer.setPlaceholderText("Select a file from the sidebar to view")
        self.file_viewer.setVisible(False)
        chat_layout.addWidget(self.file_viewer, stretch=0)
        self.file_viewer.setFixedHeight(200)

        self.attach_label = QLabel("")
        self.attach_label.setStyleSheet("color: gray; font-size: 11px;")
        chat_layout.addWidget(self.attach_label)

        self.input = ChatInput()
        self.input.setPlaceholderText("Message the model... (Enter to send, Shift+Enter for newline)")
        self.input.setFixedHeight(88)
        self.input.send_requested.connect(self._on_send_clicked)

        self.attach_btn = QPushButton("Attach")
        self.attach_btn.clicked.connect(self._attach_file)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send_clicked)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        self.stop_btn.setEnabled(False)

        side = QVBoxLayout()
        side.addWidget(self.attach_btn)
        side.addWidget(self.send_btn)
        side.addWidget(self.stop_btn)
        side.addStretch(1)

        bottom = QHBoxLayout()
        bottom.addWidget(self.input, stretch=1)
        bottom.addLayout(side)
        chat_layout.addLayout(bottom)

        # Splitter between sidebar and chat
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(sidebar)
        splitter.addWidget(chat_panel)
        splitter.setStretchFactor(0, 0)  # Sidebar doesn't grow
        splitter.setStretchFactor(1, 1)  # Chat panel grows
        splitter.setHandleWidth(1)

        main_layout.addWidget(splitter, stretch=1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Starting...")

    def _bootstrap(self):
        try:
            installed = [m["name"] for m in self.client.list_models()]
        except requests.RequestException:
            self.statusBar().showMessage(
                f"Ollama unreachable at {self.config['server_url']} "
                "- configure it in Settings"
            )
            return

        configured = self.config.get("model")
        if configured and configured in installed:
            self.model = configured
        elif installed:
            self.model = self.client.pick_default_model()
            if self.model != configured:
                self.config["model"] = self.model
                save_config(self.config)
        else:
            self.statusBar().showMessage(
                "Ollama reachable but no models installed - pull one first"
            )
            return
        self.statusBar().showMessage(
            f"Connected to {self.client.server_url} | Model: {self.model}"
        )

    def _refresh_conversation_list(self):
        """Reload the conversation sidebar."""
        self.conversation_list.clear()
        conversations = self.conversation_mgr.list_conversations()
        if not conversations:
            item = QListWidgetItem("(No conversations yet)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.conversation_list.addItem(item)
            return

        title_width = max(self.conversation_list.width() - 70, 100)
        for entry in conversations:
            size_text = self.conversation_mgr.get_conversation_size(entry["id"])
            widget = ConversationListItem(
                entry, max_title_width=title_width, size_text=size_text
            )
            widget.clicked.connect(self._load_conversation)
            widget.deleted.connect(self._delete_conversation)
            widget.rename_requested.connect(self._rename_conversation)
            item = QListWidgetItem()
            item.setSizeHint(widget.sizeHint())
            self.conversation_list.addItem(item)
            self.conversation_list.setItemWidget(item, widget)

    def _switch_to_conversations(self):
        self.conversations_section.setVisible(True)
        self.projects_section.setVisible(False)
        self.conversations_tab_btn.setChecked(True)

    def _switch_to_projects(self):
        self.conversations_section.setVisible(False)
        self.projects_section.setVisible(True)
        self.projects_tab_btn.setChecked(True)
        self._refresh_project_list()

    def _load_first_conversation_or_create(self):
        """Load first existing conversation or create a new one on startup."""
        conversations = self.conversation_mgr.list_conversations()
        if conversations:
            self._load_conversation(conversations[0]["id"])
        else:
            self._new_conversation()

    def _new_conversation(self):
        """Start a fresh conversation."""
        if self.worker is not None:
            QMessageBox.information(
                self, "Busy", "Wait for the current response to finish."
            )
            return

        if self.history:
            self._auto_save_conversation()

        self.current_conv_id = self.conversation_mgr.create_conversation()["id"]
        self.history = []
        self.transcript = []
        self.ai_buffer = ""
        self.input.clear()
        self._render()
        self._refresh_conversation_list()

    def _load_conversation(self, conv_id: str):
        """Load a saved conversation."""
        if self.worker is not None:
            QMessageBox.information(
                self, "Busy", "Wait for the current response to finish."
            )
            return

        conv_data = self.conversation_mgr.load_conversation(conv_id)
        if conv_data is None:
            self.statusBar().showMessage(f"Could not load conversation {conv_id[:8]}...")
            return

        self.current_conv_id = conv_id
        self.history = conv_data.get("messages", [])
        self.transcript = [(m["role"], m["content"]) for m in self.history]
        self.ai_buffer = ""
        self._render()
        self.statusBar().showMessage(f"Loaded conversation: {conv_data.get('title', '')}")

    def _auto_save_conversation(self):
        """Persist current conversation to disk."""
        if not self.current_conv_id:
            return

        if not self.history:
            return  # Nothing to save

        self.conversation_mgr.save_conversation(self.current_conv_id, self.history)

        conv_data = self.conversation_mgr.load_conversation(self.current_conv_id)
        if conv_data and conv_data.get("title") == "New Conversation":
            self._request_ai_title(self.current_conv_id)

        self.statusBar().showMessage(f"Saved conversation {self.current_conv_id[:8]}...", 3000)

    def _on_project_selected(self):
        """Handle project list selection change."""
        selected = self.project_list.selectedItems()
        if not selected:
            # Deselect case - clear files list but keep project as "active"
            self.files_list.clear()
            self.files_cache = []
            self.file_viewer.setVisible(False)
            return

        proj_id = selected[0].data(Qt.ItemDataRole.UserRole)
        if proj_id:
            self._load_project_by_id(proj_id)
            self._populate_files_list(proj_id)  # Always populate when selected

    def _get_tool_definitions(self) -> list[dict]:
        """Return tool definitions for the AI if a project is active."""
        if not self.current_project_id:
            return []

        proj_data = self.project_mgr.load_project(self.current_project_id)
        if not proj_data:
            return []

        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read the content of a file within the project scope.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative path to the file within the project."
                            }
                        },
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Create or overwrite a file within the project scope. Automatically commits changes after writing.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative path to the file within the project."
                            },
                            "content": {
                                "type": "string",
                                "description": "The content to write to the file."
                            }
                        },
                        "required": ["path", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files in the project scope. Use recursive=false for root directory only.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "recursive": {
                                "type": "boolean",
                                "description": "Whether to list files recursively. Default true.",
                                "default": True
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Delete a file within the project scope.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative path to the file within the project."
                            }
                        },
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "git_commit",
                    "description": "Stage and commit all changes with the given message.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "Commit message describing the changes."
                            }
                        },
                        "required": ["message"]
                    }
                }
            },
        ]

    def _execute_tool_call(self, tool: dict) -> str:
        """Execute a tool call and return the result."""
        name = tool.get("name", "")
        args = tool.get("arguments", {})

        try:
            if name == "read_file":
                path = args.get("path", "")
                content = self.project_mgr.read_file(self.current_project_id, path)
                if content is None:
                    return f"[Error: Could not read file {path}]"
                if len(content) > 20000:
                    content = content[:20000] + "\n\n[...file truncated...]"
                return f"[File content of {path}]\n{content}"

            elif name == "write_file":
                path = args.get("path", "")
                content = args.get("content", "")
                success = self.project_mgr.write_file(self.current_project_id, path, content)
                if success:
                    commit_result = self.project_mgr.git_commit(
                        self.current_project_id, f"Update {path}"
                    )
                    return f"[Successfully wrote to {path}]\n[Git commit: {'success' if commit_result[0] else 'failed'}]"
                return f"[Error: Failed to write file {path}]"

            elif name == "list_files":
                recursive = args.get("recursive", True)
                files = self.project_mgr.list_files(self.current_project_id, recursive)
                if len(files) > 100:
                    files = files[:100] + [f"... ({len(files) - 100} more files)"]
                return "[Project files]\n" + "\n".join(files)

            elif name == "delete_file":
                path = args.get("path", "")
                success = self.project_mgr.delete_file(self.current_project_id, path)
                if success:
                    return f"[Successfully deleted {path}]"
                return f"[Error: Failed to delete {path}]"

            elif name == "git_commit":
                message = args.get("message", "Auto-committed")
                success, output = self.project_mgr.git_commit(self.current_project_id, message)
                if success:
                    return f"[Successfully committed: {message}]"
                return f"[Error: Commit failed - {output}]"

            else:
                return f"[Error: Unknown tool '{name}']"
        except Exception as exc:
            return f"[Error executing tool {name}: {exc}]"

    def _view_file(self, item):
        """View file contents in the file viewer panel."""
        rel_path = item.text()
        if not self.current_project_id:
            return

        content = self.project_mgr.read_file(self.current_project_id, rel_path)
        if content is None:
            self.file_viewer.setPlainText(f"[Could not read file: {rel_path}]")
            return

        # Detect language for syntax hinting
        ext = Path(rel_path).suffix.lower()
        lang_map = {
            ".py": "python", ".js": "javascript", ".html": "html",
            ".css": "css", ".json": "json", ".md": "markdown",
            ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
            ".txt": "", ".toml": "", ".ini": "",
        }
        lang = lang_map.get(ext, "")

        preview = f"**File:** `{rel_path}`\n\n"
        if lang:
            preview += f"\n```{lang}\n{content}\n```\n"
        else:
            preview += f"\n```\n{content}\n```\n"

        self.file_viewer.setHtml(CHAT_STYLESHEET + preview)
        self.file_viewer.setVisible(True)

    def _check_first_project_warning(self):
        """Warn user about AI file modifications on first project."""
        if self.project_mgr.first_project_warning_shown:
            return
        projects = self.project_mgr.list_projects()
        if not projects:
            # No projects yet, show warning on next project creation
            return

        QMessageBox.warning(
            self,
            "Project File Modifications",
            "AI will modify files directly in this project without asking.\n\n"
            "⚠️ IMPORTANT: Back up your project folder regularly!\n\n"
            "The AI can create, edit, and delete files freely. "
            "Always review changes before pushing to remote.",
            QMessageBox.StandardButton.Ok
        )
        self.project_mgr.first_project_warning_shown = True

    def _new_project(self):
        """Create a new project with user-specified scope."""
        if self.worker is not None:
            QMessageBox.information(
                self, "Busy", "Wait for the current response to finish."
            )
            return

        name, ok = QInputDialog.getText(self, "New Project", "Project name:")
        if not ok or not name.strip():
            return

        # Choose scope directory
        scope_path = Path(QFileDialog.getExistingDirectory(
            self, "Select Project Scope Directory"
        ))
        if not scope_path:
            return

        try:
            self.current_project_id = self.project_mgr.create_project(name.strip(), scope_path)
            QMessageBox.information(
                self, "Project Created",
                f"Project '{name}' created at {scope_path}\n\n"
                "The AI will only access files within this directory."
            )
            self._refresh_project_list()
            self._switch_to_projects()
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to create project: {exc}")

    def _refresh_project_list(self):
        """Reload the project list."""
        self.project_list.clear()
        self.files_list.clear()
        self.files_cache = []
        self.file_viewer.setVisible(False)

        projects = self.project_mgr.list_projects()
        if not projects:
            item = QListWidgetItem("(No projects yet)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.project_list.addItem(item)
            return

        for entry in projects:
            item = QListWidgetItem(entry["name"])
            item.setToolTip(f"Scope: {entry['scope_path']}\nCreated: {entry['created'][:10]}")
            item.setData(Qt.ItemDataRole.UserRole, entry["id"])
            self.project_list.addItem(item)

        # Select active project if any
        if self.current_project_id:
            for i in range(self.project_list.count()):
                if self.project_list.item(i).data(Qt.ItemDataRole.UserRole) == self.current_project_id:
                    self.project_list.setCurrentItem(self.project_list.item(i))
                    self._populate_files_list(self.current_project_id)
                    break

    def _populate_files_list(self, proj_id: str):
        """Populate the files list for a project."""
        self.files_cache = self.project_mgr.list_files(proj_id)
        self.files_list.clear()
        for rel_path in self.files_cache[:50]:  # Limit display
            item = QListWidgetItem(rel_path)
            item.setData(Qt.ItemDataRole.UserRole, rel_path)
            self.files_list.addItem(item)
        if len(self.files_cache) > 50:
            item = QListWidgetItem(f"... and {len(self.files_cache) - 50} more")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.files_list.addItem(item)

    def _refresh_git_status(self):
        """Refresh git status display."""
        if not self.current_project_id:
            self.git_status_label.setText("Git: No project")
            return

        status = self.project_mgr.git_status(self.current_project_id)
        if status.strip() == "":
            self.git_status_label.setText("Git: Clean")
        elif "nothing to commit" in status.lower():
            self.git_status_label.setText("Git: Clean")
        else:
            changes = len([l for l in status.split("\n") if l.strip()])
            self.git_status_label.setText(f"Git: {changes} change(s)")

    def _load_project_by_id(self, proj_id: str):
        """Load a project by ID."""
        proj_data = self.project_mgr.load_project(proj_id)
        if not proj_data:
            return

        self.current_project_id = proj_id
        self.files_cache = self.project_mgr.list_files(proj_id)
        self._refresh_git_status()
        self.statusBar().showMessage(f"Active project: {proj_data['name']}")

    def _delete_conversation(self, conv_id: str):
        """Delete a conversation after confirmation."""
        if self.conversation_mgr.delete_conversation(conv_id):
            if self.current_conv_id == conv_id:
                remaining = self.conversation_mgr.list_conversations()
                if remaining:
                    self._load_conversation(remaining[0]["id"])
                else:
                    self._new_conversation()
            self._refresh_conversation_list()
            storage_stats = self.conversation_mgr.calculate_storage_usage()
            self.storage_footer.setText(f"Storage: {storage_stats['formatted']}")

    def _request_ai_title(self, conv_id: str):
        """Ask the model for a short (~30 char) conversation title."""
        if self.title_worker is not None or self.model is None:
            return
        if not self.current_conv_id or len(self.history) < 2:
            return
        worker = TitleWorker(self.client, self.model, conv_id, self.history)
        worker.title_ready.connect(self._on_title_ready)
        worker.failed.connect(self._on_title_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self._on_title_worker_finished)
        self.title_worker = worker
        worker.start()

    def _on_title_ready(self, conv_id: str, title: str):
        conv = self.conversation_mgr.load_conversation(conv_id)
        if conv:
            self.conversation_mgr.save_conversation(
                conv_id, conv.get("messages", []), title
            )
            self._refresh_conversation_list()

    def _on_title_failed(self, message: str):
        self.statusBar().showMessage(
            f"Title generation failed: {message}", 5000
        )
        if self.current_conv_id:
            fallback = self.conversation_mgr.auto_title_conversation(
                self.current_conv_id, self.history
            )
            if fallback and fallback != "New Conversation":
                self._on_title_ready(self.current_conv_id, fallback)

    def _on_title_worker_finished(self):
        if self.sender() is self.title_worker:
            self.title_worker = None

    def _show_project_context_menu(self, pos):
        """Right-click menu for project list."""
        item = self.project_list.itemAt(pos)
        if not item:
            # Right-click on empty space - deselect project
            if QMessageBox.question(
                self, "Deselect Project?",
                "Clear the current project selection? You can continue chatting without a project.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            ) == QMessageBox.StandardButton.Yes:
                self.current_project_id = None
                self.files_cache = []
                self.files_list.clear()
                self.file_viewer.setVisible(False)
                self._refresh_git_status()
            return

        proj_id = item.data(Qt.ItemDataRole.UserRole)
        if not proj_id:
            return

        menu = QMenu(self)
        open_in_editor_action = menu.addAction("Open Scope in File Manager")
        menu.addSeparator()
        refresh_action = menu.addAction("Refresh Git Status")
        delete_action = menu.addAction("Delete Project")

        action = menu.exec(self.project_list.mapToGlobal(pos))
        if action == open_in_editor_action:
            scope = self.project_mgr.get_project_scope(proj_id)
            if scope:
                # Detach from parent process and don't wait for completion
                subprocess.Popen(
                    ["xdg-open", str(scope)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        elif action == refresh_action:
            self._refresh_git_status()
        elif action == delete_action:
            if QMessageBox.question(
                self, "Delete Project?",
                f"Delete project '{item.text()}\'s scope?\n\nThis removes project metadata only. Files remain.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            ) == QMessageBox.StandardButton.Yes:
                self.project_mgr.delete_project(proj_id)
                if self.current_project_id == proj_id:
                    self.current_project_id = None
                    self.files_cache = []
                    self._refresh_git_status()
                self._refresh_project_list()

    def _rename_conversation(self, conv_id: str):
        """Prompt to rename a conversation."""
        conv_data = self.conversation_mgr.load_conversation(conv_id)
        if not conv_data:
            return
        new_title, ok = QInputDialog.getText(
            self, "Rename Conversation", "New title:",
            text=conv_data.get("title", "")
        )
        if ok and new_title.strip():
            self.conversation_mgr.save_conversation(
                conv_id, conv_data.get("messages", []),
                new_title.strip()
            )
            self._refresh_conversation_list()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        added = False
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.pending_attachments.append(url.toLocalFile())
                added = True
        if added:
            names = ", ".join(Path(p).name for p in self.pending_attachments)
            self.attach_label.setText(f"Attached: {names}")
        event.acceptProposedAction()

    def _system_messages(self) -> list[dict]:
        parts = []
        if self.config["system_message"].strip():
            parts.append(self.config["system_message"].strip())

        # Inject project context if active (NO file list - tools handle that)
        if self.current_project_id:
            proj_data = self.project_mgr.load_project(self.current_project_id)
            if proj_data:
                scope = proj_data["scope_path"]
                project_ctx = (
                    f"You are working in a project named '{proj_data['name']}' at {scope}.\n\n"
                    f"You HAVE ACCESS TO THESE TOOLS that you MUST USE:\n"
                    f"- read_file(path): Read a file's content\n"
                    f"- write_file(path, content): Create or overwrite a file (auto-commits)\n"
                    f"- list_files(recursive=true): List files in the project\n"
                    f"- delete_file(path): Delete a file\n"
                    f"- git_commit(message): Stage and commit changes\n\n"
                    f"When you need to make file changes:\n"
                    f"1. CALL write_file with the path and content\n"
                    f"2. The file will be written automatically\n"
                    f"3. It will be auto-committed with a brief summary\n\n"
                    f"Do NOT write code blocks or bash commands - USE THE TOOLS DIRECTLY."
                )
                parts.append(project_ctx)

        if self.config["memories_enabled"]:
            block = self.memory_store.render(
                self.config["memory_max_chars"]
            )
            if block:
                parts.append("Persistent user memories:\n" + block)

        if not parts:
            return []
        return [{"role": "system", "content": "\n\n".join(parts)}]

    def _on_send_clicked(self):
        self._send_with_tools()

    def _on_stop_clicked(self):
        if self.worker is not None:
            self.client.abort()

    def _on_chunk(self, delta: str):
        self.ai_buffer += delta
        self.transcript[-1] = ("assistant", self.ai_buffer)
        self._render()

    def _on_done(self):
        self._finalize_assistant()
        self._render()
        self._set_busy(False)

    def _on_error(self, message: str):
        self._finalize_assistant()
        self.statusBar().showMessage(f"Error: {message}")
        self._render()
        self._set_busy(False)

    def _on_worker_finished(self):
        if self.ai_buffer:
            self._finalize_assistant()
            self._render()
        self._set_busy(False)
        if self.sender() is self.worker:
            self.worker = None

    def _finalize_assistant(self):
        if self.ai_buffer:
            self.history.append(
                {"role": "assistant", "content": self.ai_buffer}
            )
        elif self.transcript and self.transcript[-1][0] == "assistant":
            if not self.transcript[-1][1]:
                self.transcript.pop()
        self.ai_buffer = ""

        self._auto_save_conversation()

    def _send_with_tools(self):
        """Send message with tool support if project active."""
        if self.model is None:
            QMessageBox.warning(self, "No model", "No model available.")
            return

        text = self.input.toPlainText().strip()
        if not text and not self.pending_attachments:
            return

        if not self.current_conv_id:
            self.current_conv_id = self.conversation_mgr.create_conversation()["id"]
            self._refresh_conversation_list()

        user_content = text
        attachment_ctx = self._attachment_context(self.pending_attachments)
        if attachment_ctx:
            user_content = f"{text}\n\n{attachment_ctx}".strip()

        self.history.append({"role": "user", "content": user_content})
        self.transcript.append(("user", text))
        self.transcript.append(("note", f"sending with {'tool support' if self.current_project_id else 'no tools'}"))

        self.pending_attachments.clear()
        self.attach_label.setText("")
        self.input.clear()
        self.ai_buffer = ""
        self.transcript.append(("assistant", ""))

        self._set_busy(True)
        self._render()

        # Prepare system messages
        sys_messages = self._system_messages()
        all_messages = sys_messages + self.history

        # Use tool worker if project active, else regular worker
        if self.current_project_id:
            tools = self._get_tool_definitions()
            self.worker = ToolExecutionWorker(
                self.client, self.model, all_messages, tools,
                self._execute_tool_call
            )
            self.worker.content_reset.connect(self._on_tool_content_reset)
            self.worker.content_chunk.connect(self._on_tool_content)
            self.worker.tool_executed.connect(self._on_tool_log)
            self.worker.thinking.connect(self._on_tool_thinking)
            self.worker.finished_all.connect(self._on_tools_done)
            self.worker.failed.connect(self._on_tool_error)
            self.worker.finished.connect(self.worker.deleteLater)
        else:
            self.worker = ChatWorker(
                self.client, self.model, all_messages
            )
            self.worker.chunk_received.connect(self._on_chunk)
            self.worker.finished_ok.connect(self._on_done)
            self.worker.failed.connect(self._on_error)
            self.worker.finished.connect(self.worker.deleteLater)

        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _on_tool_content_reset(self):
        # New tool-loop iteration: start the assistant bubble fresh so
        # per-iteration preambles don't accumulate into one message.
        self.ai_buffer = ""
        self.transcript[-1] = ("assistant", "")

    def _on_tool_thinking(self, active: bool):
        if active:
            self.statusBar().showMessage("Thinking...")
        else:
            self.statusBar().showMessage("Generating response...")

    def _on_tool_content(self, content: str):
        if content:
            self.ai_buffer = content
            self.transcript[-1] = ("assistant", self.ai_buffer)
            self._render()

    def _on_tool_log(self, message: str):
        self.transcript.append(("tool", message))
        # Live-update the files sidebar and git status after mutations
        if message.startswith("→ write_file") or message.startswith("→ delete_file"):
            if self.current_project_id:
                self._populate_files_list(self.current_project_id)
                self._refresh_git_status()
        self._render()

    def _on_tools_done(self):
        self._finalize_assistant()
        self._render()
        self._set_busy(False)
        self.statusBar().showMessage("Response complete", 3000)

    def _on_tool_error(self, message: str):
        self._finalize_assistant()
        self.statusBar().showMessage(f"Tool error: {message}", 5000)
        self._render()
        self._set_busy(False)

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    def _attach_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Attach file")
        if path:
            self.pending_attachments.append(path)
            names = ", ".join(Path(p).name for p in self.pending_attachments)
            self.attach_label.setText(f"Attached: {names}")

    @staticmethod
    def _attachment_context(paths: list[str]) -> str:
        sections = []
        for p in paths:
            try:
                content = Path(p).read_text(
                    encoding="utf-8", errors="replace"
                )[:MAX_ATTACHMENT_CHARS]
            except OSError as exc:
                sections.append(f"(could not read {p}: {exc})")
                continue
            lang = Path(p).suffix.lstrip(".")
            sections.append(
                f"Attached file: `{Path(p).name}`\n\n"
                f"```{lang}\n{content}\n```\n"
            )
        return "\n\n".join(sections)

    def _render(self):
        parts = []
        for role, text in self.transcript:
            label = {"user": "You", "assistant": "Assistant"}.get(role, role)
            if role in ("note", "tool"):
                parts.append(f"*{text}*")
            else:
                parts.append(f"**{label}**\n\n{text}")
        scrollbar = self.chat_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        old_position = scrollbar.value()
        self.chat_view.setMarkdown("\n\n---\n\n".join(parts))
        scrollbar = self.chat_view.verticalScrollBar()
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(min(old_position, scrollbar.maximum()))

    def _open_settings(self):
        if self.worker is not None:
            QMessageBox.information(
                self, "Busy", "Wait for the current response to finish."
            )
            return
        dialog = SettingsDialog(self.config, self.client, self.conversation_mgr, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        url_changed = values["server_url"] != self.config["server_url"]
        self.config.update(values)
        save_config(self.config)
        if url_changed:
            self.client = OllamaClient(self.config["server_url"])
        self._bootstrap()
