"""Edit proposal parsing and application for the proposal-based workflow."""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Regex to extract <proposed-edit> blocks from Claude's response.
# Very tolerant: optional whitespace everywhere, DOTALL for multiline content.
_PROPOSAL_RE = re.compile(
    r'<proposed-edit\s+file\s*=\s*"(?P<file>[^"]+)"\s*>\s*'
    r"<old>\s*(?P<old>.*?)\s*</old>\s*"
    r"<new>\s*(?P<new>.*?)\s*</new>\s*"
    r"</proposed-edit>",
    re.DOTALL,
)


@dataclass
class EditProposal:
    """A single proposed edit extracted from Claude's response."""

    file_path: str
    old_text: str
    new_text: str
    status: str = field(default="pending")  # pending | accepted | rejected


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences that might wrap proposed-edit blocks."""
    # Strip ```xml ... ``` or ``` ... ``` wrappers around our tags
    # Handle both individual fences per block and one large fence around all blocks
    text = re.sub(r"```(?:xml|lyx|text|html)?\s*\n\s*(<proposed-edit)", r"\1", text)
    text = re.sub(r"(</proposed-edit>)\s*\n\s*```", r"\1", text)
    return text


def parse_proposals(text: str) -> list[EditProposal]:
    """Parse all <proposed-edit> blocks from response text."""
    text = _strip_code_fences(text)
    proposals = []
    for m in _PROPOSAL_RE.finditer(text):
        old = m.group("old").strip("\n")
        new = m.group("new").strip("\n")
        proposals.append(
            EditProposal(
                file_path=m.group("file").strip(),
                old_text=old,
                new_text=new,
            )
        )

    # Debug: if tags exist but regex failed, dump context to stderr
    if not proposals and "<proposed-edit" in text:
        print("[edits.py] WARNING: <proposed-edit> found but regex didn't match.", file=sys.stderr)
        # Show text around the first tag for debugging
        idx = text.index("<proposed-edit")
        snippet = text[max(0, idx - 20):idx + 200]
        print(f"[edits.py] Snippet: {snippet!r}", file=sys.stderr)

    return proposals


# Matches common LyX markup commands — if either old or new text contains
# these, the edit is NOT plain text and must go through the file-write path.
_LYX_MARKUP_RE = re.compile(
    r"\\(?:begin_inset|end_inset|begin_layout|end_layout|emph |lang |"
    r"begin_deeper|end_deeper|backslash|labelwidthstring|paragraph_spacing|"
    r"align |family |series |shape |size |bar |strikeout |"
    r"begin_body|end_body|begin_header|end_header)"
)


def is_plain_text_edit(old_text: str, new_text: str) -> bool:
    """Return True if neither old nor new text contains LyX markup.

    Plain-text edits can be applied via LyX's word-replace LFUN, which
    preserves the undo stack.
    """
    return not _LYX_MARKUP_RE.search(old_text) and not _LYX_MARKUP_RE.search(new_text)


def collapse_lyx_wrapping(text: str) -> str:
    """Collapse LyX's hard line wrapping into flowing text.

    Single newlines are joined into spaces (LyX wrapping artifacts).
    Double newlines (paragraph breaks) are preserved.
    """
    # Split on paragraph breaks (two or more newlines)
    paragraphs = re.split(r"\n[ \t]*\n", text)
    collapsed = []
    for para in paragraphs:
        # Join single newlines within a paragraph
        collapsed.append(" ".join(para.split()))
    return "\n\n".join(collapsed)


def _build_flex_pattern(old_text: str) -> str:
    """Build a regex pattern from old_text that handles LyX line wrapping.

    LyX hard-wraps lines at ~80 columns, inserting newlines at arbitrary
    positions — even mid-word (e.g. "historica\\nl" for "historical").
    This builds a pattern where ``\\s*`` is allowed between any adjacent
    non-whitespace characters within a paragraph, while paragraph breaks
    (``\\n\\n``) are preserved as mandatory boundaries.
    """
    # Split on paragraph breaks (2+ newlines, possibly with spaces/tabs between)
    paragraphs = re.split(r"\n[ \t]*\n", old_text)

    para_patterns = []
    for para in paragraphs:
        chars = [ch for ch in para if not ch.isspace()]
        if not chars:
            continue
        # Each char escaped, with \s* between them to absorb arbitrary wrapping
        para_patterns.append(r"\s*".join(re.escape(ch) for ch in chars))

    # Paragraph breaks: require at least two newlines
    return r"\s*\n\s*\n\s*".join(para_patterns)


def apply_edit(
    project_root: Path, file_path: str, old_text: str, new_text: str
) -> str | None:
    """Apply a single search-and-replace edit to a file on disk.

    Returns None on success, or an error message string on failure.
    Falls back to whitespace-flexible matching when exact match fails,
    to handle LyX's arbitrary line wrapping.
    """
    full_path = project_root / file_path
    if not full_path.exists():
        return f"file not found: {file_path}"

    content = full_path.read_text(encoding="utf-8")

    # Fast path: exact match
    if old_text in content:
        if content.count(old_text) != 1:
            return f"old text matches {content.count(old_text)} times (must be unique)"
        new_content = content.replace(old_text, new_text, 1)
        full_path.write_text(new_content, encoding="utf-8")
        return None

    # Fallback: flexible whitespace matching (handles LyX line wrapping)
    # Skip for very large old_text to avoid regex performance issues
    if len(old_text) > 10_000:
        return "old text not found (exact match failed; too large for flexible match)"

    pattern = _build_flex_pattern(old_text)
    try:
        matches = list(re.finditer(pattern, content))
    except re.error as e:
        return f"regex error in flexible match: {e}"

    if len(matches) == 0:
        return "old text not found (even with flexible whitespace matching)"
    if len(matches) > 1:
        return f"flexible match found {len(matches)} locations (must be unique — add more context)"

    # Replace the matched span with new_text
    m = matches[0]
    new_content = content[: m.start()] + new_text + content[m.end() :]
    full_path.write_text(new_content, encoding="utf-8")
    print(
        f"[edits.py] Flexible match applied to {file_path} "
        f"(span {m.start()}:{m.end()}, {m.end()-m.start()} chars replaced)",
        file=sys.stderr,
    )
    return None
