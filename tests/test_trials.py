"""Side-by-side trials of one issue across provider lanes."""

import json
import sys
from pathlib import Path

import pytest

from agent_parley import (
    budgets,
    cli,
    dashboard,
    issues,
    launch,
    lifecycle,
    quota,
    reclaim,
    reviews,
    roster,
    store,
    supervision,
    tables,
    trials,
)
from agent_parley.claims import forge_mark
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


def run(monkeypatch, bridge, *command: str) -> None:
    """Runs one command through the command-line entry point."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *command]
    )
    assert cli.main() == 0


def started(bridge, repo, paired) -> tuple[Path, Path, Path, dict]:
    """Starts the trial of issue #7 on both lanes of the pair."""
    claude = Path(paired["lanes"]["claude"])
    codex = Path(paired["lanes"]["codex"])
    trial = bridge.trial_start(repo, "7", "claude,codex")
    return claude, codex, claude.parent, trial


def lane(directory: Path, issue: str, name: str) -> dict:
    """Reads one lane's entry of an issue's running trial."""
    record = issues.snapshot(directory)["issues"][issue]
    return trials.running(record)["lanes"][name]


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
    view = dashboard.collect(bridge.home, False, {})
    for row in view["projects"][0]["rows"]:
        assert row["trials"] == ["7"]
        assert "#7(trial)" in row["issues"]
    capsys.readouterr()
    run(monkeypatch, bridge, "trial", "show", "7", "--repo", str(repo))
    assert "client-reported tokens" in capsys.readouterr().out
    with pytest.raises(BridgeError, match="nobody is not in the trial of #7"):
        bridge.trial_keep(repo, "7", "nobody")
    assigned = []
    monkeypatch.setattr("agent_parley.cli.forge.assigned", lambda *a: False)
    monkeypatch.setattr(
        "agent_parley.cli.forge.assign", lambda *a: assigned.append(a[1]) or 1
    )

    run(
        monkeypatch,
        bridge,
        "trial",
        "keep",
        "7",
        "--lane",
        "codex",
        "--repo",
        str(repo),
    )

    assert capsys.readouterr().out == (
        "Kept codex's branch for #7; released claude; no branch was deleted.\n"
    )
    assert assigned == ["7"]
    assert forge_mark(directory, "7", added=True)
    ended = trials.ended(directory)[-1]
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
    run(monkeypatch, bridge, "metrics", "trials")
    assert "client tokens" in capsys.readouterr().out


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

    with pytest.raises(
        BridgeError,
        match="codex already holds 2 claims, the project cap "
        "max_claims_per_lane is 2",
    ):
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
    rerun = "1 failed, 2 passed in 1s\nfixed it\n3 passed in 1s\n"
    assert trials.tests(rerun) == {"run": 3, "passed": 3}


