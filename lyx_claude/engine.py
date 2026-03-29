"""Conversation engine: uses the claude CLI as backend (works with Max plan)."""

import json
import shutil
import sys
import time

from PySide6.QtCore import QObject, QProcess, Signal

from .edits import EditProposal, parse_proposals, strip_change_markers


SYSTEM_PROMPT = """\
You are an AI writing assistant integrated with LyX, a document processor for LaTeX.
The user is a philosopher writing a multi-volume academic book. You help with:
- Discussing and refining arguments, structure, and prose
- Suggesting edits to the document (as search-and-replace on raw LyX text)
- Answering questions about the text

## Selections

The user can paste text they copied from LyX into the chat.  When they do,
it appears between `[Selected text from LyX]` and `[End selection]` markers.
This is the specific passage they want you to focus on.
NEVER mention these markers, their format, or how the selection mechanism works
to the user — just work with the text as if they highlighted it for you.

## LyX Format

The document is in LyX format (.lyx), a plain-text markup. Key conventions:
- `\\begin_inset` / `\\end_inset` delimit insets (cross-refs, emphasis, foreign language, etc.)
- `\\emph on` / `\\emph default` toggle emphasis
- `\\lang ngerman`, `\\lang french`, `\\lang italian` switch languages within `\\begin_inset Text` blocks
- `\\begin_inset CommandInset ref` for cross-references
- `\\begin_layout Standard` for normal paragraphs
- `\\begin_layout Chapter*` for unnumbered chapters
- Blank lines separate paragraphs within a layout block

IMPORTANT — line wrapping in .lyx files:
LyX hard-wraps long lines at arbitrary positions in its source format.  These
line breaks are MEANINGLESS — LyX joins them back when rendering and exporting
to LaTeX.  For example, the source might say:
    "the missing element of an achieved form of historica
    l life—in Hegel's sense"
but this renders correctly as "historical life—in Hegel's sense".
Do NOT treat mid-word line breaks in .lyx source as typos or errors.
When proposing edits, you MUST match the exact line breaks as they appear in
the source, even if words are split across lines.

## How to Edit the Document

Your Edit and Write tools are disabled.  The ONLY way to modify files is by
outputting proposed-edit blocks in your response text.  The sidecar app parses
these blocks and applies them as tracked changes in LyX for the user to review
using LyX's built-in Track Changes interface (Document > Changes).

CRITICAL RULES — you MUST follow all of these:
- ALWAYS output the blocks directly when you want to suggest changes.
  NEVER ask "shall I provide the edit blocks?" or "would you like me to show
  the changes?" — just OUTPUT them immediately.
- NEVER mention the proposed-edit format to the user or explain how it works.
- NEVER wrap the blocks in markdown code fences (no ```).
- NEVER refer to the blocks as "XML" or discuss their syntax.
- Briefly describe what you are changing in plain language, then output the blocks.
- If the document contains \\change_inserted, \\change_deleted, or
  \\change_unchanged markers, IGNORE them when writing <old> text — they are
  tracking metadata, not part of the prose.

Format (one block per edit, raw in your response text, not in code fences):

<proposed-edit file="relative/path/to/file.lyx">
<old>
exact old text to find (multi-line OK)
</old>
<new>
exact replacement text (multi-line OK)
</new>
</proposed-edit>

The `file` attribute MUST be the exact filename provided in the "Current document"
section below — NEVER guess or invent a filename.
The old text must appear EXACTLY ONCE in the file — include enough surrounding
context (neighboring lines) to guarantee uniqueness.
Work with the raw LyX markup, not rendered text.
Preserve all LyX inset structure (never break \\begin_inset/\\end_inset pairs).
You may output multiple blocks in one response.

## Dual Context

You receive the document in two forms:
1. **Raw LyX markup** — use this for proposing edits (search-and-replace must match raw markup exactly)
2. **Plain text (rendered)** — use this for reading and understanding the prose naturally

The plain text is exported by LyX and reflects the rendered output without markup.
When it is available, prefer reading the plain text for comprehension, but always
edit the raw LyX markup.

The current document content is provided below. When it changes (user saves in LyX),
you'll receive an updated version.
"""


