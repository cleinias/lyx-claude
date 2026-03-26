"""Entry point for LyX-Claude sidecar."""

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .ui import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("LyX-Claude")
    app.setOrganizationName("lyx-claude")

    window = MainWindow()

    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).resolve()
        if path.is_dir():
            # Argument is a project directory
            window.load_project(path)
        elif path.exists() and path.suffix == ".lyx":
            # Argument is a single .lyx file — also set its parent as project root
            window.load_project(path.parent)
            window.load_file(path)
        else:
            print(f"Warning: {path} is not a directory or .lyx file", file=sys.stderr)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
