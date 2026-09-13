"""Exercises ownership and issue validation at the GitHub metadata boundary."""

import pytest

from scripts.check_pr_hygiene import (
    issue_numbers,
    validate,
    validate_repository,
)


def metadata():
    return {
        "title": "ci: enforce pull-request metadata",
        "assignees": [{"login": "owner"}],
        "labels": [{"name": "ci"}],
        "milestone": {"number": 1},
        "user": {"type": "User"},
        "body": "## Problem and result\nRefs #7\n## Verification\n"
        "make check\n## Compatibility and risks\nNone.",
    }


def test_valid_pr_and_case_insensitive_explicit_references():
    assert issue_numbers("Refs #7; fixes #8; RESOLVES #9; random #10") == {
        7,
        8,
        9,
    }
    assert validate(metadata(), [{"number": 7}]) == []


def test_missing_owner_type_and_real_issue_fail():
    pr = metadata()
    pr.update(assignees=[], labels=[{"name": "unrelated"}])
    errors = validate(pr, [{"number": 7, "pull_request": {}}])
    assert len(errors) == 3


def test_issue_milestones_must_match_and_release_needs_one():
    pr = metadata()
    assert validate(pr, [{"number": 7, "milestone": {"number": 2}}])
    pr.update(labels=[{"name": "release"}], milestone=None)
    assert validate(pr, [{"number": 7}])


def test_bot_bodies_preserved_but_metadata_still_required():
    pr = metadata()
    pr.update(user={"type": "Bot"}, body="Dependabot notes. Refs #7")
    assert validate(pr, [{"number": 7}]) == []
    pr["assignees"] = []
    assert validate(pr, [{"number": 7}])


def test_human_pr_requires_template_sections():
    pr = metadata()
    pr["body"] = "Refs #7"
    assert len(validate(pr, [{"number": 7}])) == 3


@pytest.mark.parametrize(
    ("title", "message", "valid"),
    [
        ("PR_TITLE", "PR_BODY", True),
        ("COMMIT_OR_PR_TITLE", "PR_BODY", False),
        ("PR_TITLE", "COMMIT_MESSAGES", False),
        ("PR_TITLE", "BLANK", False),
        (None, None, False),
    ],
)
def test_squash_defaults_preserve_validated_pr_metadata(title, message, valid):
    errors = validate_repository(
        {
            "squash_merge_commit_title": title,
            "squash_merge_commit_message": message,
        }
    )
    assert bool(errors) is not valid
