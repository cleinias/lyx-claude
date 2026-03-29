# LyX-Claude: IDE Parity Roadmap

Gap analysis and roadmap for bringing lyx-claude closer to the behavior of
Claude integrations in IDEs like PyCharm and VS Code, adapted for a document
editing workflow.

## What lyx-claude already has

- Streaming chat with token/timing display
- Project file tree with click-to-load
- Dual context (raw LyX markup + plain text export) in system prompt
- Edit proposals via `<proposed-edit>` blocks
- **Tracked-change editing** — edits applied as LyX Track Changes
  (`\change_deleted`/`\change_inserted`), reviewed in LyX's native UI
- Flexible whitespace matching for LyX's hard line wrapping
- Selection import from LyX (Wayland-aware workaround via internal cut stack)
- Layout-at-cursor awareness
- File watching with auto-refresh on save
- Multi-turn conversation via `--resume`
- Fallback card UI for edits that cross paragraph boundaries

## Completed (this session)

### Edit apply via tracked changes
- **Was**: Fragile card-based Accept/Reject with filename bug (Claude
  invented filenames). Reviewing diffs in small cards was awkward for prose.
- **Now**: Edits are written directly into the `.lyx` file as tracked changes.
  LyX reloads and shows strikethrough/colored text. User reviews in context
  with full formatting using LyX's Document > Changes UI. Accept All / Reject
  All buttons in sidecar send LFUNs to LyX.
- **Also fixed**: Filename bug — sidecar now uses the real file path from
  DocumentManager, ignoring Claude's `file` attribute entirely.
- **Also fixed**: Context pollution — tracked-change markers are stripped
  before sending document content to Claude.
- This simultaneously addresses the original gap items for **diff viewer**
  and **edit apply reliability**.

## Roadmap

### Priority 1 — High impact, low effort

#### 1. Model selection UI
IDE lets you switch between Sonnet, Opus, Haiku mid-conversation.
lyx-claude hardcodes `--model sonnet`.

**Implementation sketch:**
- Add a dropdown/combo box to the toolbar or status bar (Sonnet / Opus / Haiku)
- Wire it to `ConversationEngine.set_model()`
- Already supported by the engine — just needs UI

#### 2. Slash commands
IDE provides `/clear`, `/compact`, `/model`, `/export`, etc.
lyx-claude has toolbar buttons and keyboard shortcuts but no text commands.

**Minimum set:**
- `/clear` — clear conversation history (already has Ctrl action, just add text command)
- `/model <name>` — switch model
- `/refresh` — refresh document context
- `/export` — dump conversation to a text file

**Implementation sketch:**
- Intercept input text starting with `/` in `_send_message()`
- Dispatch to existing methods or new handlers
- Show help text for `/help`

### Priority 2 — High impact, moderate effort

#### 3. Conversation history and resume UI
IDE stores past conversations, searchable by keyword/time. lyx-claude has
`--resume` but no UI to browse or revisit past sessions.

**Implementation sketch:**
- On each conversation start, log session ID + timestamp + first user message
  to a local JSON file
- Add a "History" panel or dialog listing past sessions
- Double-click to resume a session via `--resume <session_id>`
- Optional: search/filter by text

#### 4. Checkpoints / rewind
Every prompt creates a snapshot of the file. User can rewind to any prior
state. Critical safety net for a writing tool where an accepted edit might
turn out wrong.

**Implementation sketch:**
- Before applying tracked changes, copy the `.lyx` file to a timestamped
  backup (e.g., `.lyx-claude-checkpoints/filename_2024-03-29T10-30-00.lyx`)
- Show a "Rewind" button or `/undo` command that lists checkpoints
- Restoring a checkpoint: copy it back, send `buffer-reload dump`
- Simple and robust — no need for git integration

#### 5. Context compaction (`/compact`)
When context fills up, IDE compresses it with optional focus instructions.
lyx-claude has no way to manage context growth across a long session.

**Implementation sketch:**
- `/compact` sends a special prompt asking Claude to summarize the
  conversation so far, then starts a new session with the summary as
  system context
- Or: use `claude --resume` with a compaction prompt
- Track token usage and show a warning when approaching limits

### Priority 3 — Medium impact, moderate effort

#### 6. @-mention file references
Type `@filename` in chat to attach additional files as context.
Useful for discussing how one chapter relates to another.

**Implementation sketch:**
- Detect `@` followed by a filename in user input
- Look up the file in the project tree
- Append its content (or a summary) to the user message
- Autocomplete from the project file list

#### 7. Plan mode
Claude explores and produces a plan before making changes. User reviews,
annotates, then approves. Good for structural edits like reorganizing a chapter.

**Implementation sketch:**
- `/plan <instruction>` sends the prompt with a system instruction to
  produce a plan document instead of edit blocks
- Display the plan in chat
- User can say "go ahead" to execute, or refine
- Could also work as a two-step: plan → approve → execute

#### 8. Permission modes
Cycle between "ask before each edit" (show tracked changes, wait for
Accept All), "auto-accept edits" (apply and accept immediately), and
"suggest only" (show in chat but don't modify file).

**Implementation sketch:**
- Add a mode selector (toolbar dropdown or `/mode` command)
- In auto-accept mode: after `apply_all_tracked()`, immediately call
  `accept_all_changes()` and skip showing TrackedChangeBar
- In suggest-only mode: show the old EditPanel cards without modifying
  the file

### Priority 4 — Nice to have

#### 9. Conversation export
`/export` dumps the conversation as a text or markdown file.
Useful for keeping research notes from a brainstorming session.

#### 10. Side questions
Quick question that doesn't pollute conversation history.
"What does this Latin phrase mean?" without derailing the editing session.

**Implementation sketch:**
- `/btw <question>` sends a one-shot query (no `--resume`) and displays
  the answer inline
- Does not affect the main session's context

#### 11. Prompt suggestions
After Claude responds, show suggested follow-up prompts as clickable chips.
Tab to accept.

#### 12. Image paste
Paste images into the prompt (Ctrl+V). Could be useful for discussing
diagrams, figures, or page layouts.

#### 13. Multi-file edits
Currently each response's proposals target the active file. Supporting
atomic edits across multiple files (e.g., updating a cross-reference in
two chapters) would require extending `apply_all_tracked()` to handle
multiple file paths.

## Not relevant for LyX

These IDE features don't apply to a document-editing workflow:
- Git worktrees, PR/issue integration
- Terminal output references, shell command execution
- Jupyter notebook integration
- Browser/DevTools integration
- Code diagnostics (linting, type errors)
- MCP servers, plugins, sub-agents
- Code-specific hooks (pre-commit, test runners)
