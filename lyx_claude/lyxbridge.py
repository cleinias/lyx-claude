"""LyX server pipe bridge for live communication with the LyX editor.

LyX exposes a named-pipe interface (Edit > Preferences > Paths > Server pipe).
Given a base path like ``~/.lyx/lyxpipe``, two FIFOs are created:

* ``lyxpipe.in``  — commands written by clients, read by LyX
* ``lyxpipe.out`` — responses written by LyX, read by clients

Protocol format::

    Send:    LYXCMD:clientname:function:argument\\n
    Success: INFO:clientname:function:data\\n
    Error:   ERROR:clientname:function:error message\\n
    Hello:   LYXSRV:clientname:hello\\n
    Bye:     LYXSRV:clientname:bye\\n

**Caveat:** LyX closes and reopens both pipes on every buffer save.  This
bridge detects the broken pipe and reconnects automatically.
"""

import os
import select
import stat
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


def find_lyxpipe() -> str:
    """Auto-detect the LyX server pipe path across platforms and versions.

    Probes candidate locations for an active pipe (FIFO exists), preferring
    the newest LyX version found.  If no active pipe is found, returns the
    best-guess default so the reconnect timer can keep trying.

    Linux candidates:   ~/.lyx/lyxpipe, ~/.lyx-<version>/lyxpipe
    macOS candidates:   ~/Library/Application Support/LyX-<version>/lyxpipe
    """
    home = Path.home()
    candidates: list[Path] = []

    if sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        if app_support.is_dir():
            # Versioned dirs, newest first
            lyx_dirs = sorted(app_support.glob("LyX-*"), reverse=True)
            for d in lyx_dirs:
                if d.is_dir():
                    candidates.append(d / "lyxpipe")
            # Unversioned fallback
            candidates.append(app_support / "LyX" / "lyxpipe")
    else:
        # Linux / other Unix
        # Versioned dirs, newest first
        lyx_dirs = sorted(home.glob(".lyx-*"), reverse=True)
        for d in lyx_dirs:
            if d.is_dir():
                candidates.append(d / "lyxpipe")
        # Default unversioned
        candidates.append(home / ".lyx" / "lyxpipe")

    # Probe for an active FIFO
    for pipe_path in candidates:
        in_path = str(pipe_path) + ".in"
        try:
            if stat.S_ISFIFO(os.stat(in_path).st_mode):
                return str(pipe_path)
        except OSError:
            continue

    # No active pipe — return platform default for reconnect attempts
    if candidates:
        return str(candidates[0])
    return str(home / ".lyx" / "lyxpipe")


