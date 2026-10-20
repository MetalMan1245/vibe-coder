"""VibeCoder main window: chat UI with conversation management.

Phase 2 adds conversation persistence, switching, and deletion.
"""

from pathlib import Path
import re
import subprocess

import requests
import os, sys
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QTextCursor, QFont, QMouseEvent, QTextOption, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSizePolicy,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget, QSplitter, QMenu,
    QStyle, QInputDialog, QScrollArea
)

from vibe_coder import __version__
from vibe_coder.config import load_config, save_config, DEFAULTS
from vibe_coder.memory import MemoryStore
from vibe_coder.ollama_client import OllamaClient
from vibe_coder.conversation_manager import ConversationManager, CONVERSATIONS_DIR
from vibe_coder.vibeignore import VIBEIGNORE_FILENAME, TEMPLATE_NAMES, pretty_name

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
    finished_all = pyqtSignal(list)      # pass back full messages list
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
        self._max_tool_iterations = 15  # Reduced from 30 to prevent hangs

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
                    # Validate tool call structure before executing
                    if not tc.get("name"):
                        self.tool_executed.emit("→ Error: Tool call missing name, skipping")
                        continue
                    if not isinstance(tc.get("arguments"), dict):
                        self.tool_executed.emit(f"→ Error: Invalid arguments for {tc['name']}, skipping")
                        continue

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

            # Emit the COMPLETE messages list so caller can persist it
            self.finished_all.emit(messages)
        except Exception as exc:
            self.failed.emit(str(exc))

class ModelStatusIndicator(QLabel):
    """Visual indicator showing model state with spinner animation."""

    # Spinner frames (braille dots animation, similar to Ollama terminal)
    SPINNERS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    # Status colors
    COLOR_IDLE = "#888888"       # Gray - inactive
    COLOR_THINKING = "#6d4aff"   # Purple - working
    COLOR_STOPPED = "#ff6b6b"    # Red - stopped
    COLOR_ERROR = "#ffaa00"      # Orange - error/retrying

    def __init__(self, parent=None):
        super().__init__("● Idle", parent)
        self.setStyleSheet(f"color: {self.COLOR_IDLE}; font-family: monospace; font-size: 13px;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance_spinner)
        self._frame = 0

    def _advance_spinner(self):
        """Advance spinner animation frame."""
        self._frame = (self._frame + 1) % len(self.SPINNERS)
        if self.status == "thinking":
            spinner = self.SPINNERS[self._frame]
            self.setText(f"{spinner} Thinking...")

    def set_status(self, status: str, message: str = None):
        """Set the status indicator state.

        status: "idle", "thinking", "stopped", "error"
        message: optional custom message after spinner
        """
        self._timer.stop()
        self._frame = 0

        if status == "idle":
            self.setText("● Idle")
            self.setStyleSheet(f"color: {self.COLOR_IDLE}; font-family: monospace; font-size: 13px;")
            self.status = "idle"

        elif status == "thinking":
            self.setText("⠋ Thinking...")
            self.setStyleSheet(f"color: {self.COLOR_THINKING}; font-family: monospace; font-size: 13px;")
            self.status = "thinking"
            self._timer.start(100)  # 10fps for smooth animation

        elif status == "stopped":
            self.setText("■ Stopped")
            self.setStyleSheet(f"color: {self.COLOR_STOPPED}; font-family: monospace; font-size: 13px;")
            self.status = "stopped"

        elif status == "error":
            msg = message or "Error"
            self.setText(f"✗ {msg}")
            self.setStyleSheet(f"color: {self.COLOR_ERROR}; font-family: monospace; font-size: 13px;")
            self.status = "error"

    def update_thinking_message(self, message: str):
        """Update the thinking message while keeping spinner running."""
        if self.status == "thinking":
            self._timer.stop()
            self._frame = (self._frame + 1) % len(self.SPINNERS)
            spinner = self.SPINNERS[self._frame]
            self.setText(f"{spinner} {message}...")
            self._timer.start(100)


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
        self.setMinimumWidth(520)

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

        # Advanced section toggle
        self.advanced_check = QCheckBox("Show advanced options")
        self.advanced_check.stateChanged.connect(self._toggle_advanced)

        # AI Tools Template (advanced, hidden by default)
        self.tools_edit = QTextEdit()
        self.tools_edit.setPlaceholderText("Tools template for projects - use {project_name} and {scope_path} placeholders")
        self.tools_edit.setPlainText(config.get("tools_template", DEFAULTS["tools_template"]))
        self.tools_edit.setMaximumHeight(150)
        self.tools_edit.setVisible(False)

        self.tools_warning_label = QLabel("⚠️ WARNING: Changing this template can break tool behavior. "
                                          "Only modify if you understand how the AI interprets tool instructions.")
        self.tools_warning_label.setStyleSheet("color: #cc6600; font-size: 11px; padding: 6px;")
        self.tools_warning_label.setWordWrap(True)
        self.tools_warning_label.setVisible(False)

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
        form.addWidget(self.advanced_check)
        self.tools_label = QLabel("AI Tools Template (for projects):")
        self.tools_label.setVisible(False)
        form.addWidget(self.tools_label)
        form.addWidget(self.tools_edit)
        form.addWidget(self.tools_warning_label)
        form.addLayout(mem_row)
        form.addWidget(self.storage_label)
        form.addWidget(buttons)

        self.refresh_models()

    def _toggle_advanced(self):
        """Toggle visibility of advanced options."""
        show = self.advanced_check.isChecked()
        self.tools_label.setVisible(show)
        self.tools_edit.setVisible(show)
        self.tools_warning_label.setVisible(show)

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
            "tools_template": self.tools_edit.toPlainText(),
            "memories_enabled": self.mem_check.isChecked(),
        }

