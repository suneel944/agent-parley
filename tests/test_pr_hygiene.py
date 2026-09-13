"""Exercises ownership and issue validation at the GitHub metadata boundary."""

import pytest

from scripts.check_pr_hygiene import (
    issue_numbers,
    validate,
    validate_commit_references,
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


@pytest.mark.parametrize(
    "keyword",
    [
        "ref",
        "refs",
        "fix",
        "fixes",
        "close",
        "closed",
        "closes",
        "resolve",
        "resolved",
        "resolves",
    ],
)
def test_issue_reference_keywords_match_release_accounting(keyword):
    assert issue_numbers(f"{keyword} #7; ReFs #0") == {7}


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


def commit_messages(*messages):
    return [{"commit": {"message": message}} for message in messages]


@pytest.mark.parametrize(
    "title",
    [
        "feat: add coordination",
        "fix(runtime): preserve references",
        "perf(store)!: change persistence",
    ],
)
def test_releasing_prs_preserve_references_across_multiple_commits(title):
    pr = metadata()
    pr.update(
        title=title,
        body="Ref #7\nFixes #8\nClosed #9\nRESOLVED #10",
    )
    commits = commit_messages(
        "feat: first part\n\nRefs #7\nFix #8",
        "fix: second part\n\nCloses #9\nResolve #10\nRefs #7",
    )
    assert validate_commit_references(pr, commits) == []


@pytest.mark.parametrize(
    ("body", "messages"),
    [
        ("Refs #7\nRefs #8", ["feat: partial\n\nRefs #7"]),
        ("Refs #7", ["feat: extra\n\nRefs #7\nRefs #8"]),
    ],
    ids=["missing", "extra"],
)
def test_releasing_prs_reject_different_reference_sets(body, messages):
    pr = metadata()
    pr.update(title="feat: preserve issue accounting", body=body)
    assert validate_commit_references(pr, commit_messages(*messages)) == [
        "Preserve exactly the validated PR issue references in commit "
        "messages using Refs #N or a closing keyword."
    ]


@pytest.mark.parametrize(
    "title",
    [
        "docs: explain references",
        "chore: maintain references",
        "ci: validate references",
    ],
)
def test_nonreleasing_prs_do_not_require_commit_references(title):
    pr = metadata()
    pr.update(title=title, body="Refs #7")
    assert (
        validate_commit_references(pr, commit_messages("No references")) == []
    )
