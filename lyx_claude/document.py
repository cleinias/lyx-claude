"""Document manager: reads, tracks, and scans .lyx files."""

from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, Signal, QTimer


# Directories to skip when scanning for .lyx files
SKIP_DIRS = {".git", ".venv", "__pycache__", "src", "dosdevices", "pdftomusic-2.0.0d.0"}


class DocumentManager(QObject):
    """Watches and reads .lyx files for context."""

    content_changed = Signal(str, str)  # (filepath, content)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._current_path: Path | None = None
        self._current_content: str = ""
        self._project_root: Path | None = None
        self._watcher.fileChanged.connect(self._on_file_changed)
        # LyX writes to temp file then renames, which can remove the watch.
        # Poll periodically to re-add if needed.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._check_watch)

    def set_project_root(self, path: str | Path):
        """Set the project root directory."""
        self._project_root = Path(path)

    def get_project_root(self) -> Path | None:
        return self._project_root

    def scan_project(self, root: str | Path | None = None) -> list[Path]:
        """Find all .lyx files under the project root, sorted by path."""
        root = Path(root) if root else self._project_root
        if not root or not root.is_dir():
            return []

        lyx_files = []
        for p in sorted(root.rglob("*.lyx")):
            # Skip hidden dirs, venvs, etc.
            parts = p.relative_to(root).parts
            if any(part in SKIP_DIRS or part.startswith(".") for part in parts):
                continue
            # Skip backup/emergency files
            if p.name.endswith((".lyx~", ".lyx.emergency", ".lyx.orig", ".lyx.rej")):
                continue
            lyx_files.append(p)
        return lyx_files

    def open_file(self, path: str | Path) -> str:
        """Open a .lyx file and start watching it. Returns content."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not path.suffix == ".lyx":
            raise ValueError(f"Not a .lyx file: {path}")

        # Remove old watch
        if self._current_path:
            self._watcher.removePath(str(self._current_path))
            self._poll_timer.stop()

        self._current_path = path
        self._current_content = path.read_text(encoding="utf-8")
        self._watcher.addPath(str(path))
        self._poll_timer.start()
        return self._current_content

    def get_content(self) -> str:
        return self._current_content

    def get_path(self) -> Path | None:
        return self._current_path

    def refresh(self) -> str:
        """Re-read the current file from disk."""
        if self._current_path and self._current_path.exists():
            self._current_content = self._current_path.read_text(encoding="utf-8")
            return self._current_content
        return self._current_content

    def _on_file_changed(self, path: str):
        p = Path(path)
        if p.exists():
            self._current_content = p.read_text(encoding="utf-8")
            self.content_changed.emit(str(p), self._current_content)

    def _check_watch(self):
        """Re-add file watch if LyX's save removed it."""
        if self._current_path and self._current_path.exists():
            watched = self._watcher.files()
            if str(self._current_path) not in watched:
                self._watcher.addPath(str(self._current_path))
                self._on_file_changed(str(self._current_path))