class VibeIgnoreDialog(QDialog):
    """Edit the project's .vibeignore — the context-only ignore file.

    Separate from .gitignore on purpose: rules here only hide files from
    the AI (listings/reads) to save context; git is never affected, so
    internal docs can stay visible to vibe-coder while excluded from
    publishing, and vice versa.
    """

    def __init__(self, project_mgr, proj_id: str, parent=None):
        super().__init__(parent)
        self.project_mgr = project_mgr
        self.proj_id = proj_id
        self.setWindowTitle(f"Edit {VIBEIGNORE_FILENAME} — context filter")
        self.setMinimumSize(560, 480)

        intro = QLabel(
            f"<b>{VIBEIGNORE_FILENAME}</b> is read <u>only by vibe-coder</u> "
            "(never by git). Files matching these patterns are hidden from the "
            "model's <code>list_files</code>/<code>read_file</code> tools to "
            "save context. Syntax = standard gitignore patterns "
            "(comments with <code>#</code>, negation with <code>!</code>, "
            "<code>**</code> globs, trailing <code>/</code> for directories)."
        )
        intro.setWordWrap(True)

        self.editor = QTextEdit()
        self.editor.setPlaceholderText("# one pattern per line, e.g.\ntarget/\n__pycache__/\nnotes/private/**")
        current = self.project_mgr.read_vibeignore(proj_id)
        if current is None:
            from vibe_coder.vibeignore import default_content
            current = default_content([])
            self.editor.setToolTip("(file not on disk yet — pressing Save will create it)")
        self.editor.setPlainText(current)
        self.editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        self.templates_btn = QPushButton("Add language templates…")
        self.templates_btn.clicked.connect(self._add_templates)
        self.restore_btn = QPushButton("Restore defaults")
        self.restore_btn.clicked.connect(self._restore_defaults)
        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.templates_btn)
        buttons_row.addWidget(self.restore_btn)
        buttons_row.addStretch(1)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.editor, stretch=1)
        layout.addLayout(buttons_row)
        layout.addWidget(box)

    def _add_templates(self):
        keys = TemplatePickerDialog(self).exec()
        if keys == QDialog.DialogCode.Rejected:
            return
        added_keys = TemplatePickerDialog.selected_keys
        text = self.editor.toPlainText().rstrip("\n")
        from vibe_coder.vibeignore import TEMPLATES
        existing = {l.strip() for l in text.splitlines() if l.strip()}
        for key in added_keys:
            block = TEMPLATES.get(key, [])
            missing = [l for l in block[1:] if l.strip() and l.strip() not in existing]
            if not missing:
                continue
            text += "\n\n" + "\n".join(block)
            existing.update(l.strip() for l in block if l.strip())
        self.editor.setPlainText(text + "\n")

    def _restore_defaults(self):
        if QMessageBox.question(
            self, "Restore defaults?",
            "Replace the editor contents with the generated starter rules?\n"
            "(Nothing is written until you press Save.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        from vibe_coder.vibeignore import default_content
        self.editor.setPlainText(default_content([]))

    def _save(self):
        content = self.editor.toPlainText()
        if content and not content.endswith("\n"):
            content += "\n"
        if not self.project_mgr.write_vibeignore(self.proj_id, content):
            QMessageBox.critical(self, "Error",
                                 f"Could not write {VIBEIGNORE_FILENAME}.")
            return
        count, warnings = self.project_mgr.reload_vibeignore(self.proj_id)
        if warnings:
            QMessageBox.warning(
                self, "Some patterns skipped",
                "Saved with warnings:\n\n" + "\n".join(warnings[:10])
            )
        else:
            QMessageBox.information(
                self, "Saved",
                f"{VIBEIGNORE_FILENAME} updated: {count} active rule(s).\n"
                "The AI now sees a filtered file listing."
            )
        self.accept()


class TemplatePickerDialog(QDialog):
    """GitHub-Desktop style template picker, but for .vibeignore rules.

    exec() returns Accepted/Rejected; chosen keys are exposed via the
    class-level ``selected_keys`` attribute after acceptance.
    """

    selected_keys: list[str] = []

    def __init__(self, parent=None, title: str = "Add vibeignore templates",
                 message: str = ("Choose languages to add their build/cache junk "
                                 "to the context filter:")):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(320)
        TemplatePickerDialog.selected_keys = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(message))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        grid_layout = QVBoxLayout(container)
        grid_layout.setSpacing(4)
        self._checks: dict[str, QCheckBox] = {}
        for key in TEMPLATE_NAMES:
            cb = QCheckBox(pretty_name(key))
            grid_layout.addWidget(cb)
            self._checks[key] = cb
        grid_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)

        select_all_row = QHBoxLayout()
        select_all_btn = QPushButton("Select all")
        select_all_btn.clicked.connect(lambda: [c.setChecked(True) for c in self._checks.values()])
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: [c.setChecked(False) for c in self._checks.values()])
        select_all_row.addWidget(select_all_btn)
        select_all_row.addWidget(clear_btn)
        select_all_row.addStretch(1)
        layout.addLayout(select_all_row)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _on_accept(self):
        TemplatePickerDialog.selected_keys = [
            k for k, cb in self._checks.items() if cb.isChecked()
        ]
        self.accept()


