"""Conversation engine: uses the claude CLI as backend (works with Max plan)."""

import json
import shutil

from PySide6.QtCore import QObject, QProcess, Signal


SYSTEM_PROMPT = """\
You are an AI writing assistant integrated with LyX, a document processor for LaTeX.
The user is a philosopher writing a multi-volume academic book. You help with:
- Discussing and refining arguments, structure, and prose
- Suggesting edits to the document (as search-and-replace on raw LyX text)
- Answering questions about the text

## LyX Format

The document is in LyX format (.lyx), a plain-text markup. Key conventions:
- `\\begin_inset` / `\\end_inset` delimit insets (cross-refs, emphasis, foreign language, etc.)
- `\\emph on` / `\\emph default` toggle emphasis
- `\\lang ngerman`, `\\lang french`, `\\lang italian` switch languages within `\\begin_inset Text` blocks
- `\\begin_inset CommandInset ref` for cross-references
- `\\begin_layout Standard` for normal paragraphs
- `\\begin_layout Chapter*` for unnumbered chapters
- Blank lines separate paragraphs within a layout block

## Editing Rules

When proposing edits to the document:
1. Show the exact old text and new text for search-and-replace
2. The old_text must be unique in the document
3. Include enough surrounding context for uniqueness
4. Work with the raw LyX markup, not rendered text
5. Preserve all LyX inset structure (don't break \\begin_inset/\\end_inset pairs)

## Current Document

The current document content is provided below. When it changes (user saves in LyX),
you'll receive an updated version.
"""


class ConversationEngine(QObject):
    """Manages conversation with Claude via the claude CLI."""

    streaming_chunk = Signal(str)
    response_finished = Signal(str)
    response_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._document_content: str = ""
        self._model = "sonnet"
        self._process: QProcess | None = None
        self._session_id: str | None = None
        self._buffer: str = ""
        self._full_text: str = ""
        self._stderr_buf: str = ""
        self._claude_bin = shutil.which("claude") or "claude"

    def set_model(self, model_id: str):
        self._model = model_id

    def set_document_content(self, content: str):
        self._document_content = content

    def _build_system(self) -> str:
        system = SYSTEM_PROMPT
        if self._document_content:
            doc = self._document_content
            if len(doc) > 300_000:
                doc = doc[:300_000] + "\n\n[... document truncated ...]"
            system += f"\n\n---\n\n{doc}"
        return system

    def send_message(self, user_text: str):
        """Send a user message via claude CLI with streaming JSON output."""
        if self._process and self._process.state() != QProcess.NotRunning:
            return

        self._buffer = ""
        self._full_text = ""
        self._stderr_buf = ""

        args = [
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", self._model,
            "--system-prompt", self._build_system(),
            "--dangerously-skip-permissions",
        ]

        # Resume session for multi-turn conversation
        if self._session_id:
            args.extend(["--resume", self._session_id])

        args.append("--")  # separate options from positional prompt
        args.append(user_text)

        self._process = QProcess(self)
        self._process.setProgram(self._claude_bin)
        self._process.setArguments(args)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.readyReadStandardError.connect(self._on_stderr)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.start()
        self._process.closeWriteChannel()  # claude CLI waits for stdin to close

    def _on_stdout(self):
        data = self._process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self._buffer += data

        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_stream_message(msg)

    def _handle_stream_message(self, msg: dict):
        msg_type = msg.get("type", "")

        if msg_type == "system" and msg.get("subtype") == "init":
            sid = msg.get("session_id")
            if sid:
                self._session_id = sid

        elif msg_type == "stream_event":
            event = msg.get("event", {})
            event_type = event.get("type", "")

            if event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        self._full_text += text
                        self.streaming_chunk.emit(text)

        elif msg_type == "assistant":
            # Full assistant message — capture session_id
            sid = msg.get("session_id")
            if sid:
                self._session_id = sid

        elif msg_type == "result":
            # Final result object — we handle completion in _on_finished
            pass

    def _on_stderr(self):
        data = self._process.readAllStandardError().data().decode("utf-8", errors="replace")
        if data.strip():
            self._stderr_buf += data

    def _on_finished(self, exit_code, exit_status):
        # Flush remaining buffer
        if self._buffer.strip():
            for line in self._buffer.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    self._handle_stream_message(msg)
                except json.JSONDecodeError:
                    pass
            self._buffer = ""

        if exit_code == 0 and self._full_text:
            self.response_finished.emit(self._full_text)
        elif exit_code != 0:
            err = self._stderr_buf.strip() if self._stderr_buf.strip() else f"claude exited with code {exit_code}"
            self.response_error.emit(err)
        else:
            self.response_finished.emit(self._full_text or "(empty response)")

        self._stderr_buf = ""

    def _on_process_error(self, error):
        error_map = {
            QProcess.FailedToStart: "Failed to start claude CLI. Is it installed?",
            QProcess.Crashed: "claude CLI crashed",
            QProcess.Timedout: "claude CLI timed out",
        }
        self.response_error.emit(error_map.get(error, f"Process error: {error}"))

    def clear_history(self):
        self._session_id = None

    def is_busy(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning
