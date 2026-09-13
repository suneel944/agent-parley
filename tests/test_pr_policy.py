"""Checks project metadata overrides without a live GitHub account."""

import json

import pytest

from agent_parley import cli, roster
from agent_parley.state import BridgeError


def test_unlabelled_repository_can_open_a_plain_request(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "gh", lambda *args: json.dumps({"labels": [], "milestone": None})
    )
    assert cli.hygiene_metadata(tmp_path, ["1"]) == ([], "")
    body = cli.pull_request_body(
        {"summary": "Result", "evidence": "Tests"}, ["1"]
    )
    assert "Result" in body and "Tests" in body and "Refs #1" in body


def test_custom_labels_and_milestone_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "gh",
        lambda *args: json.dumps(
            {"labels": [{"name": "feature"}], "milestone": {"title": args[3]}}
        ),
    )
    assert cli.hygiene_metadata(
        tmp_path,
        ["1", "2"],
        {"change_type_labels": ["feature"], "milestone": "ignore"},
    ) == (["feature"], "")
    with pytest.raises(BridgeError, match="different milestones"):
        cli.hygiene_metadata(tmp_path, ["1", "2"])


@pytest.mark.parametrize(
    "policy",
    [
        None,
        {"unknown": True},
        {"require_label": "yes"},
        {"change_type_labels": "feature"},
        {"milestone": "invent"},
        {"body_template": 42},
    ],
)
def test_policy_rejects_malformed_configuration(policy):
    with pytest.raises(BridgeError):
        roster.pull_request_policy(policy)


def test_repository_template_preserves_report_and_issue_reference():
    body = cli.pull_request_body(
        {"summary": "Result", "evidence": "Verified"},
        ["9"],
        "## Custom checklist\n\n- [ ] Reviewed\n\n$summary",
    )
    assert body.startswith("## Custom checklist")
    assert "Result" in body and "Verified" in body and "Refs #9" in body
