"""VibeCoder main window: chat UI with attachment scaffolding.

Phase 1 intentionally excludes projects, git, and the code editor.
The attachment pipeline (files -> text embedded in the user message)
is the same mechanism Phase 2 will use for project files.
"""

from pathlib import Path

import requests
import os
import sys
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QTextBrowser, QTextEdit,
    QVBoxLayout, QWidget,
)

from vibe_coder import __version__
from vibe_coder.config import load_config, save_config
from vibe_coder.memory import MemoryStore
from vibe_coder.ollama_client import OllamaClient

# Phase 1 attachment cap; Phase 2 will replace ad-hoc reads with
# project-scoped file access.
MAX_ATTACHMENT_CHARS = 200_000

if sys.platform.startswith('linux'):
    os.environ.setdefault('QT_QPA_PLATFORMTHEME', 'xdgdesktopportal')

class ChatWorker(QThread):
    """Streams a chat response off the GUI thread.

    All signals are emitted from this thread's context; slots connected
    with default AutoConnection are queued onto the GUI thread, so there
    is no cross-thread widget access (avoids the QNetworkAccessManager /
    child-object threading class of bugs you've hit before).
    """

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
        except Exception as exc:  # noqa: BLE001 - surfaced to GUI slot
            if not self._client.aborted:
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
    """Manual memory editor. Automatic extraction is deferred by design."""

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
        buttons.clicked.connect(lambda _: self.accept())

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
    def __init__(self, config: dict, client: OllamaClient, parent=None):
        super().__init__(parent)
        self.config = config
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
        self.sys_edit.setPlaceholderText(
            "System message, e.g. 'My OS is CachyOS unless otherwise specified.'"
        )
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
        form.addWidget(buttons)

        self.refresh_models()

    def refresh_models(self):
        # Read the URL live so refreshing picks up edits before saving.
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
        for m in reversed(models):  # biggest first in the dropdown
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"VibeCoder v{__version__}")
        self.resize(820, 640)

        self.config = load_config()
        self.memory_store = MemoryStore()
        self.client = OllamaClient(self.config["server_url"])

        # Conversation state. System message is rebuilt per request so
        # settings/memories take effect immediately without restart.
        self.history: list[dict] = []      # {"role", "content"} sent to model
        self.transcript: list[tuple] = []   # (role, text) for display
        self.pending_attachments: list[str] = []
        self.ai_buffer = ""
        self.worker: ChatWorker | None = None
        self.model: str | None = None

        self._build_ui()
        self._bootstrap()

    # ---- UI construction -------------------------------------------

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        top = QHBoxLayout()
        top.addStretch(1)
        top.addWidget(self.settings_btn)
        layout.addLayout(top)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        layout.addWidget(self.chat_view, stretch=1)

        self.attach_label = QLabel("")
        self.attach_label.setStyleSheet("color: gray;")
        layout.addWidget(self.attach_label)

        self.input = ChatInput()
        self.input.setPlaceholderText(
            "Message the model... (Enter to send, Shift+Enter for newline)"
        )
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
        layout.addLayout(bottom)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Starting...")

    # ---- Startup ---------------------------------------------------

    def _bootstrap(self):
        """Resolve the model from what is actually installed."""
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
            # Configured model is gone/renamed, or none was set:
            # fall back to the smallest coder model.
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

    # ---- Messaging -------------------------------------------------

    def _system_messages(self) -> list[dict]:
        """Assemble the system message from config + (optional) memories.

        Kept deliberately compact: raw system message plus at most
        memory_max_chars characters of bullets. Nothing else.
        """
        parts = []
        if self.config["system_message"].strip():
            parts.append(self.config["system_message"].strip())
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
        if self.worker is not None:
            return  # a stream is in flight
        if self.model is None:
            QMessageBox.warning(
                self, "No model", "No model available. Check Settings."
            )
            return
        text = self.input.toPlainText().strip()
        if not text and not self.pending_attachments:
            return

        user_content = text
        attachment_ctx = self._attachment_context(self.pending_attachments)
        if attachment_ctx:
            user_content = f"{text}\n\n{attachment_ctx}".strip()

        self.history.append({"role": "user", "content": user_content})
        self.transcript.append(("user", text))
        for path in self.pending_attachments:
            self.transcript.append(("note", f"attached: {path}"))

        self.pending_attachments.clear()
        self.attach_label.setText("")
        self.input.clear()
        self.ai_buffer = ""
        self.transcript.append(("assistant", ""))

        self._set_busy(True)
        self._render()

        self.worker = ChatWorker(
            self.client, self.model,
            self._system_messages() + self.history,
        )
        self.worker.chunk_received.connect(self._on_chunk)
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_error)
        # QThread.finished fires only after run() returns and the thread
        # has wound down - the safe point to schedule release.
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

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
        # Fires on QThread.finished. Covers the Stop-button path where
        # neither finished_ok nor failed is emitted, and releases the
        # worker reference now that the thread has fully stopped.
        if self.ai_buffer:
            self._finalize_assistant()
            self._render()
        self._set_busy(False)
        if self.sender() is self.worker:
            self.worker = None

    def _finalize_assistant(self):
        # Idempotent: several end-of-stream paths may fire in sequence.
        if self.ai_buffer:
            self.history.append(
                {"role": "assistant", "content": self.ai_buffer}
            )
        elif self.transcript and self.transcript[-1][0] == "assistant":
            if not self.transcript[-1][1]:
                self.transcript.pop()
        self.ai_buffer = ""

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    # ---- Attachments (scaffolding for Phase 2) ---------------------

    def _attach_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Attach file")
        if path:
            self.pending_attachments.append(path)
            names = ", ".join(Path(p).name for p in self.pending_attachments)
            self.attach_label.setText(f"Attached: {names}")

    @staticmethod
    def _attachment_context(paths: list[str]) -> str:
        """Embed attachments as fenced text blocks in the user message.

        Phase 2 will reuse this exact pipeline for project files;
        images will switch to Ollama's multimodal message format here.
        """
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
                f"```{lang}\n{content}\n```"
            )
        return "\n\n".join(sections)

    # ---- Rendering -------------------------------------------------

    def _render(self):
        parts = []
        for role, text in self.transcript:
            label = {"user": "You", "assistant": "Assistant",
                     "note": "_note_"}.get(role, role)
            if role == "note":
                parts.append(f"*{text}*")
            else:
                parts.append(f"**{label}**\n\n{text}")
        self.chat_view.setMarkdown("\n\n---\n\n".join(parts))
        scrollbar = self.chat_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ---- Settings --------------------------------------------------

    def _open_settings(self):
        if self.worker is not None:
            QMessageBox.information(
                self, "Busy", "Wait for the current response to finish."
            )
            return
        dialog = SettingsDialog(self.config, self.client, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        url_changed = values["server_url"] != self.config["server_url"]
        self.config.update(values)
        save_config(self.config)
        if url_changed:
            self.client = OllamaClient(self.config["server_url"])
        self._bootstrap()
