"""PySide6 chat UI for LyX-Claude sidecar."""

from pathlib import Path

from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QAction, QFont, QFontDatabase, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
    QPlainTextEdit, QPushButton, QSplitter, QStatusBar,
    QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .document import DocumentManager
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


class MainWindow(QMainWindow):
    """Main chat window with project file sidebar."""

    FLUSH_INTERVAL_MS = 40  # batch streaming updates (~25 fps)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LyX-Claude")
        self.resize(950, 900)

        self._doc_manager = DocumentManager(self)
        self._engine = ConversationEngine(self)

        # Streaming text buffer — flushed on timer
        self._pending_text = ""
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

        splitter.addWidget(chat_panel)
        splitter.setSizes([220, 700])

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

    def _connect_signals(self):
        self._engine.streaming_chunk.connect(self._on_chunk)
        self._engine.response_finished.connect(self._on_response_done)
        self._engine.response_error.connect(self._on_error)
        self._doc_manager.content_changed.connect(self._on_doc_changed)

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
        self._engine.send_message(text)
        self._send_btn.setEnabled(False)
        self._status.showMessage("Claude is thinking...")
        self._flush_timer.start()

    # --- Streaming handlers ---

    @Slot(str)
    def _on_chunk(self, text: str):
        self._pending_text += text

    @Slot()
    def _flush_pending(self):
        """Flush accumulated text to the display in one batch."""
        if not self._pending_text:
            return
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
        self._append_line("\n")
        self._send_btn.setEnabled(True)
        self._status.showMessage("Ready")

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

    # --- Text output ---

    def _append_line(self, text: str):
        """Append a complete line to the display."""
        self._chat_display.appendPlainText(text)
        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())
