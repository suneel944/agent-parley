"""Checks release-note extraction across maintained changelog formats."""

import pytest

from scripts.release_artifacts import release_notes


@pytest.mark.parametrize(
    "heading",
    [
        "## [0.3.1] - 2026-09-05",
        "## [0.3.1](https://github.com/example/compare/v0.3.0...v0.3.1) "
        "(2026-09-05)",
    ],
)
def test_extracts_only_requested_version(heading):
    text = f"# Changelog\n\n{heading}\n\n### Fixes\n\n- Fixed.\n\n"
    text += "## [0.3.0] - 2026-09-04\n\nOlder.\n"
    assert release_notes(text, "0.3.1") == (
        "## What's Changed\n\n### Fixes\n\n- Fixed.\n"
    )


@pytest.mark.parametrize(
    "text",
    [
        "## [0.3.10] - 2026-09-05\nDifferent version.",
        "## [0.3.1] - 2026-09-05\n\n## [0.3.0] - 2026-09-04\nOlder.",
    ],
)
def test_rejects_missing_or_empty_release_notes(text):
    with pytest.raises(ValueError):
        release_notes(text, "0.3.1")


def test_overview_and_full_changelog_wrap_the_generated_section():
    text = "# Changelog\n\n## [0.3.1] - 2026-09-05\n\n### Fixes\n\n- Fixed.\n"
    assert release_notes(
        text, "0.3.1", "\nWhat this is.\n\n", "https://example.com/repo/"
    ) == (
        "## What's Changed\n\nWhat this is.\n\n### Fixes\n\n- Fixed.\n\n"
        "**Full Changelog**: https://example.com/repo/commits/v0.3.1\n"
    )
    assert release_notes(text, "0.3.1", "   \n", "  ") == release_notes(
        text, "0.3.1"
    )
