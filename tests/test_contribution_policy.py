"""Exercises the contribution text policy without embedding credits."""

import pytest

from scripts.check_policy import duplicate_paragraph_errors, has_attribution


@pytest.mark.parametrize(
    "text",
    [
        "Generated" + " with " + "Claude",
        "Co-Authored-By:" + " Codex <bot@example.com>",
        "This PR was" + " generated with Release Please.",
        chr(0x1F916) + " Created releases:",
    ],
)
def test_rejects_authorship_credits(text):
    assert has_attribution(text)


@pytest.mark.parametrize(
    "text",
    [
        "Run Claude Code and Codex in separate worktrees.",
        "The CLI uses the existing native authentication.",
        "Copyright 2026 Suneel Kaushik S",
        "Co-Authored-By: Example Maintainer <human@example.com>",
    ],
)
def test_preserves_product_documentation_and_required_notices(text):
    assert not has_attribution(text)


PARAGRAPH = (
    "Native Windows is not supported. The wheel installs there, but every "
    "command exits with status 2 and a line pointing to WSL2, because the "
    "runtime relies on POSIX primitives that native Windows does not "
    "provide."
)


def test_duplicate_paragraph_is_reported(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text(f"{PARAGRAPH}\n\n{PARAGRAPH}\n")
    errors = duplicate_paragraph_errors(tmp_path)
    assert len(errors) == 1
    assert "docs/x.md" in errors[0]
    assert "lines 1, 3" in errors[0]
    assert "Native Windows is not supported" in errors[0]


def test_repeated_fenced_code_block_is_not_reported(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    block = "```\n" + PARAGRAPH + "\n```\n"
    (docs / "x.md").write_text(f"{block}\n{block}")
    assert duplicate_paragraph_errors(tmp_path) == []


def test_clean_file_has_no_errors(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text(f"{PARAGRAPH}\n\nSomething else entirely.\n")
    assert duplicate_paragraph_errors(tmp_path) == []


def test_changelog_duplicates_are_ignored(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "CHANGELOG.md").write_text(f"{PARAGRAPH}\n\n{PARAGRAPH}\n")
    assert duplicate_paragraph_errors(tmp_path) == []
