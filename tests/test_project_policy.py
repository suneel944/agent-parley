"""Checks the project policy file that gates ready reports and resolves."""

import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, forge, issues, reviews, rules
from agent_parley.cli import git
from agent_parley.state import BridgeError
from tests.test_claim_termination import escalated, evidence, registered

REVIEW = """[[rule]]
name = "large-diff-review"
kind = "review"
lines = 3
"""

REVIEWER = """[[rule]]
name = "migrations-need-codex"
kind = "reviewer"
paths = ["migrations/*"]
lane = "codex"
"""

TEST = """[[rule]]
name = "fix-needs-test"
kind = "test"
title = "fix:"
tests = ["tests/*.py"]
"""


@pytest.fixture
def lane(bridge, paired, monkeypatch):
    """Claims issue 1 for the Claude lane and returns its worktree."""
    monkeypatch.setattr(forge, "issue_text", lambda repo, number: (None, ""))
    worktree = Path(paired["lanes"]["claude"])
    bridge.issue(worktree, "claim", "1")
    return worktree


def adopt(bridge, repo, tmp_path, text):
    """Imports a policy file as the operator from the base checkout."""
    source = tmp_path / "chosen-policy.toml"
    source.write_text(text)
    return rules.install(bridge, repo, source)


def commit(lane, path, lines, subject):
    """Commits one file of the given line count on the lane's branch."""
    target = lane / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"line {n}\n" for n in range(lines)))
    git(lane, "add", path)
    git(lane, "commit", "-m", subject)


def ready(bridge, lane):
    """Files a ready report for the lane's claim."""
    return bridge.report(lane, "ready", "Done.", "", "make check passed")


def state(lane):
    """Reads the claim's execution state."""
    return issues.snapshot(lane.parent)["issues"]["1"]["execution"]["state"]


def returned_round(lane, reviewer):
    """Records a review round on the claim and returns its findings."""
    record = issues.snapshot(lane.parent)["issues"]["1"]
    entry = reviews.request(
        lane.parent,
        builder="claude",
        issue="1",
        claim=record["claim_id"],
        branch="feat/1",
        paths=[],
        candidates=[reviewer],
        providers={},
    )
    reviews.returned(lane.parent, entry["id"], reviewer, "Looks fine.")


def test_no_policy_file_leaves_ready_unchanged(bridge, lane):
    commit(lane, "tests/test_none.py", 1, "fix: no policy")
    commit(lane, "big.txt", 50, "feat: large")
    ready(bridge, lane)
    assert state(lane) == "ready"


def test_review_rule_refuses_a_large_diff_until_a_round_returns(
    bridge, repo, lane, tmp_path
):
    adopt(bridge, repo, tmp_path, REVIEW)
    commit(lane, "small.txt", 3, "feat: small")
    ready(bridge, lane)
    assert state(lane) == "ready"
    commit(lane, "big.txt", 10, "feat: large")
    with pytest.raises(
        BridgeError,
        match="rule large-diff-review failed: 13 changed lines exceed 3",
    ):
        ready(bridge, lane)
    returned_round(lane, "codex")
    ready(bridge, lane)


def test_reviewer_rule_needs_the_named_lane_on_matching_paths(
    bridge, repo, lane, tmp_path
):
    adopt(bridge, repo, tmp_path, REVIEWER)
    commit(lane, "src/app.py", 2, "feat: app")
    ready(bridge, lane)
    commit(lane, "migrations/001.sql", 2, "feat: schema")
    with pytest.raises(
        BridgeError,
        match=r"migrations-need-codex failed: migrations/001.sql match "
        r"migrations/\* but codex returned no review round",
    ):
        ready(bridge, lane)
    returned_round(lane, "codex")
    ready(bridge, lane)
    assert state(lane) == "ready"


def test_test_rule_needs_a_changed_test_file_for_a_fix_title(
    bridge, repo, lane, tmp_path
):
    adopt(bridge, repo, tmp_path, TEST)
    commit(lane, "src/feature.py", 2, "feat: no test needed")
    ready(bridge, lane)
    commit(lane, "src/bug.py", 2, "fix: the bug")
    with pytest.raises(
        BridgeError,
        match=r"fix-needs-test failed: commit 'fix: the bug' starts with "
        r"'fix:' but no changed file matches tests/\*\.py",
    ):
        ready(bridge, lane)
    commit(lane, "tests/test_bug.py", 2, "test: cover the bug")
    ready(bridge, lane)
    assert state(lane) == "ready"


