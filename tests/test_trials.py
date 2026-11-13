"""Side-by-side trials of one issue across provider lanes."""

import json
import sys
from pathlib import Path

import pytest

from agent_parley import (
    budgets,
    cli,
    issues,
    lifecycle,
    quota,
    tables,
    trials,
)
from agent_parley.cli import git
from agent_parley.state import BridgeError, write_json


def commit(repo: Path, name: str, content: str = "work\n") -> str:
    """Creates one test commit in a lane and returns its object name."""
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"Add {name}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def quiet_forge(monkeypatch):
    """Keeps every forge call and consumption reading local."""
    monkeypatch.setattr("agent_parley.cli.forge.issue_title", lambda *a: None)
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *a: True)
    readings = {"claude": 100, "codex": 50}

    def reading(home, directory, manifest, name):
        return {
            "participant": name,
            "tokens": readings.get(name),
            "calls": 0,
            "hours": 0.0,
            "session": "s",
            "at": 0.0,
        }

    monkeypatch.setattr(budgets, "claim_reading", reading)
    return readings


def test_two_lane_trial_runs_through_ready_keep_and_release(
    bridge, repo, paired, quiet_forge, capsys, monkeypatch
):
    claude = Path(paired["lanes"]["claude"])
    codex = Path(paired["lanes"]["codex"])
    directory = claude.parent
    base = git(repo, "rev-parse", "HEAD")

    trial = bridge.trial_start(repo, "7", "claude,codex")

    assert trial["base"] == base
    assert sorted(trial["lanes"]) == ["claude", "codex"]
    assert trial["lanes"]["claude"]["provider"] == "claude"
    assert trial["lanes"]["codex"]["provider"] == "codex"
    record = issues.snapshot(directory)["issues"]["7"]
    assert record["owner"] is None
    assert issues.counted_claims({"7": record}, "codex") == ["7"]
    assert "7" not in issues.unclaimed(issues.snapshot(directory))
    with pytest.raises(BridgeError, match="in a trial"):
        bridge.issue(codex, "claim", "7")

    quiet_forge["claude"] = 400
    commit(claude, "a.txt", "one\ntwo\n")
    bridge.report(claude, "ready", "Done", "", "5 passed, 1 failed in 2s")
    quiet_forge["codex"] = 90
    commit(codex, "b.txt", "one\n")
    bridge.report(codex, "ready", "Done", "", "3 passed")

    shown = bridge.trial_show(repo, "7")
    rows = {row["lane"]: row for row in shown["lanes"]}
    assert shown["state"] == trials.RUNNING
    assert rows["claude"]["diff"] == {"files": 1, "added": 2, "deleted": 0}
    assert (rows["claude"]["tests_run"], rows["claude"]["tests_passed"]) == (
        6,
        5,
    )
    assert rows["claude"]["client_tokens"] == 300
    assert rows["codex"]["client_tokens"] == 40
    assert rows["codex"]["findings"] is None
    assert rows["codex"]["ready_seconds"] is not None
    assert "not billed spend" in trials.render(shown)

    project = next(
        item
        for item in bridge.status_snapshot()["projects"]
        if item["root"] == str(repo)
    )
    for record in project["participants"]:
        assert record["trials"] == [7]
        assert "#7(trial)" in tables.status_row(record, ())
    capsys.readouterr()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "trial",
            "show",
            "7",
            "--repo",
            str(repo),
        ],
    )
    cli.main()
    assert "client-reported tokens" in capsys.readouterr().out

    ended = bridge.trial_keep(repo, "7", "codex")

    assert ended["state"] == trials.KEPT
    assert ended["lanes"]["codex"]["outcome"] == trials.KEPT
    assert ended["lanes"]["claude"]["outcome"] == trials.RELEASED
    record = issues.snapshot(directory)["issues"]["7"]
    assert record["owner"] == "codex"
    assert "trial" not in record
    assert record["claim_id"] == ended["lanes"]["codex"]["claim_id"]
    assert lifecycle.state(record)["state"] == lifecycle.READY
    assert issues.counted_claims({"7": record}, "claude") == []
    branch = paired["participants"]["claude"]["branch"]
    assert git(repo, "branch", "--list", branch).strip()
    assert (claude / "a.txt").exists()
    assert bridge.trial_show(repo, "7")["kept"] == "codex"

    outcome = cli.metrics.trials(bridge.home)
    assert outcome["providers"]["codex"]["kept"] == 1
    assert outcome["providers"]["claude"]["released"] == 1
    assert outcome["providers"]["claude"]["client_tokens"] == 300
    assert "client tokens" in cli.metrics.describe_trials(outcome)


def test_trial_refuses_a_lane_parked_on_its_quota(
    bridge, repo, paired, quiet_forge
):
    directory = Path(paired["lanes"]["claude"]).parent
    write_json(
        directory / quota.LEDGER,
        {"parked": {"codex": {"cause": "limit", "until": 2**31}}},
    )

    with pytest.raises(BridgeError, match="parked"):
        bridge.trial_start(repo, "8", "claude,codex")


def test_trial_respects_the_claim_cap(bridge, repo, paired, quiet_forge):
    codex = Path(paired["lanes"]["codex"])
    bridge.issue(codex, "claim", "3")
    bridge.issue(codex, "claim", "5")

    with pytest.raises(BridgeError, match="codex"):
        bridge.trial_start(repo, "9", "claude,codex")
    assert "trial" not in (
        issues.snapshot(codex.parent)["issues"].get("9") or {}
    )


def test_trial_needs_two_lanes_an_unowned_issue_and_the_operator(
    bridge, repo, paired, quiet_forge
):
    codex = Path(paired["lanes"]["codex"])
    with pytest.raises(BridgeError, match="at least 2"):
        bridge.trial_start(repo, "4", "codex,codex")
    with pytest.raises(BridgeError, match="operator"):
        bridge.trial_start(codex, "4", "claude,codex")
    bridge.issue(codex, "claim", "4")
    with pytest.raises(BridgeError, match="owned by codex"):
        bridge.trial_start(repo, "4", "claude,codex")
    with pytest.raises(BridgeError, match="no trial"):
        bridge.trial_keep(repo, "4", "codex")


def test_test_counts_are_read_only_when_stated():
    assert trials.tests("all good") == {"run": None, "passed": None}
    assert trials.tests("12 passed, 2 errors") == {"run": 14, "passed": 12}


def test_ended_record_ignores_damage(tmp_path):
    (tmp_path / trials.RECORD_NAME).write_text("[1]")
    assert trials.ended(tmp_path) == []
    (tmp_path / trials.RECORD_NAME).write_text(json.dumps({"trials": [1]}))
    assert trials.ended(tmp_path) == []
