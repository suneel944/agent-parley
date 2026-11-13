"""Exercises issue template and metadata validation at the GitHub boundary."""

import pytest

from scripts.check_issue_hygiene import template_rules, validate

BUG = (
    "### Version and environment\n0.16.0\n### Reproduction\n1. Run it.\n"
    "### Expected and actual behavior\nIt fails."
)
FEATURE = (
    "### What problem does this solve?\nGap.\n"
    "### Proposed behavior and alternatives\nAdd it.\n"
    "### Boundaries and acceptance criteria\nTests pass."
)


def issue(title="bug: a lane stalls", body=BUG, **fields):
    record = {
        "title": title,
        "body": body,
        "assignees": [{"login": "owner"}],
        "labels": [{"name": "bug"}],
        "milestone": {"number": 1},
    }
    record.update(fields)
    return record


def test_templates_supply_prefixes_and_required_sections():
    rules = template_rules()
    assert rules["bug: "] == [
        "Version and environment",
        "Reproduction",
        "Expected and actual behavior",
    ]
    assert rules["feature: "] == [
        "What problem does this solve?",
        "Proposed behavior and alternatives",
    ]


@pytest.mark.parametrize(
    ("title", "body"),
    [("bug: a lane stalls", BUG), ("feature: report merges", FEATURE)],
)
def test_compliant_issues_pass(title, body):
    assert validate(issue(title, body), template_rules()) == []


@pytest.mark.parametrize(
    "title", ["fix: a lane stalls", "A lane stalls", "bug: ", "bug:x"]
)
def test_title_needs_a_template_prefix_and_text(title):
    errors = validate(issue(title), template_rules())
    assert errors == [
        "Start the title with 'bug: ' or 'feature: ' and describe the issue."
    ]


def test_missing_required_section_is_named():
    body = BUG.replace("### Reproduction", "### What happened")
    assert validate(issue(body=body), template_rules()) == [
        "Include the issue template section: ### Reproduction"
    ]


def test_sections_come_from_the_matching_template():
    errors = validate(issue("feature: report merges", BUG), template_rules())
    assert len(errors) == 2
    assert all("template section" in error for error in errors)


def test_missing_owner_label_and_milestone_fail():
    bare = issue(assignees=[], labels=[{"name": "duplicate"}], milestone=None)
    assert validate(bare, template_rules()) == [
        "Assign at least one owner.",
        "Add a change-type label.",
        "Add the issue to a milestone.",
    ]


def test_empty_body_lists_every_section():
    assert len(validate(issue(body=None), template_rules())) == 3