def test_a_resolve_rule_refuses_completion_but_not_release(
    bridge, repo, paired, lane, monkeypatch, tmp_path
):
    registered(bridge, paired)
    adopt(bridge, repo, tmp_path, REVIEW + 'on = ["resolve"]\n')
    commit(lane, "big.txt", 10, "feat: large")
    ready(bridge, lane)
    escalated(bridge, lane, monkeypatch)
    evidence(monkeypatch, "MERGED", time.time())
    with pytest.raises(
        BridgeError, match="Project policy refuses resolve on #1: rule "
    ):
        bridge.issue_resolve(repo, "1")
    assert issues.snapshot(lane.parent)["issues"]["1"]["owner"] == "claude"
    returned_round(lane, "codex")
    assert bridge.issue_resolve(repo, "1")["outcome"] == "complete"


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        (
            REVIEW + "limit = 4\n",
            r"line 5: rule 1 key 'limit' is unknown for a review rule",
        ),
        (
            "[[rule]]\nname = 'x'\nkind = 'review'\n",
            r"line 1: rule 1 key 'lines' is missing",
        ),
        (
            REVIEW + REVIEWER.replace("migrations/*", ""),
            r"line 8: rule 2 key 'paths' must be a list",
        ),
        (
            REVIEW + "on = ['merge']\n",
            r"line 5: rule 1 key 'on' must be a nonempty list of ready",
        ),
        ("[rules]\nname = 'x'\n", r"line 1: key 'rules' is unknown"),
        (REVIEW + "kind = [\n", r"line 5: not valid TOML"),
        (REVIEW + "lines = 4\n", r"line 5: not valid TOML"),
        (REVIEW + REVIEW, r"line 6: rule 2 key 'name' repeats"),
    ],
)
def test_a_malformed_policy_is_refused_with_its_line_and_key(
    bridge, repo, lane, tmp_path, text, refusal
):
    with pytest.raises(BridgeError, match=refusal):
        adopt(bridge, repo, tmp_path, text)
    assert not (lane.parent / rules.FILE_NAME).exists()
    (lane.parent / rules.FILE_NAME).write_text(text)
    with pytest.raises(BridgeError, match=refusal):
        ready(bridge, lane)
    assert state(lane) != "ready"


def test_a_lane_cannot_import_the_policy(bridge, lane, tmp_path):
    with pytest.raises(BridgeError, match="never from an assigned worktree"):
        adopt(bridge, lane, tmp_path, REVIEW)
    assert not (lane.parent / rules.FILE_NAME).exists()


def main(monkeypatch, bridge, *arguments):
    """Runs one CLI command against the test home and returns its status."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    return cli.main()


def snapshot_files(directory):
    """Reads every state file's bytes, to prove nothing changed."""
    return {
        path: path.read_bytes()
        for path in sorted(directory.glob("*"))
        if path.is_file()
    }


def test_policy_check_prints_every_rule_and_changes_nothing(
    bridge, repo, lane, monkeypatch, capsys, tmp_path
):
    check = ("policy", "check", "--repo", str(repo))
    assert main(monkeypatch, bridge, *check) == 0
    assert "No project policy" in capsys.readouterr().out
    source = tmp_path / "chosen.toml"
    source.write_text(REVIEW + REVIEWER + TEST)
    arguments = ("policy", "import", str(source), "--repo", str(repo))
    assert main(monkeypatch, bridge, *arguments) == 0
    assert "Recorded 3 policy rule(s)" in capsys.readouterr().out
    commit(lane, "src/bug.py", 5, "fix: the bug")
    before = snapshot_files(lane.parent)
    assert main(monkeypatch, bridge, *check) == 0
    printed = capsys.readouterr().out
    assert snapshot_files(lane.parent) == before
    assert "#1 held by claude:" in printed
    assert "FAIL large-diff-review (review, on ready): 5 changed" in printed
    assert "pass migrations-need-codex (reviewer, on ready)" in printed
    assert "FAIL fix-needs-test (test, on ready): commit 'fix: the bug'" in (
        printed
    )
