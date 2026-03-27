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
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


class LyXBridge(QObject):
    """Communicates with a running LyX instance via the server pipe protocol."""

    connection_changed = Signal(bool)   # True = connected, False = disconnected
    filename_changed = Signal(str)      # emitted when LyX's active file changes

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

        # Say hello
        if not self._raw_send(f"LYXSRV:{self.CLIENT_NAME}:hello\n"):
            self._cleanup()
            return False

        # Best-effort read of the hello response
        self._read_response(timeout=1.0)

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
        """Periodic check: detect if LyX switched to a different file."""
        name = self.get_filename()
        if name is not None and name != self._current_filename:
            self._current_filename = name
            self.filename_changed.emit(name)
