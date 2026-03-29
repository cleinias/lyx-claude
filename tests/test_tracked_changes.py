"""Tests for tracked-change support in edits.py."""

import time
from pathlib import Path

from lyx_claude.edits import (
    CLAUDE_AUTHOR_ID,
    CLAUDE_AUTHOR_NAME,
    EditProposal,
    _bernstein_hash,
    apply_all_tracked,
    apply_tracked_edit,
    ensure_tracking_header,
    strip_change_markers,
)

# Minimal valid .lyx file for testing
SAMPLE_LYX = """\
#LyX 2.3 created this file. For more info see http://www.lyx.org/
\\lyxformat 544
\\begin_document
\\begin_header
\\tracking_changes false
\\output_changes false
\\end_header

\\begin_body

\\begin_layout Standard
This is a test paragraph with some sample text.
\\end_layout

\\begin_layout Standard
This is a second paragraph.
\\end_layout

\\end_body
\\end_document
"""


def test_bernstein_hash():
    """Hash should match LyX's Author.cpp Bernstein hash."""
    # The hash for "Claude Assistant" with empty email
    h = _bernstein_hash("Claude Assistant", "")
    assert isinstance(h, int)
    assert h == CLAUDE_AUTHOR_ID
    # Verify it's a reasonable int32 value
    assert -2**31 <= h < 2**31


def test_ensure_tracking_header_enables_tracking():
    result = ensure_tracking_header(SAMPLE_LYX)
    assert "\\tracking_changes true" in result
    assert "\\output_changes true" in result
    assert "\\tracking_changes false" not in result
    assert "\\output_changes false" not in result


def test_ensure_tracking_header_adds_author():
    result = ensure_tracking_header(SAMPLE_LYX)
    expected = f'\\author {CLAUDE_AUTHOR_ID} "{CLAUDE_AUTHOR_NAME}"'
    assert expected in result
    # Author should be before \end_header
    author_pos = result.index(expected)
    header_pos = result.index("\\end_header")
    assert author_pos < header_pos


def test_ensure_tracking_header_idempotent():
    """Calling ensure_tracking_header twice should not duplicate the author."""
    result = ensure_tracking_header(SAMPLE_LYX)
    result2 = ensure_tracking_header(result)
    author_line = f'\\author {CLAUDE_AUTHOR_ID} "{CLAUDE_AUTHOR_NAME}"'
    assert result2.count(author_line) == 1
    assert result2.count("\\tracking_changes true") == 1


def test_ensure_tracking_header_preserves_existing_true():
    """If tracking is already enabled, don't break it."""
    content = SAMPLE_LYX.replace("\\tracking_changes false", "\\tracking_changes true")
    content = content.replace("\\output_changes false", "\\output_changes true")
    result = ensure_tracking_header(content)
    assert result.count("\\tracking_changes true") == 1
    assert result.count("\\output_changes true") == 1


def test_apply_tracked_edit_replacement():
    content = ensure_tracking_header(SAMPLE_LYX)
    old = "some sample text"
    new = "some revised text"
    result, err = apply_tracked_edit(content, old, new, CLAUDE_AUTHOR_ID)
    assert err is None
    assert f"\\change_deleted {CLAUDE_AUTHOR_ID}" in result
    assert f"\\change_inserted {CLAUDE_AUTHOR_ID}" in result
    assert "\\change_unchanged" in result
    assert "some sample text" in result  # old text still present (marked deleted)
    assert "some revised text" in result  # new text present (marked inserted)


def test_apply_tracked_edit_pure_deletion():
    content = ensure_tracking_header(SAMPLE_LYX)
    old = "some sample text"
    new = ""
    result, err = apply_tracked_edit(content, old, new, CLAUDE_AUTHOR_ID)
    assert err is None
    assert f"\\change_deleted {CLAUDE_AUTHOR_ID}" in result
    assert "\\change_inserted" not in result
    assert "\\change_unchanged" in result


def test_apply_tracked_edit_pure_insertion():
    content = ensure_tracking_header(SAMPLE_LYX)
    old = "with "
    new = "with some extra words and "
    result, err = apply_tracked_edit(content, old, new, CLAUDE_AUTHOR_ID)
    assert err is None
    # Both deleted (original "with ") and inserted (new text) should appear
    assert f"\\change_deleted {CLAUDE_AUTHOR_ID}" in result
    assert f"\\change_inserted {CLAUDE_AUTHOR_ID}" in result


