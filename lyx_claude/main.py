"""Entry point for LyX-Claude sidecar."""

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .lyxbridge import LyXBridge, find_lyxpipe
from .ui import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("LyX-Claude")
    app.setOrganizationName("lyx-claude")

    # Parse CLI args (QApplication already consumed Qt-specific ones)
    parser = argparse.ArgumentParser(description="LyX-Claude sidecar")
    parser.add_argument("path", nargs="?", help="Project directory or .lyx file")
    parser.add_argument(
        "--pipe", default=None,
        help="Path to LyX server pipe (auto-detected if not specified)",
    )
    parser.add_argument(
        "--no-pipe", action="store_true",
        help="Disable LyX pipe integration (file-only mode)",
    )
    args = parser.parse_args(app.arguments()[1:])

    window = MainWindow()

    # Set up LyX pipe bridge (unless disabled)
    if not args.no_pipe:
        pipe_path = args.pipe or find_lyxpipe()
        bridge = LyXBridge(pipe_path, parent=window)
        window.set_bridge(bridge)
        bridge.start()

    if args.path:
        path = Path(args.path).resolve()
        if path.is_dir():
            window.load_project(path)
        elif path.exists() and path.suffix == ".lyx":
            window.load_project(path.parent)
            window.load_file(path)
        else:
            print(f"Warning: {path} is not a directory or .lyx file", file=sys.stderr)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
