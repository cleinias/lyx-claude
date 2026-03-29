# Plan: Apply Claude's Edits as LyX Tracked Changes

## Context

Currently, when Claude proposes edits, the sidecar parses `<proposed-edit>` XML blocks
and shows old/new cards with per-card Accept/Reject buttons. This has two problems:
1. The filename bug (Claude invents filenames, breaking the apply step)
2. Reviewing diffs in a small card panel is awkward for prose editing

LyX has native Track Changes support (like Word's "Track Changes"). Instead of
the card-based review, we can write Claude's edits directly into the .lyx file as
tracked changes, then reload LyX. The user reviews and accepts/rejects changes
inside LyX itself — in context, with full formatting, using a familiar interface.

## Approach

### Workflow (replaces current Accept/Reject cards)

1. Claude responds with `<proposed-edit>` blocks (unchanged)
2. Sidecar parses proposals (unchanged)
3. **New:** Sidecar applies ALL proposals as tracked changes to the .lyx file:
   - Adds `\author <hash> "Claude Assistant"` to header (if not present)
   - Sets `\tracking_changes true` and `\output_changes true` in header
   - For each proposal: wraps old text in `\change_deleted`, inserts new text with `\change_inserted`
4. Sends `buffer-reload dump` to LyX (reload without confirmation dialog)
5. Shows summary in chat: "Applied N tracked changes — review in LyX"
6. Shows errors for any proposals that couldn't be applied
7. User reviews changes in LyX's Track Changes UI (strikethrough/colored text,
   Navigate Changes, Accept/Reject per-change)
8. Sidecar offers "Accept All" / "Reject All" convenience buttons that send LFUNs

### Tracked change format in .lyx files

For a replacement (old -> new):
```
\change_deleted 584371530 1711720800
old text stays here (marked as deleted)
\change_inserted 584371530 1711720800
new replacement text (marked as inserted)
\change_unchanged
```

For a pure deletion: `\change_deleted` + old text + `\change_unchanged`
For a pure insertion: `\change_inserted` + new text + `\change_unchanged`

Markers go on their own line. Default state at paragraph start is "unchanged".

### Paragraph boundary constraint

Change markers cannot span `\begin_layout`/`\end_layout` boundaries. Edits that
cross paragraph boundaries will fall back to the existing direct-edit approach
(write to file + reload), with a note in chat.

## Files to Modify

### 1. `lyx_claude/edits.py` — Add tracked change functions

New constants:
- `CLAUDE_AUTHOR_NAME = "Claude Assistant"`
- `CLAUDE_AUTHOR_ID` — Bernstein hash of the name (matching LyX's hash algorithm)

New functions:
- **`ensure_tracking_header(content) -> str`** — Modify the .lyx header:
  - Set `\tracking_changes true` (replace existing line)
  - Set `\output_changes true` (replace existing line)
  - Add `\author <CLAUDE_AUTHOR_ID> "Claude Assistant"` before `\end_header` (if not present)

- **`apply_tracked_edit(content, old_text, new_text, author_id) -> (new_content, error)`**
  - Find old_text using existing matching logic (exact match, then flex pattern)
  - Check if matched region crosses `\begin_layout`/`\end_layout` — if so, return error
  - Build replacement: `\change_deleted` + old text + `\change_inserted` + new text + `\change_unchanged`
  - Handle edge cases: pure deletion (empty new_text), pure insertion (empty old_text)
  - Return modified content or error string

- **`apply_all_tracked(filepath, proposals) -> (n_applied, errors: list[str])`**
  - Read file, call `ensure_tracking_header()`, apply each proposal via `apply_tracked_edit()`
  - Write file once at the end (all changes in one write)
  - Return count of successful applies and list of error messages

- **`strip_change_markers(content) -> str`**
  - Remove `\change_inserted ...`, `\change_deleted ...`, `\change_unchanged` lines
  - Remove `\author` lines for non-document authors
  - Purpose: send clean content to Claude so tracked-change markup doesn't confuse it

Keep existing functions (`parse_proposals`, `apply_edit`, `is_plain_text_edit`, etc.)
as fallback for cases tracked changes can't handle.

### 2. `lyx_claude/engine.py` — Update system prompt + clean context

System prompt changes:
- Replace "The sidecar app parses these blocks and shows the user an Accept/Reject panel"
  with "Your edits will be applied as tracked changes in LyX for the user to review"
- Keep the `<proposed-edit>` format specification (we still parse it the same way)
- Add note: if the file contains `\change_*` markers, ignore them when writing `<old>` text
- Remove mention of Accept/Reject panel behavior

Context cleaning:
- In `_build_system()`, call `strip_change_markers()` on document content before
  including in system prompt. This prevents Claude from seeing/matching tracked
  change markup in its old-text searches.

Filename fix:
- Get the real file path from `DocumentManager` (or `LyXBridge.get_filename()`)
  instead of relying on Claude to use the right name. The sidecar already knows
  the file path — use it when applying edits regardless of what Claude puts in
  the `file` attribute.

### 3. `lyx_claude/ui.py` — Replace edit panel with tracked-change workflow

Remove per-card Accept/Reject UI. Replace with:

**On `edits_proposed` signal:**
1. Get the current file path from `DocumentManager` (ignore Claude's file attribute)
2. Call `apply_all_tracked()` to write tracked changes
3. Send `buffer-reload dump` via LyXBridge
4. Show summary in chat:
   - "Applied N tracked changes — review in LyX (Document > Changes)"
   - List any errors
5. Show a small **TrackedChangeBar** widget (replaces EditPanel):
   - "N tracked changes applied" label
   - "Accept All" button -> sends `all-changes-accept` LFUN
   - "Reject All" button -> sends `all-changes-reject` LFUN
   - Auto-hides after user acts

**For proposals that fail tracked-change apply** (cross-paragraph, file not found):
- Fall back to showing the old EditProposalCard with per-card Accept/Reject
- Log the reason in chat

### 4. `lyx_claude/lyxbridge.py` — Add change-tracking LFUNs

New methods:
- **`accept_all_changes() -> bool`** — sends `all-changes-accept` LFUN
- **`reject_all_changes() -> bool`** — sends `all-changes-reject` LFUN
- **`reload_buffer_no_confirm() -> bool`** — sends `buffer-reload dump` (skip confirmation dialog)

## Verification

1. **Unit test the core function**: Create a small .lyx file, apply a tracked edit,
   verify the output has correct `\change_deleted`/`\change_inserted` markers and
   a valid header with author entry.

2. **End-to-end test**:
   - Launch lyx-claude with a test .lyx file open in LyX
   - Ask Claude to make a prose edit
   - Verify the file gets tracked change markers
   - Verify LyX reloads and shows the changes
   - Accept/reject changes in LyX and verify the result

3. **Edge cases to test**:
   - Edit within a single paragraph (happy path)
   - Edit crossing paragraph boundaries (should fall back)
   - Multiple edits in one response
   - File already has tracked changes from a previous round
   - File already has `\tracking_changes true` and existing authors
   - Pure insertion / pure deletion
   - Text with LyX markup (insets, font changes) within the edit region

## Notes

- After applying tracked changes, LyX will have `\tracking_changes true` in the
  running session. The user's own subsequent edits will also be tracked. This is
  intentional — they can toggle it off via Document > Change Tracking if desired.
- The `file` attribute in Claude's `<proposed-edit>` blocks will be ignored in
  favor of the actual file path from DocumentManager. This fixes the filename bug.
- Claude will receive clean content (change markers stripped) to avoid confusion
  when proposing edits on a file that already has tracked changes.