def test_apply_tracked_edit_not_found():
    content = ensure_tracking_header(SAMPLE_LYX)
    result, err = apply_tracked_edit(content, "nonexistent text", "replacement", CLAUDE_AUTHOR_ID)
    assert err is not None
    assert "not found" in err


def test_apply_tracked_edit_crosses_paragraph():
    content = ensure_tracking_header(SAMPLE_LYX)
    # This old text spans across \end_layout and \begin_layout
    old = "sample text.\n\\end_layout\n\n\\begin_layout Standard\nThis is a second"
    result, err = apply_tracked_edit(content, old, "replacement", CLAUDE_AUTHOR_ID)
    assert err is not None
    assert "paragraph boundary" in err


def test_apply_tracked_edit_ambiguous():
    content = "hello world\nhello world\n"
    result, err = apply_tracked_edit(content, "hello world", "bye", CLAUDE_AUTHOR_ID)
    assert err is not None
    assert "2 times" in err


def test_apply_all_tracked(tmp_path):
    lyx_file = tmp_path / "test.lyx"
    lyx_file.write_text(SAMPLE_LYX, encoding="utf-8")

    proposals = [
        EditProposal(file_path="test.lyx", old_text="sample text", new_text="revised text"),
        EditProposal(file_path="test.lyx", old_text="second paragraph", new_text="second section"),
    ]

    n_applied, errors = apply_all_tracked(lyx_file, proposals)
    assert n_applied == 2
    assert errors == []

    result = lyx_file.read_text(encoding="utf-8")
    assert "\\tracking_changes true" in result
    assert result.count(f"\\change_deleted {CLAUDE_AUTHOR_ID}") == 2
    assert result.count(f"\\change_inserted {CLAUDE_AUTHOR_ID}") == 2
    assert result.count("\\change_unchanged") == 2


def test_apply_all_tracked_partial_failure(tmp_path):
    lyx_file = tmp_path / "test.lyx"
    lyx_file.write_text(SAMPLE_LYX, encoding="utf-8")

    proposals = [
        EditProposal(file_path="test.lyx", old_text="sample text", new_text="revised text"),
        EditProposal(file_path="test.lyx", old_text="nonexistent", new_text="whatever"),
    ]

    n_applied, errors = apply_all_tracked(lyx_file, proposals)
    assert n_applied == 1
    assert len(errors) == 1
    assert "Edit 2" in errors[0]


def test_strip_change_markers():
    content = (
        "\\begin_layout Standard\n"
        "\\change_deleted 276206893 1711720800\n"
        "old text\n"
        "\\change_inserted 276206893 1711720800\n"
        "new text\n"
        "\\change_unchanged\n"
        "more text\n"
        "\\end_layout\n"
    )
    result = strip_change_markers(content)
    assert "\\change_deleted" not in result
    assert "\\change_inserted" not in result
    assert "\\change_unchanged" not in result
    assert "old text" in result
    assert "new text" in result
    assert "more text" in result


def test_strip_change_markers_preserves_normal_content():
    """Stripping should not affect content without change markers."""
    result = strip_change_markers(SAMPLE_LYX)
    # Should be essentially the same (maybe minor whitespace differences)
    assert "\\begin_layout Standard" in result
    assert "sample text" in result
    assert "second paragraph" in result


def test_roundtrip_apply_then_strip(tmp_path):
    """After applying tracked changes, stripping should give clean content."""
    lyx_file = tmp_path / "test.lyx"
    lyx_file.write_text(SAMPLE_LYX, encoding="utf-8")

    proposals = [
        EditProposal(file_path="test.lyx", old_text="sample text", new_text="revised text"),
    ]
    apply_all_tracked(lyx_file, proposals)
    result = lyx_file.read_text(encoding="utf-8")

    # Should have markers
    assert "\\change_deleted" in result

    # After stripping, should be clean
    stripped = strip_change_markers(result)
    assert "\\change_deleted" not in stripped
    assert "\\change_inserted" not in stripped
