"""Exercises the contribution text policy without embedding credits."""

import pytest

from scripts.check_policy import has_attribution


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
