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

### LyX pipe integration

If LyX is configured with a server pipe (Edit > Preferences > Paths > Server pipe), the sidecar connects automatically and:

- Detects which file is open in LyX and loads it as context
- Tells LyX to reload the buffer after an edit is applied
- Shows connection status in the status bar

To disable pipe integration:

```bash
.venv/bin/lyx-claude --no-pipe ~/path/to/your/project
```

To use a non-default pipe path:

```bash
.venv/bin/lyx-claude --pipe ~/.lyx/lyxpipe ~/path/to/your/project
```

## Features

- **Project file tree** — left sidebar lists all `.lyx` files; click to switch context
- **Streaming chat** — responses appear in real time as they're generated, with elapsed time and token counts
- **Edit proposals** — Claude proposes edits as structured diffs; you preview the old/new text and click Accept or Reject before anything touches disk
- **Whitespace-flexible matching** — edit proposals handle LyX's arbitrary line wrapping (even mid-word breaks) via a regex fallback when exact match fails
- **LyX pipe integration** — auto-detects the active file in LyX, reloads the buffer after edits are applied, and reconnects automatically when LyX saves (which resets the pipe)
- **File watching** — when you save in LyX, the sidecar picks up changes and refreshes Claude's context
- **Read-only tool access** — Claude can read, glob, and grep files in your project but cannot modify them directly; all changes go through the proposal workflow
- **Multi-turn conversation** — session persists across messages

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

```
lyx_claude/
  main.py        — entry point, argument parsing, LyX bridge setup
  ui.py          — PySide6 UI: chat window, file tree, edit proposal cards
  engine.py      — conversation engine (shells out to claude CLI)
  edits.py       — edit proposal parsing, whitespace-flexible matching, file application
  document.py    — file reading, watching, project scanning
  lyxbridge.py   — LyX server pipe protocol (named-pipe I/O, auto-reconnect)
```

The sidecar shells out to the `claude` CLI in `--print` mode with streaming JSON output. This means it uses your existing Claude Code authentication — no separate API key or billing. Claude's tools are restricted to `Read`, `Glob`, and `Grep` (read-only); all file modifications go through a proposal-based workflow where the user explicitly accepts or rejects each change.

The edit proposal system handles LyX's `.lyx` format, which hard-wraps lines at ~80 columns at arbitrary positions (even mid-word). When Claude's proposed old text doesn't exactly match the file due to different wrapping, a whitespace-flexible regex fallback finds the correct span and applies the replacement.

The LyX bridge communicates over named pipes (`lyxpipe.in` / `lyxpipe.out`) using LyX's server protocol. It polls for filename changes and reconnects automatically when LyX closes and reopens the pipes (which happens on every buffer save).