def test_a_trial_lane_requests_a_review_round_and_show_counts_findings(
    bridge, repo, paired, quiet_forge, monkeypatch
):
    manifest = bridge.add_participant(repo, "claude-b", "claude")
    claude, codex, directory, _ = started(bridge, repo, paired)
    store.initialize(bridge.home)
    actors = {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, manifest["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "claude-b", "codex")
    }
    monkeypatch.setattr(
        supervision, "lane_state", lambda *a, **k: {"process_alive": True}
    )

    def mail(sender, to, body, key):
        return store.call(
            bridge.home,
            actors[sender],
            "send_message",
            {
                "to": to,
                "subject": paired["participants"]["claude"]["branch"],
                "body_md": body,
                "idempotency_key": key,
            },
        )["review"]

    asked = mail("claude", [reviews.ADDRESS, "a.txt"], "Check it.", "ask")
    assert asked["issue"] == "7"
    assert asked["reviewer"] == "claude-b"
    answered = mail(
        "claude-b", [reviews.ADDRESS, asked["id"]], "- one\n- two\n", "found"
    )
    assert answered["state"] == reviews.RETURNED

    rows = {row["lane"]: row for row in bridge.trial_show(repo, "7")["lanes"]}
    assert rows["claude"]["findings"] == 2
    assert rows["codex"]["findings"] is None


def test_evidence_over_the_report_budget_keeps_its_test_counts(
    bridge, repo, paired, quiet_forge
):
    claude, _, directory, _ = started(bridge, repo, paired)
    commit(claude, "a.txt")
    evidence = "collected line\n" * 400 + "4 passed, 1 failed in 1s\n"
    assert len(evidence.encode()) > 4096

    bridge.report(claude, "ready", "Done", "", evidence)

    assert lane(directory, "7", "claude")["tests"] == {"run": 5, "passed": 4}


def test_blocked_and_waiting_reports_from_a_trial_lane(
    bridge, repo, paired, quiet_forge
):
    claude, codex, directory, _ = started(bridge, repo, paired)

    bridge.report(codex, "blocked", "Stuck", "needs a key", "")
    bridge.report(claude, "waiting", "Waiting", "CI runs", "", until=60.0)

    assert lane(directory, "7", "codex")["reported"]["state"] == "blocked"
    assert lane(directory, "7", "claude")["reported"]["state"] == "waiting"
    assert "ready_at" not in lane(directory, "7", "codex")
    record = issues.snapshot(directory)["issues"]["7"]
    assert record["owner"] is None
    assert trials.running(record)["state"] == trials.RUNNING


def test_cancel_releases_every_lane_and_requeues_the_issue(
    bridge, repo, paired, quiet_forge, capsys, monkeypatch
):
    claude, codex, directory, _ = started(bridge, repo, paired)
    with pytest.raises(BridgeError, match="Only the operator can cancel"):
        bridge.trial_cancel(codex, "7")
    capsys.readouterr()

    run(monkeypatch, bridge, "trial", "cancel", "7", "--repo", str(repo))

    assert capsys.readouterr().out == (
        "Cancelled the trial of #7; released claude, codex; no branch was "
        "deleted.\n"
    )
    ended = trials.ended(directory)[-1]
    assert ended["state"] == trials.CANCELLED
    assert {entry["outcome"] for entry in ended["lanes"].values()} == {
        trials.RELEASED
    }
    ledger = issues.snapshot(directory)
    assert "trial" not in ledger["issues"]["7"]
    assert "7" in issues.unclaimed(ledger)
    assert git(repo, "branch", "--list", ended["lanes"]["claude"]["branch"])
    with pytest.raises(BridgeError, match="runs no trial"):
        bridge.trial_cancel(repo, "7")
    data = roster.read(directory)
    entry = ended["lanes"]["claude"]
    released = bridge._trial_release_text(
        data, "7", "claude", entry, ended["id"]
    )
    kept = f"trial-7-{ended['id']}-claude"
    assert f"git branch {kept} && git reset --hard {data['base']}" in released
    assert f"trial commits stay on {kept}" in released
    assert "left as it is" not in released
    second = bridge._trial_release_text(data, "7", "claude", entry, "other")
    assert "trial-7-other-claude" in second
    assert kept not in second


def test_trial_start_refuses_a_dirty_or_moved_lane(
    bridge, repo, paired, quiet_forge
):
    claude = Path(paired["lanes"]["claude"])
    codex = Path(paired["lanes"]["codex"])
    (claude / "shared.txt").write_text("edited\n")
    with pytest.raises(BridgeError, match="claude's worktree has uncommitted"):
        bridge.trial_start(repo, "7", "claude,codex")
    git(claude, "checkout", "--", "shared.txt")
    commit(codex, "c.txt")
    with pytest.raises(BridgeError, match="codex's head is not the trial base"):
        bridge.trial_start(repo, "7", "claude,codex")
    assert "7" not in issues.snapshot(claude.parent)["issues"]


def test_an_overdue_trial_tells_every_lane_until_it_ends(
    bridge, repo, paired, quiet_forge
):
    _, _, directory, _ = started(bridge, repo, paired)
    manifest = roster.read(directory)
    ledger = issues.snapshot(directory)
    ledger["issues"]["7"]["trial"]["deadline"] = 1.0
    write_json(directory / "issues.json", ledger)

    supervision.deadline_notices(directory, manifest)

    record = issues.snapshot(directory)["issues"]["7"]
    notice = issues.deadline_notice(record)
    assert notice["waiting"] == ["claude", "codex"]
    assert "trial cancel 7" in notice["text"]
    supervision.deadline_notices(directory, manifest)
    assert (
        issues.snapshot(directory)["issues"]["7"]["deadline_notice"]
        == (record["deadline_notice"])
    )

    bridge.trial_cancel(repo, "7")

    record = issues.snapshot(directory)["issues"]["7"]
    assert issues.deadline_notice(record) == {}
    supervision.deadline_notices(directory, manifest)
    assert "deadline_notice" not in issues.snapshot(directory)["issues"]["7"]


def test_trial_membership_counts_as_a_claim_elsewhere(
    bridge, repo, paired, quiet_forge, monkeypatch
):
    _, _, directory, _ = started(bridge, repo, paired)
    manifest = roster.read(directory)

    held = launch.lane_context("codex", manifest, directory, {})
    assert any("#7 (trial from base" in line for line in held)
    rows = {
        row["participant"]: row for row in reclaim.plan(directory, manifest)
    }
    assert rows["claude"]["reason"] == reclaim.CLAIMED
    assert rows["codex"]["reason"] == reclaim.CLAIMED

    monkeypatch.setattr(supervision, "fit", lambda *a, **k: {"fit": True})
    config = {"stalled_after": 1, "inactive_after": 1}
    assert (
        supervision._overdue_peer(
            bridge.home,
            directory,
            manifest,
            {**config, "max_claims_per_lane": 1},
            {},
            "claude",
        )
        is None
    )
    assert (
        supervision._overdue_peer(
            bridge.home,
            directory,
            manifest,
            {**config, "max_claims_per_lane": 2},
            {},
            "claude",
        )
        == "codex"
    )


def test_trial_mail_skips_a_lane_missing_from_the_roster(bridge, repo):
    bridge._trial_mail({"participants": {}, "root": str(repo)}, "gone", "x")


def test_ended_record_ignores_damage(tmp_path):
    (tmp_path / trials.RECORD_NAME).write_text("[1]")
    assert trials.ended(tmp_path) == []
    (tmp_path / trials.RECORD_NAME).write_text(json.dumps({"trials": [1]}))
    assert trials.ended(tmp_path) == []