class ConversationEngine(QObject):
    """Manages conversation with Claude via the claude CLI."""

    streaming_chunk = Signal(str)
    response_finished = Signal(str)
    response_error = Signal(str)
    edits_proposed = Signal(list)   # list[EditProposal]
    parse_debug = Signal(str)       # diagnostic info about proposal parsing

    def __init__(self, parent=None):
        super().__init__(parent)
        self._document_content: str = ""
        self._document_relpath: str = ""
        self._plaintext_content: str = ""
        self._current_layout: str = ""
        self._model = "sonnet"
        self._process: QProcess | None = None
        self._session_id: str | None = None
        self._buffer: str = ""
        self._full_text: str = ""
        self._stderr_buf: str = ""
        self._claude_bin = shutil.which("claude") or "claude"
        self._start_time: float = 0.0
        self._usage: dict | None = None  # token usage from result message

    def set_model(self, model_id: str):
        self._model = model_id

    def set_document_content(self, content: str, relpath: str = ""):
        self._document_content = content
        if relpath:
            self._document_relpath = relpath

    def set_plaintext_content(self, content: str):
        self._plaintext_content = content

    def set_current_layout(self, layout: str):
        self._current_layout = layout

    def _build_system(self) -> str:
        system = SYSTEM_PROMPT
        if self._document_relpath:
            system += f"\n\n## Current document\n\nFilename: `{self._document_relpath}`\n"
            system += "Use this EXACT path in the `file` attribute of every proposed-edit block.\n"
        if self._document_content:
            # Strip tracked-change markers so Claude sees clean content
            doc = strip_change_markers(self._document_content)
            if len(doc) > 300_000:
                doc = doc[:300_000] + "\n\n[... document truncated ...]"
            system += f"\n\n---\n\n## Raw LyX markup\n\n{doc}"
        if self._plaintext_content:
            plain = self._plaintext_content
            if len(plain) > 100_000:
                plain = plain[:100_000] + "\n\n[... plain text truncated ...]"
            system += f"\n\n---\n\n## Plain text (rendered)\n\n{plain}"
        return system

    def send_message(self, user_text: str):
        """Send a user message via claude CLI with streaming JSON output."""
        if self._process and self._process.state() != QProcess.NotRunning:
            return

        self._buffer = ""
        self._full_text = ""
        self._stderr_buf = ""
        self._start_time = time.monotonic()
        self._usage = None

        args = [
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", self._model,
            "--system-prompt", self._build_system(),
            "--dangerously-skip-permissions",
            "--allowedTools", "Read,Glob,Grep",
        ]

        # Resume session for multi-turn conversation
        if self._session_id:
            args.extend(["--resume", self._session_id])

        args.append("--")  # separate options from positional prompt
        # Prepend layout context if available
        if self._current_layout:
            user_text = f"[Cursor is at layout: {self._current_layout}]\n\n{user_text}"
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
            # Capture token usage if present
            usage = msg.get("usage") or msg.get("result", {}).get("usage")
            if usage:
                self._usage = usage

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
            # Check for edit proposals in the response
            proposals = parse_proposals(self._full_text)
            if proposals:
                self.edits_proposed.emit(proposals)
            elif "<proposed-edit" in self._full_text:
                # Tags present but regex didn't match — emit debug info
                self.parse_debug.emit(
                    "Warning: <proposed-edit> tags found but could not be parsed"
                )
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

    def elapsed(self) -> float:
        """Seconds since the current request started."""
        if self._start_time and self.is_busy():
            return time.monotonic() - self._start_time
        return 0.0

    def last_duration(self) -> float:
        """Seconds the last completed request took."""
        if self._start_time:
            return time.monotonic() - self._start_time
        return 0.0

    def last_usage(self) -> dict | None:
        """Token usage dict from the last completed response, or None."""
        return self._usage
