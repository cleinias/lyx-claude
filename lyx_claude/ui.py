"""PySide6 chat UI for LyX-Claude sidecar."""

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtGui import QAction, QFont, QFontDatabase, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStatusBar, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .document import DocumentManager
from .edits import EditProposal, apply_edit
from .engine import ConversationEngine


def _mono_font(size: int = 10) -> QFont:
    """Return a compact monospace font."""
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPointSize(size)
    return font


class ChatInput(QPlainTextEdit):
    """Text input that sends on Enter, inserts newline on Shift+Enter."""

    def __init__(self, send_callback, parent=None):
        super().__init__(parent)
        self._send = send_callback
        self.setPlaceholderText("Type a message... (Enter to send, Shift+Enter for newline)")
        self.setMaximumHeight(120)
        self.setFont(_mono_font())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self._send()
                return
        super().keyPressEvent(event)


class FileTree(QTreeWidget):
    """Tree widget showing .lyx files in the project."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabel("Project files")
        self.setRootIsDecorated(True)
        self.setAnimated(True)
        self.setFont(_mono_font(9))

    def populate(self, root: Path, files: list[Path]):
        """Fill the tree with .lyx files grouped by directory."""
        self.clear()

        dirs: dict[str, list[Path]] = {}
        for f in files:
            rel = f.relative_to(root)
            parent = str(rel.parent) if rel.parent != Path(".") else ""
            dirs.setdefault(parent, []).append(f)

        for dir_name in sorted(dirs.keys()):
            file_list = dirs[dir_name]
            if dir_name:
                dir_item = QTreeWidgetItem(self, [dir_name])
                dir_item.setFlags(dir_item.flags() & ~Qt.ItemIsSelectable)
                font = dir_item.font(0)
                font.setBold(True)
                dir_item.setFont(0, font)
                for f in sorted(file_list):
                    child = QTreeWidgetItem(dir_item, [f.name])
                    child.setData(0, Qt.UserRole, str(f))
                dir_item.setExpanded(True)
            else:
                for f in sorted(file_list):
                    item = QTreeWidgetItem(self, [f.name])
                    item.setData(0, Qt.UserRole, str(f))

    def highlight_file(self, filepath: str):
        """Select the item matching the given filepath."""
        iterator = self.findItems("*", Qt.MatchWildcard | Qt.MatchRecursive)
        for item in iterator:
            path = item.data(0, Qt.UserRole)
            if path == filepath:
                self.setCurrentItem(item)
                return


class EditProposalCard(QFrame):
    """Card showing a single edit proposal with diff and Accept/Reject buttons."""

    accepted = Signal(object)   # emits the EditProposal
    rejected = Signal(object)   # emits the EditProposal

    _OLD_STYLE = "background-color: #3c1f1f; color: #f8d7da; padding: 6px; border-radius: 3px;"
    _NEW_STYLE = "background-color: #1f3c1f; color: #d4edda; padding: 6px; border-radius: 3px;"
    _RESOLVED_STYLE = "background-color: #2a2a2a; color: #888;"

    def __init__(self, proposal: EditProposal, parent=None):
        super().__init__(parent)
        self._proposal = proposal
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.setStyleSheet("EditProposalCard { border: 1px solid #555; border-radius: 4px; padding: 4px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # Header: file name + buttons
        header = QHBoxLayout()
        file_label = QLabel(f"<b>{proposal.file_path}</b>")
        file_label.setStyleSheet("color: #ccc;")
        header.addWidget(file_label, stretch=1)

        self._accept_btn = QPushButton("Accept")
        self._accept_btn.setFixedWidth(70)
        self._accept_btn.setStyleSheet("background-color: #2e7d32; color: white; border-radius: 3px; padding: 3px;")
        self._accept_btn.clicked.connect(self._on_accept)
        header.addWidget(self._accept_btn)

        self._reject_btn = QPushButton("Reject")
        self._reject_btn.setFixedWidth(70)
        self._reject_btn.setStyleSheet("background-color: #c62828; color: white; border-radius: 3px; padding: 3px;")
        self._reject_btn.clicked.connect(self._on_reject)
        header.addWidget(self._reject_btn)

        layout.addLayout(header)

        # Old text (red)
        self._old_label = QLabel(self._truncate(proposal.old_text))
        self._old_label.setFont(_mono_font(9))
        self._old_label.setWordWrap(True)
        self._old_label.setStyleSheet(self._OLD_STYLE)
        self._old_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self._old_label)

        # Arrow
        arrow = QLabel("\u2193")  # ↓
        arrow.setAlignment(Qt.AlignCenter)
        arrow.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(arrow)

        # New text (green)
        self._new_label = QLabel(self._truncate(proposal.new_text))
        self._new_label.setFont(_mono_font(9))
        self._new_label.setWordWrap(True)
        self._new_label.setStyleSheet(self._NEW_STYLE)
        self._new_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self._new_label)

        # Status label (hidden until resolved)
        self._status_label = QLabel()
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setStyleSheet("color: #888; padding: 4px;")
        self._status_label.hide()
        layout.addWidget(self._status_label)

    @staticmethod
    def _truncate(text: str, max_lines: int = 15) -> str:
        lines = text.split("\n")
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
        return text

    def _on_accept(self):
        self._proposal.status = "accepted"
        self._resolve("Accepted")
        self.accepted.emit(self._proposal)

    def _on_reject(self):
        self._proposal.status = "rejected"
        self._resolve("Rejected")
        self.rejected.emit(self._proposal)

    def _resolve(self, label: str):
        self._accept_btn.hide()
        self._reject_btn.hide()
        self._old_label.setStyleSheet(self._RESOLVED_STYLE)
        self._new_label.setStyleSheet(self._RESOLVED_STYLE)
        self._status_label.setText(label)
        self._status_label.show()


class EditPanel(QWidget):
    """Collapsible panel that holds edit proposal cards."""

    all_resolved = Signal()  # emitted when every card has been accepted or rejected

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: list[EditProposalCard] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(2)

        # Header bar
        header = QHBoxLayout()
        title = QLabel("<b>Proposed Edits</b>")
        title.setStyleSheet("color: #ddd; padding: 2px;")
        header.addWidget(title, stretch=1)

        self._accept_all_btn = QPushButton("Accept All")
        self._accept_all_btn.setFixedWidth(90)
        self._accept_all_btn.setStyleSheet("background-color: #2e7d32; color: white; border-radius: 3px; padding: 3px;")
        self._accept_all_btn.clicked.connect(self._accept_all)
        header.addWidget(self._accept_all_btn)

        self._reject_all_btn = QPushButton("Reject All")
        self._reject_all_btn.setFixedWidth(90)
        self._reject_all_btn.setStyleSheet("background-color: #c62828; color: white; border-radius: 3px; padding: 3px;")
        self._reject_all_btn.clicked.connect(self._reject_all)
        header.addWidget(self._reject_all_btn)

        layout.addLayout(header)

        # Scroll area for cards
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMaximumHeight(300)
        self._scroll.setStyleSheet("QScrollArea { border: none; }")

        self._card_container = QWidget()
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(6)
        self._card_layout.addStretch()
        self._scroll.setWidget(self._card_container)

        layout.addWidget(self._scroll)

        self.hide()

    def show_proposals(self, proposals: list):
        """Display a new batch of edit proposals."""
        self._clear_cards()

        for proposal in proposals:
            card = EditProposalCard(proposal)
            card.accepted.connect(self._on_card_resolved)
            card.rejected.connect(self._on_card_resolved)
            self._cards.append(card)
            # Insert before the stretch
            self._card_layout.insertWidget(self._card_layout.count() - 1, card)

        self.show()

    def _clear_cards(self):
        for card in self._cards:
            self._card_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def _on_card_resolved(self, proposal):
        # Check if all are resolved
        if all(c._proposal.status != "pending" for c in self._cards):
            self._accept_all_btn.hide()
            self._reject_all_btn.hide()
            self.all_resolved.emit()

    def _accept_all(self):
        for card in self._cards:
            if card._proposal.status == "pending":
                card._on_accept()

    def _reject_all(self):
        for card in self._cards:
            if card._proposal.status == "pending":
                card._on_reject()


class MainWindow(QMainWindow):
    """Main chat window with project file sidebar."""

    FLUSH_INTERVAL_MS = 40  # batch streaming updates (~25 fps)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LyX-Claude")
        self.resize(950, 900)

        self._doc_manager = DocumentManager(self)
        self._engine = ConversationEngine(self)
        self._bridge = None  # set via set_bridge() if pipe integration is enabled

        # Streaming text buffer — flushed on timer
        self._pending_text = ""
        self._has_received_text = False
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(self.FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush_pending)

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        # Menu bar
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        open_project_action = QAction("Open &project folder...", self)
        open_project_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_project_action.triggered.connect(self._open_project)
        file_menu.addAction(open_project_action)

        open_action = QAction("&Open .lyx file...", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self._open_file)
        file_menu.addAction(open_action)

        refresh_action = QAction("&Refresh context", self)
        refresh_action.setShortcut(QKeySequence("Ctrl+R"))
        refresh_action.triggered.connect(self._refresh_context)
        file_menu.addAction(refresh_action)

        file_menu.addSeparator()

        clear_action = QAction("&Clear conversation", self)
        clear_action.triggered.connect(self._clear_conversation)
        file_menu.addAction(clear_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Toolbar
        toolbar = self.addToolBar("Main")
        toolbar.addAction(open_project_action)
        toolbar.addAction(refresh_action)
        toolbar.addAction(clear_action)

        # Central widget with splitter: file tree | chat
        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Horizontal)
        outer_layout.addWidget(splitter)

        # Left: file tree
        self._file_tree = FileTree()
        self._file_tree.itemClicked.connect(self._on_file_tree_clicked)
        self._file_tree.setMinimumWidth(150)
        splitter.addWidget(self._file_tree)

        # Right: chat panel
        chat_panel = QWidget()
        chat_layout = QVBoxLayout(chat_panel)
        chat_layout.setContentsMargins(4, 0, 0, 0)

        # Document info bar
        self._doc_label = QLabel("No document loaded  —  open a project folder or .lyx file")
        self._doc_label.setStyleSheet("color: gray; padding: 4px;")
        chat_layout.addWidget(self._doc_label)

        # Chat display — plain text, read-only, fast
        self._chat_display = QPlainTextEdit()
        self._chat_display.setReadOnly(True)
        self._chat_display.setFont(_mono_font())
        self._chat_display.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        chat_layout.addWidget(self._chat_display, stretch=1)

        # Edit proposal panel (hidden until proposals arrive)
        self._edit_panel = EditPanel()
        chat_layout.addWidget(self._edit_panel)

        # Input area
        input_layout = QHBoxLayout()
        self._chat_input = ChatInput(self._send_message)
        input_layout.addWidget(self._chat_input, stretch=1)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(60)
        send_btn.clicked.connect(self._send_message)
        self._send_btn = send_btn
        input_layout.addWidget(send_btn, alignment=Qt.AlignBottom)

        chat_layout.addLayout(input_layout)

        # Command bar — keyboard shortcut hints
        cmd_bar = QLabel(
            "Enter Send  |  Shift+Enter Newline  |  "
            "Ctrl+R Refresh  |  Ctrl+O Open file  |  "
            "Ctrl+Shift+O Open project"
        )
        cmd_bar.setStyleSheet(
            "color: #777; background-color: #1a1a1a; padding: 3px 8px;"
            "border-top: 1px solid #333; font-size: 9pt;"
        )
        cmd_bar.setAlignment(Qt.AlignCenter)
        chat_layout.addWidget(cmd_bar)

        splitter.addWidget(chat_panel)
        splitter.setSizes([220, 700])

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

        # Permanent LyX connection indicator (right side of status bar)
        self._bridge_label = QLabel("LyX: No pipe")
        self._bridge_label.setStyleSheet("color: #888; padding: 2px 8px;")
        self._status.addPermanentWidget(self._bridge_label)

    def _connect_signals(self):
        self._engine.streaming_chunk.connect(self._on_chunk)
        self._engine.response_finished.connect(self._on_response_done)
        self._engine.response_error.connect(self._on_error)
        self._engine.edits_proposed.connect(self._on_edits_proposed)
        self._engine.parse_debug.connect(self._on_parse_debug)
        self._doc_manager.content_changed.connect(self._on_doc_changed)
        self._edit_panel.all_resolved.connect(self._on_edits_resolved)

    # --- LyX pipe bridge ---

    def set_bridge(self, bridge):
        """Enable live LyX pipe integration."""
        self._bridge = bridge
        self._bridge_label.setText("LyX: Disconnected")
        self._bridge_label.setStyleSheet("color: #f44336; padding: 2px 8px;")
        bridge.connection_changed.connect(self._on_bridge_connection)
        bridge.filename_changed.connect(self._on_bridge_filename)

    @Slot(bool)
    def _on_bridge_connection(self, connected: bool):
        if connected:
            self._bridge_label.setText("LyX: Connected")
            self._bridge_label.setStyleSheet("color: #4caf50; padding: 2px 8px;")
        else:
            self._bridge_label.setText("LyX: Disconnected")
            self._bridge_label.setStyleSheet("color: #f44336; padding: 2px 8px;")

    @Slot(str)
    def _on_bridge_filename(self, filename: str):
        """LyX switched to a different file — auto-load it."""
        path = Path(filename)
        if path.exists() and path.suffix == ".lyx":
            current = self._doc_manager.get_path()
            if current is None or current.resolve() != path.resolve():
                self.load_file(path)

    # --- Project / file actions ---

    def load_project(self, project_dir: str | Path):
        project_dir = Path(project_dir)
        self._doc_manager.set_project_root(project_dir)
        files = self._doc_manager.scan_project()
        self._file_tree.populate(project_dir, files)
        self.setWindowTitle(f"LyX-Claude — {project_dir.name}")
        self._status.showMessage(f"Project: {project_dir}  ({len(files)} .lyx files)")

    def load_file(self, path: str | Path):
        path = Path(path)
        try:
            content = self._doc_manager.open_file(path)
            self._engine.set_document_content(content)
            name = path.name
            self._doc_label.setText(f"{name}  ({len(content):,} chars)")
            self._status.showMessage(f"Loaded: {path}")
            self._append_line(f"--- Document loaded: {name} ---")
            self._file_tree.highlight_file(str(path))
        except Exception as e:
            self._status.showMessage(f"Error: {e}")

    @Slot()
    def _open_project(self):
        path = QFileDialog.getExistingDirectory(
            self, "Open project folder", str(Path.home())
        )
        if path:
            self.load_project(path)

    @Slot()
    def _open_file(self):
        start_dir = str(self._doc_manager.get_project_root() or Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Open LyX file", start_dir,
            "LyX files (*.lyx);;All files (*)"
        )
        if path:
            self.load_file(path)

    @Slot(QTreeWidgetItem, int)
    def _on_file_tree_clicked(self, item: QTreeWidgetItem, column: int):
        filepath = item.data(0, Qt.UserRole)
        if filepath:
            self.load_file(filepath)

    @Slot()
    def _refresh_context(self):
        content = self._doc_manager.refresh()
        if content:
            self._engine.set_document_content(content)
            self._status.showMessage("Context refreshed")
            self._append_line("--- Context refreshed ---")
        else:
            self._status.showMessage("No document to refresh")

    @Slot()
    def _clear_conversation(self):
        self._engine.clear_history()
        self._chat_display.clear()
        self._status.showMessage("Conversation cleared")

    @Slot()
    def _send_message(self):
        text = self._chat_input.toPlainText().strip()
        if not text or self._engine.is_busy():
            return

        self._chat_input.clear()
        self._append_line(f"> {text}")
        self._append_line("")
        self._has_received_text = False
        self._engine.send_message(text)
        self._send_btn.setEnabled(False)
        self._status.showMessage("Thinking...")
        self._flush_timer.start()

    # --- Streaming handlers ---

    @Slot(str)
    def _on_chunk(self, text: str):
        self._pending_text += text

    @Slot()
    def _flush_pending(self):
        """Flush accumulated text to the display in one batch."""
        # Update status bar with elapsed time
        busy = self._engine.is_busy()
        if busy:
            elapsed = self._engine.elapsed()
            if self._has_received_text:
                self._status.showMessage(f"Responding... {elapsed:.0f}s")
            else:
                self._status.showMessage(f"Thinking... {elapsed:.0f}s")

        if not self._pending_text:
            return
        self._has_received_text = True
        text = self._pending_text
        self._pending_text = ""
        cursor = self._chat_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self._chat_display.setTextCursor(cursor)
        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())

    @Slot(str)
    def _on_response_done(self, full_text: str):
        self._flush_pending()
        self._flush_timer.stop()

        # Build completion summary
        duration = self._engine.last_duration()
        usage = self._engine.last_usage()
        parts = [f"{duration:.0f}s"]
        if usage:
            out_tok = usage.get("output_tokens")
            if out_tok:
                parts.append(f"{out_tok:,} tok")
        summary = " | ".join(parts)

        # Visible separator after the streamed response, with timing info.
        # _append_line scrolls to bottom (appendPlainText alone does not).
        self._append_line("")
        self._append_line(f"--- [{summary}] ---")
        self._append_line("")
        self._send_btn.setEnabled(True)
        self._status.showMessage(f"Ready  —  {summary}")

    @Slot(str)
    def _on_error(self, error_msg: str):
        self._flush_pending()
        self._flush_timer.stop()
        self._append_line(f"\n[Error: {error_msg}]\n")
        self._send_btn.setEnabled(True)
        self._status.showMessage("Error occurred")

    @Slot(str, str)
    def _on_doc_changed(self, filepath: str, content: str):
        self._engine.set_document_content(content)
        name = Path(filepath).name
        self._doc_label.setText(f"{name}  ({len(content):,} chars)")
        self._status.showMessage(f"Document updated: {name}")

    # --- Edit proposal handlers ---

    @Slot(list)
    def _on_edits_proposed(self, proposals: list):
        """Show proposals in the edit panel and wire up accept/reject."""
        self._edit_panel.show_proposals(proposals)

        # Connect each card's accepted signal to apply the edit
        for card in self._edit_panel._cards:
            card.accepted.connect(self._apply_proposal)
            card.rejected.connect(self._reject_proposal)

        n = len(proposals)
        duration = self._engine.last_duration()
        self._status.showMessage(
            f"{n} edit{'s' if n != 1 else ''} proposed  —  {duration:.0f}s  —  review below"
        )

    def _apply_proposal(self, proposal):
        """Apply an accepted edit to disk."""
        project_root = self._doc_manager.get_project_root()
        if not project_root:
            self._append_line("[Error: no project root set — cannot apply edit]")
            return

        ok = apply_edit(project_root, proposal.file_path, proposal.old_text, proposal.new_text)
        if ok:
            self._append_line(f"[Applied edit to {proposal.file_path}]")
            # Tell LyX to reload from disk
            if self._bridge and self._bridge.is_connected():
                self._bridge.reload_buffer()
            # Refresh context if this is the currently loaded file
            current = self._doc_manager.get_path()
            edited = (project_root / proposal.file_path).resolve()
            if current and current.resolve() == edited:
                self._refresh_context()
        else:
            self._append_line(f"[Failed to apply edit to {proposal.file_path} — old text not found or not unique]")

    def _reject_proposal(self, proposal):
        """Log a rejected edit."""
        self._append_line(f"[Rejected edit to {proposal.file_path}]")

    @Slot(str)
    def _on_parse_debug(self, msg: str):
        self._append_line(f"[{msg}]")

    @Slot()
    def _on_edits_resolved(self):
        """Hide the edit panel after all proposals are resolved."""
        # Small delay so the user can see the final state
        QTimer.singleShot(1500, self._edit_panel.hide)

    # --- Text output ---

    def _append_line(self, text: str):
        """Append a complete line to the display."""
        self._chat_display.appendPlainText(text)
        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())
