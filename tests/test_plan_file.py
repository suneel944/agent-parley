"""Checks the work-order plan file, its apply, its diff and its tree."""

import json
import sys

import pytest

from agent_parley import cli, issues, plan, views
from agent_parley.state import BridgeError

PLAN = """
[plan]
name = "Parser rewrite"

[dependencies]
"42" = ["17"]
"43" = ["17"]
"44" = ["42", "43"]

[groups]
parallel = ["42", "43"]
"""


def written(tmp_path, text, name="plan.toml"):
    """Writes one plan file and returns its path."""
    path = tmp_path / name
    path.write_text(text)
    return path


def test_a_plan_records_the_edges_issue_block_records(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    version = bridge.work_plan(repo, "apply", written(repo.parent, PLAN))
    assert version["name"] == "Parser rewrite"
    assert version["by"] == "operator"
    assert len(version["digest"]) == 64
    ledger = issues.snapshot(directory)["issues"]
    assert ledger["42"]["blocked_by"] == ["17"]
    assert ledger["44"]["blocked_by"] == ["42", "43"]
    assert ledger["42"]["owner"] is None


def test_applying_a_plan_claims_nothing_and_assigns_nobody(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    bridge.work_plan(repo, "apply", written(repo.parent, PLAN))
    ledger = issues.snapshot(directory)["issues"]
    assert {record["owner"] for record in ledger.values()} == {None}
    assert all(record["offer"] is None for record in ledger.values())


def test_a_second_apply_adds_nothing_further(bridge, repo, paired):
    path = written(repo.parent, PLAN)
    bridge.work_plan(repo, "apply", path)
    again = bridge.work_plan(repo, "apply", path)
    assert again["added"] == []
    assert bridge.work_plan(repo, "show")["versions"] == 2


def test_a_diff_reports_what_an_apply_would_add(bridge, repo, paired):
    path = written(repo.parent, PLAN)
    reported = bridge.work_plan(repo, "diff", path)
    assert reported["add"] == [
        ("42", "17"),
        ("43", "17"),
        ("44", "42"),
        ("44", "43"),
    ]
    assert reported["unlisted"] == []
    bridge.work_plan(repo, "apply", path)
    assert bridge.work_plan(repo, "diff", path)["add"] == []


def test_an_edge_recorded_by_hand_is_reported_as_such(bridge, repo, paired):
    lane = paired["lanes"]["claude"]
    bridge.work_plan(repo, "apply", written(repo.parent, PLAN))
    bridge.issue(lane, "claim", "50")
    bridge.issue(lane, "block", "50", on="17")
    applied = bridge.work_plan(repo, "show")
    assert applied["unplanned"] == [("50", "17")]
    assert "recorded by hand: #50 waits on #17" in plan.render(applied)


def test_a_tree_shows_owners_under_the_issues_they_wait_on(
    bridge, repo, paired
):
    lane = paired["lanes"]["claude"]
    bridge.work_plan(repo, "apply", written(repo.parent, PLAN))
    bridge.issue(lane, "claim", "42")
    tree = plan.render(bridge.work_plan(repo, "show")).splitlines()
    assert tree[0].startswith("Parser rewrite (")
    assert tree[1] == "  #17: unclaimed"
    assert tree[2] == "    #42: claude"
    assert "    #43: unclaimed" in tree
    assert "  group parallel: #42, #43" in tree


def test_a_plan_is_reported_as_one_document(bridge, repo, paired):
    bridge.work_plan(repo, "apply", written(repo.parent, PLAN))
    document = json.loads(
        views.render("plan", views.work_plan(bridge.work_plan(repo, "show")))
    )
    assert document["kind"] == "plan"
    assert document["plan"] == "Parser rewrite"
    assert document["groups"] == [
        {"group": "parallel", "issues": ["42", "43"], "ready": False}
    ]
    assert {entry["issue"] for entry in document["issues"]} == {
        "17",
        "42",
        "43",
        "44",
    }


def test_an_unapplied_project_reports_no_plan(bridge, repo, paired):
    applied = bridge.work_plan(repo, "show")
    assert applied["plan"] == ""
    assert plan.render(applied) == "No plan applied."


def test_a_cycle_is_refused_before_any_edge_is_written(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    path = written(
        repo.parent,
        '[dependencies]\n"42" = ["43"]\n"43" = ["42"]\n',
        "cycle.toml",
    )
    with pytest.raises(BridgeError, match="cycle: #42, #43"):
        bridge.work_plan(repo, "apply", path)
    assert issues.snapshot(directory)["issues"] == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[dependencies]\n"42" = ["42"]\n', "cannot wait on itself"),
        ('[dependencies]\n"x" = ["17"]\n', "positive number"),
        ('[dependencies]\n"42" = "17"\n', "at most"),
        ("[unknown]\nvalue = 1\n", "only \\[plan\\]"),
        ("not toml at all\n", "not valid TOML"),
    ],
)
def test_an_unusable_plan_is_refused_by_reason(
    bridge, repo, paired, text, message
):
    path = written(repo.parent, text, "broken.toml")
    with pytest.raises(BridgeError, match=message):
        bridge.work_plan(repo, "apply", path)


def test_the_command_line_applies_shows_and_compares(
    bridge, repo, paired, capsys, monkeypatch
):
    path = written(repo.parent, PLAN)

    def run(*arguments):
        """Runs one CLI invocation and returns its standard output."""
        monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
        assert cli.main() == 0
        return capsys.readouterr().out

    assert "Applied plan Parser rewrite" in run(
        "plan", "apply", str(path), "--repo", str(repo)
    )
    assert "#44: unclaimed" in run("plan", "show", "--repo", str(repo))
    reported = json.loads(
        run("plan", "diff", str(path), "--repo", str(repo), "--json")
    )
    assert reported["kind"] == "plan_diff"
    assert reported["add"] == []
