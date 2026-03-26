# LyX-Claude

A standalone chat sidecar that brings Claude AI alongside LyX. It runs as a separate window next to your LyX editor, giving you a conversational AI assistant that can see and edit your `.lyx` files.

## Requirements

- Python 3.11+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated (Max, Pro, or Team plan — no API key needed)
- PySide6 (installed automatically)

## Installation

```bash
git clone https://github.com/<your-username>/lyx-claude.git
cd lyx-claude
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

Launch with your LyX project directory:

```bash
.venv/bin/lyx-claude ~/path/to/your/project
```

Or point at a specific `.lyx` file (its parent directory becomes the project root):

```bash
.venv/bin/lyx-claude ~/path/to/chapter.lyx
```

## Features

- **Project file tree** — left sidebar lists all `.lyx` files; click to switch context
- **Streaming chat** — responses appear as they're generated
- **File watching** — when you save in LyX, the sidecar automatically picks up the changes
- **Full tool access** — Claude can read, edit, and create files in your project
- **Multi-turn conversation** — session persists across messages
- **Cross-platform** — works on Linux and macOS

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Enter | Send message |
| Shift+Enter | Newline in input |
| Ctrl+Shift+O | Open project folder |
| Ctrl+O | Open single .lyx file |
| Ctrl+R | Refresh document context |
| Ctrl+Q | Quit |

## Architecture

The sidecar shells out to the `claude` CLI in `--print` mode with streaming JSON output. This means it uses your existing Claude Code authentication — no separate API key or billing. The chat display is a plain-text widget with batched rendering for speed.

## Roadmap

- **Phase 2**: Structured edit proposals with preview/accept/reject UI
- **Phase 3**: LyX server pipe integration (auto-detect open file, reload after edits)
- **Phase 4**: File change watching, conversation save/load, multiple documents
