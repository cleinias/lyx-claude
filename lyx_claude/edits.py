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


def apply_edit(project_root: Path, file_path: str, old_text: str, new_text: str) -> bool:
    """Apply a single search-and-replace edit to a file on disk.

    Returns True on success, False if old_text was not found or file missing.
    """
    full_path = project_root / file_path
    if not full_path.exists():
        return False

    content = full_path.read_text(encoding="utf-8")
    if old_text not in content:
        return False

    # Ensure the match is unique
    if content.count(old_text) != 1:
        return False

    new_content = content.replace(old_text, new_text, 1)
    full_path.write_text(new_content, encoding="utf-8")
    return True