class LyXBridge(QObject):
    """Communicates with a running LyX instance via the server pipe protocol."""

    connection_changed = Signal(bool)   # True = connected, False = disconnected
    filename_changed = Signal(str)      # emitted when LyX's active file changes
    layout_changed = Signal(str)        # emitted when cursor's layout changes

    CLIENT_NAME = "lyxclaude"
    POLL_INTERVAL_MS = 3000
    RECONNECT_INTERVAL_MS = 5000
    READ_TIMEOUT = 0.5  # seconds

    def __init__(self, pipe_path: str = "~/.lyx/lyxpipe", parent=None):
        super().__init__(parent)
        self._pipe_path = Path(pipe_path).expanduser()
        self._in_fd: int | None = None
        self._out_fd: int | None = None
        self._connected = False
        self._current_filename = ""
        self._current_layout = ""

        # Periodic poll: check LyX's active filename
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)

        # Reconnect timer: runs when disconnected
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(self.RECONNECT_INTERVAL_MS)
        self._reconnect_timer.timeout.connect(self._try_connect)

    # --- Public API ---

    def start(self):
        """Begin connection attempts and polling."""
        if self._try_connect():
            return
        # LyX not reachable yet — keep trying in the background
        self._reconnect_timer.start()

    def stop(self):
        """Stop polling and disconnect."""
        self._poll_timer.stop()
        self._reconnect_timer.stop()
        self.disconnect()

    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        return self._try_connect()

    def disconnect(self):
        """Unregister from LyX and close pipes."""
        if self._connected:
            self._raw_send(f"LYXSRV:{self.CLIENT_NAME}:bye\n")
        self._cleanup()
        self._connected = False

    def send_command(self, function: str, argument: str = "") -> str | None:
        """Send an LFUN command and return the raw response, or None on failure."""
        cmd = f"LYXCMD:{self.CLIENT_NAME}:{function}:{argument}\n"
        if not self._raw_send(cmd):
            return None
        return self._read_response()

    def get_filename(self) -> str | None:
        """Return the full path of the document active in LyX, or None."""
        resp = self.send_command("server-get-name")
        if resp is None:
            return None
        return self._parse_info(resp)

    def get_layout(self) -> str | None:
        """Return the layout name at the cursor position, or None."""
        resp = self.send_command("server-get-layout")
        if resp is None:
            return None
        return self._parse_info(resp)

    def activate(self):
        """Bring LyX window to front."""
        self.send_command("lyx-activate")

    def export_selection(self) -> Path | None:
        """Export the current LyX selection as plain text via a temp buffer.

        On Wayland, pipe-dispatched LFUNs cannot write to the system clipboard.
        This works around that by using LyX's internal cut stack: ``copy``
        populates it (regardless of clipboard), then we paste into a temp
        buffer, export as text, and close it.
        """
        tmp_lyx = Path("/tmp/lyx-claude-sel.lyx")
        tmp_txt = Path("/tmp/lyx-claude-sel.txt")
        tmp_lyx.unlink(missing_ok=True)
        tmp_txt.unlink(missing_ok=True)

        # Stop polling to prevent response desync (poll responses
        # can pile up in the pipe and shift all subsequent reads).
        self._poll_timer.stop()
        self._drain_pipe()

        # Single command-sequence: LyX executes each step synchronously
        # before moving to the next, so paste sees the copy result, etc.
        # buffer-close is separate because buffer-export is async.
        seq = (
            "copy ; "
            "buffer-new ; "
            "paste ; "
            f"buffer-write-as {tmp_lyx} ; "
            "buffer-export text"
        )
        resp = self.send_command("command-sequence", seq)
        print(f"[selection] command-sequence → {resp!r}", file=sys.stderr)

        self._poll_timer.start()

        return tmp_txt

    def export_plaintext(self) -> Path | None:
        """Export the current buffer as plain text.

        Returns the expected .txt path (same dir as .lyx, with .txt extension),
        or None if the bridge is not connected.
        """
        name = self.get_filename()
        if name is None:
            return None
        self.send_command("buffer-export", "text")
        return Path(name).with_suffix(".txt")

    def word_replace(self, search: str, replace: str) -> bool:
        """Replace text using LyX's word-replace LFUN.

        Uses literal ``\\n`` as field separator per LyX protocol.
        Returns True if the command was sent successfully.
        """
        # word-replace format: "search\nreplace\n" with literal backslash-n
        arg = f"{search}\\n{replace}\\n"
        resp = self.send_command("word-replace", arg)
        return resp is not None

    def reload_buffer(self) -> bool:
        """Tell LyX to reload the current buffer from disk."""
        return self.send_command("buffer-reload") is not None

    def insert_text(self, text: str) -> bool:
        """Insert *text* at the current cursor position in LyX."""
        return self.send_command("self-insert", text) is not None

    def goto_file_row(self, filename: str, row: int) -> bool:
        """Navigate LyX to *filename* at *row*."""
        return self.send_command("server-goto-file-row", f"{filename} {row}") is not None

    # --- Internal helpers ---

    def _try_connect(self) -> bool:
        """Attempt to open the named pipes and register with LyX."""
        if self._connected:
            return True

        in_path = str(self._pipe_path) + ".in"
        out_path = str(self._pipe_path) + ".out"

        # Both FIFOs must exist
        for p in (in_path, out_path):
            try:
                if not stat.S_ISFIFO(os.stat(p).st_mode):
                    return False
            except OSError:
                return False

        try:
            # Open the read side first (LyX has .out open for writing)
            self._out_fd = os.open(out_path, os.O_RDONLY | os.O_NONBLOCK)
            # Open the write side (LyX has .in open for reading)
            self._in_fd = os.open(in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            self._cleanup()
            return False

        # Say hello and REQUIRE a response — without it, we're talking
        # to stale FIFOs left on disk from a previous LyX session.
        if not self._raw_send(f"LYXSRV:{self.CLIENT_NAME}:hello\n"):
            self._cleanup()
            return False

        hello = self._read_response(timeout=2.0)
        if hello is None:
            print(f"[bridge] No hello response from {self._pipe_path} — stale pipe?", file=sys.stderr)
            self._cleanup()
            return False
        print(f"[bridge] Connected to LyX via {self._pipe_path} — {hello}", file=sys.stderr)

        self._connected = True
        self._reconnect_timer.stop()
        self._poll_timer.start()
        self.connection_changed.emit(True)
        return True

    def _raw_send(self, msg: str) -> bool:
        if self._in_fd is None:
            return False
        try:
            os.write(self._in_fd, msg.encode("utf-8"))
            return True
        except OSError:
            self._mark_disconnected()
            return False

    def _read_response(self, timeout: float | None = None) -> str | None:
        if self._out_fd is None:
            return None
        if timeout is None:
            timeout = self.READ_TIMEOUT
        try:
            ready, _, _ = select.select([self._out_fd], [], [], timeout)
            if not ready:
                return None
            data = os.read(self._out_fd, 8192)
            if not data:
                # EOF — LyX closed its end (e.g. during a save)
                self._mark_disconnected()
                return None
            return data.decode("utf-8", errors="replace").strip()
        except OSError:
            self._mark_disconnected()
            return None

    @staticmethod
    def _parse_info(response: str) -> str | None:
        """Extract the data field from an ``INFO:client:func:data`` line."""
        for line in response.split("\n"):
            line = line.strip()
            if line.startswith("INFO:"):
                parts = line.split(":", 3)
                if len(parts) >= 4:
                    return parts[3]
        return None

    def _mark_disconnected(self):
        """Handle a broken pipe: clean up and start reconnecting."""
        if not self._connected:
            return
        self._connected = False
        self._poll_timer.stop()
        self._cleanup()
        self.connection_changed.emit(False)
        self._reconnect_timer.start()

    def _drain_pipe(self):
        """Read and discard any stale responses sitting in the pipe."""
        if self._out_fd is None:
            return
        while True:
            try:
                ready, _, _ = select.select([self._out_fd], [], [], 0.05)
                if not ready:
                    break
                data = os.read(self._out_fd, 8192)
                if not data:
                    break
                print(f"[bridge] drained: {data!r}", file=sys.stderr)
            except OSError:
                break

    def _cleanup(self):
        for fd in (self._in_fd, self._out_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._in_fd = None
        self._out_fd = None

    def _poll(self):
        """Periodic check: detect if LyX switched to a different file or layout."""
        name = self.get_filename()
        if name is not None and name != self._current_filename:
            self._current_filename = name
            self.filename_changed.emit(name)

        layout = self.get_layout()
        if layout is not None and layout != self._current_layout:
            self._current_layout = layout
            self.layout_changed.emit(layout)