class ConversationListItem(QWidget):
    """Clickable list item with a delete button and copy/delete context menu."""

    clicked = pyqtSignal(str)
    deleted = pyqtSignal(str)
    rename_requested = pyqtSignal(str)
    reveal_requested = pyqtSignal(str)

    def __init__(self, entry: dict, max_title_width: int = 170,
                 size_text: str = "", parent=None):
        super().__init__(parent)
        self.conv_id = entry["id"]
        self.entry = entry

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)
        layout.setSpacing(4)

        text_column = QVBoxLayout()
        text_column.setSpacing(2)

        # Title label: Ignored horizontal policy means the label never
        # inflates the widget's size hint, so long titles cannot push
        # the item wider than the list (which clipped the delete button).
        # Actual elision is computed in resizeEvent once the real
        # width is known, not here at construction time.
        self.title_label = QLabel(entry["title"])
        self.title_label.setStyleSheet("font-weight: bold;")
        self.title_label.setWordWrap(False)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setToolTip(entry["title"])

        # Metadata row (time + message count)
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
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(4)

        text_column.addWidget(self.title_label)
        text_column.addLayout(meta_row)

        # Delete button
        self.delete_btn = QPushButton()
        self.delete_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        )
        self.delete_btn.setFixedSize(28, 28)
        self.delete_btn.setToolTip("Delete conversation")
        self.delete_btn.setStyleSheet(
            "QPushButton { border: none; background-color: transparent; padding: 0; margin: 0; }"
            "QPushButton:hover { background-color: rgba(255, 0, 0, 0.15);"
            " border-radius: 4px; }"
        )
        self.delete_btn.clicked.connect(self._confirm_delete)

        # Add text column and delete button to main layout
        layout.addLayout(text_column, stretch=1)
        layout.addWidget(self.delete_btn)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _elide_title(self):
        """Recompute the elided title for the current real widget width."""
        # Left/right margins (8 + 4), spacing (4), delete button (28)
        available = self.width() - 44
        if available < 30:
            available = 30
        self.title_label.setText(self.title_label.fontMetrics().elidedText(
            self.entry["title"], Qt.TextElideMode.ElideRight, available
        ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_title()

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
        reveal_action = menu.addAction("Show Conversation File")
        delete_action = menu.addAction("Delete")
        action = menu.exec(event.globalPos())
        if action == rename_action:
            self.rename_requested.emit(self.conv_id)
        elif action == copy_action:
            QApplication.clipboard().setText(self.entry["title"])
        elif action == reveal_action:
            self.reveal_requested.emit(self.conv_id)
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
        self.spinner_frame = 0
        self.spinner_timer: QTimer | None = None

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

        # Model status indicator
        self.model_indicator = ModelStatusIndicator()
        top.addWidget(self.model_indicator)

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

    def showEvent(self, event):
        super().showEvent(event)
        # Widget geometry is only reliable after the window is realized;
        # rebuild the list so item widths and elision use real sizes
        # instead of the pre-show defaults.
        self._refresh_conversation_list()

    def _refresh_conversation_list(self):
        """Reload the conversation sidebar."""
        self.conversation_list.clear()
        conversations = self.conversation_mgr.list_conversations()
        if not conversations:
            item = QListWidgetItem("(No conversations yet)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.conversation_list.addItem(item)
            return

        for entry in conversations:
            size_text = self.conversation_mgr.get_conversation_size(entry["id"])
            widget = ConversationListItem(
                entry, size_text=size_text
            )
            widget.clicked.connect(self._load_conversation)
            widget.deleted.connect(self._delete_conversation)
            widget.rename_requested.connect(self._rename_conversation)
            widget.reveal_requested.connect(self._reveal_conversation_file)
            item = QListWidgetItem()
            # Pin the item width to the viewport so the widget can never
            # be wider than the visible list (which hid the delete button
            # behind horizontal scrolling).
            viewport_width = self.conversation_list.viewport().width()
            item.setSizeHint(QSize(max(viewport_width, 120), widget.sizeHint().height()))
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
        self.transcript = [
            (m["role"], m["content"])
            for m in self.history
            if m.get("content", "").strip()
        ]
        self.ai_buffer = ""
        self._render()
        self.statusBar().showMessage(f"Loaded conversation: {conv_data.get('title', '')}")

    def _auto_save_conversation(self):
        """Persist current conversation to disk - DEPRECATED, use _save_current_history."""
        self._save_current_history()
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
            loaded = self._load_project_by_id(proj_id)
            if not loaded:
                # Scope directory missing - clear selection and warn
                QMessageBox.warning(
                    self, "Project Scope Missing",
                    f"The scope directory for this project no longer exists.\n\n"
                    f"This project entry will be removed from the list.",
                    QMessageBox.StandardButton.Ok
                )
                self.project_mgr.delete_project(proj_id)
                self.current_project_id = None
                self.files_cache = []
                self.files_list.clear()
                self.file_viewer.setVisible(False)
                self._refresh_project_list()
                return
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

        # Validate tool structure before executing
        if not name:
            return "[Error: Tool call missing name]"
        if not isinstance(args, dict):
            return f"[Error: Invalid arguments for {name}] - got {type(args).__name__}"

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

        # GitHub-Desktop style: optionally generate starter .vibeignore
        # templates (context-only ignore rules — never touches git).
        picker = TemplatePickerDialog(
            self,
            title=f"Generate {VIBEIGNORE_FILENAME} templates",
            message=("Pick languages used in this project. Their build junk "
                     "(target/, __pycache__/, node_modules/...) will be kept "
                     f"out of the AI's context via {VIBEIGNORE_FILENAME}.\n"
                     "Core safe defaults are always included."),
        )
        if picker.exec() == QDialog.DialogCode.Accepted:
            chosen_keys = TemplatePickerDialog.selected_keys
        else:
            chosen_keys = []

        try:
            proj_data = self.project_mgr.create_project(
                name.strip(), scope_path, vibeignore_templates=chosen_keys)
            self.current_project_id = proj_data["id"]
            tmpl_line = (" Templates added: " + ", ".join(pretty_name(k) for k in chosen_keys)
                         if chosen_keys else "")
            QMessageBox.information(
                self, "Project Created",
                f"Project '{name}' created at {scope_path}\n\n"
                "The AI will only access files within this directory.\n\n"
                f"A starter {VIBEIGNORE_FILENAME} was generated to save "
                f"context.{tmpl_line}\n"
                "You can edit it anytime from the project's right-click menu."
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
        """Populate the files list for a project.

        The GUI shows ALL files (force=True) so build junk stays visible to
        the human; the model's tools see only the .vibeignore-filtered set.
        Hidden-from-AI entries are marked with an eye-slash prefix.
        """
        self.files_cache = self.project_mgr.list_files(proj_id, force=True)
        matcher = self.project_mgr.vibeignore_matcher(proj_id)
        self.files_list.clear()
        for rel_path in self.files_cache[:50]:  # Limit display
            hidden = bool(matcher and matcher.is_ignored(rel_path))
            item = QListWidgetItem(("🚫 " if hidden else "") + rel_path)
            item.setData(Qt.ItemDataRole.UserRole, rel_path)
            if hidden:
                item.setToolTip(f"{rel_path} — hidden from the AI by {VIBEIGNORE_FILENAME} (double-click to view anyway)")
                item.setForeground(Qt.GlobalColor.gray)
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

    def _load_project_by_id(self, proj_id: str) -> bool:
        """Load a project by ID. Returns True if successful."""
        proj_data = self.project_mgr.load_project(proj_id)
        if not proj_data:
            return False

        # Verify scope directory still exists
        scope = self.project_mgr.get_project_scope(proj_id)
        if not scope:
            return False

        self.current_project_id = proj_id
        self.files_cache = self.project_mgr.list_files(proj_id, force=True)
        self._refresh_git_status()
        self.statusBar().showMessage(f"Active project: {proj_data['name']}")
        return True

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

    def _reveal_conversation_file(self, conv_id: str):
        """Open the file manager with the conversation file selected."""
        path = CONVERSATIONS_DIR / f"{conv_id}.json"
        if not path.exists():
            QMessageBox.warning(
                self, "Missing File",
                f"The conversation file for this entry no longer exists:\n{path}"
            )
            return

        self.statusBar().showMessage(str(path), 10000)

        # FileManager1 is the freedesktop-standard way to ask the file
        # manager to open the parent with the file pre-selected
        # (works in Dolphin, Nautilus, Thunar, etc.).
        result = subprocess.run(
            ["dbus-send", "--session", "--print-reply",
             "--dest=org.freedesktop.FileManager1",
             "/org/freedesktop/FileManager1",
             "org.freedesktop.FileManager1.ShowItems",
             f"array:string:{path.as_uri()}", "string:"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            # Fallback: just open the containing directory
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(CONVERSATIONS_DIR)))

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
                template = self.config.get("tools_template", DEFAULTS["tools_template"])
                project_ctx = template.format(
                    project_name=proj_data["name"],
                    scope_path=scope
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
        self.model_indicator.update_thinking_message("Generating")

    def _on_done(self):
        self._finalize_assistant()
        self._render()
        self._set_busy(False)
        self.model_indicator.set_status("idle")

    def _on_error(self, message: str):
        self._finalize_assistant()
        self.statusBar().showMessage(f"Error: {message}")
        self._render()
        self._set_busy(False)
        self.model_indicator.set_status("error", message[:30])

    def _on_worker_finished(self):
        if self.ai_buffer:
            self._finalize_assistant()
            self._render()
        self._set_busy(False)
        if self.sender() is self.worker:
            self.worker = None

    def _finalize_assistant(self):
        if self.ai_buffer:
            # Find the last assistant message in history and update it
            # (since worker may have already added it with tool_calls)
            last_assistant_idx = next(
                (i for i, m in enumerate(self.history) if m.get("role") == "assistant"),
                len(self.history)
            )

            # If worker didn't add assistant message yet, add it now
            if last_assistant_idx == len(self.history):
                self.history.append({"role": "assistant", "content": self.ai_buffer})
            elif not self.history[last_assistant_idx].get("content"):
                # Worker added message but no content yet (has tool_calls)
                self.history[last_assistant_idx]["content"] = self.ai_buffer

        self.ai_buffer = ""

        while (self.transcript
               and self.transcript[-1][0] in ("assistant", "note")
               and not self.transcript[-1][1].strip()):
            self.transcript.pop()

        self._save_current_history()

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

        # Start thinking indicator immediately
        self.model_indicator.set_status("thinking")

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
            self.model_indicator.set_status("thinking")
        else:
            self.statusBar().showMessage("Generating response...")
            self.model_indicator.update_thinking_message("Generating")

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

    def _save_current_history(self):
        """Save current conversation history to disk with all tool calls."""
        if not self.current_conv_id or not self.history:
            return

        self.conversation_mgr.save_conversation(
            self.current_conv_id,
            self.history,
            title=None  # Preserve existing title
        )

        conv_data = self.conversation_mgr.load_conversation(self.current_conv_id)
        if conv_data and conv_data.get("title") == "New Conversation":
            self._request_ai_title(self.current_conv_id)

    def _on_tools_done(self, updated_messages: list = None):
        # If worker sent back the full messages list, preserve tool calls
        if updated_messages is not None:
            # Reconstruct history preserving original structure but keeping tool_calls
            new_history = []
            for m in updated_messages:
                if m.get("role") == "system":
                    continue
                # Ensure only serializable fields are kept
                entry = {
                    "role": m.get("role"),
                    "content": m.get("content", ""),
                }
                if "tool_calls" in m and m["tool_calls"]:
                    entry["tool_calls"] = m["tool_calls"]
                if m.get("role") == "tool":
                    entry["tool_name"] = m.get("tool_name", "")
                new_history.append(entry)
            self.history = new_history

        self._finalize_assistant()
        self._render()
        self._set_busy(False)
        self.model_indicator.set_status("idle")
        self.statusBar().showMessage("Response complete", 3000)

    def _on_tool_error(self, message: str):
        self._finalize_assistant()
        self.statusBar().showMessage(f"Tool error: {message}", 5000)
        self._render()
        self._set_busy(False)
        self.model_indicator.set_status("error", "Tool error")

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        if busy:
            self.model_indicator.set_status("thinking")
        else:
            self.model_indicator.set_status("idle")

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
            if role == "tool":
                safe = text.replace("```", "'''")
                parts.append(f"```text\n{safe}\n```")
                continue
            if not text.strip():
                continue
            label = {"user": "You", "assistant": "Assistant"}.get(role, role)
            if role == "note":
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
