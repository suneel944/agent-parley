"""Checks issue-level convergence accounting apart from lane activity."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from agent_parley import convergence, issues, store, supervision
from agent_parley.state import BridgeError, write_json

FAILURE = "FAILED tests/test_parser.py::test_split - AssertionError: 1 != 2"
OTHER = "FAILED tests/test_parser.py::test_join - KeyError: 'name'"


def item(outcome, commit, at, signature="", identifier=""):
    """Builds one verification item as `convergence.evidence` returns it."""
    return {
        "issue": "7",
        "claim_id": "c1",
        "commit": commit,
        "at": at,
        "id": identifier or f"{commit}-{at}",
        "outcome": outcome,
        "signature": signature,
    }


def folded(*items):
    """Folds items into a fresh account started at time zero."""
    entry = convergence.fresh("c1", "claude", 0.0)
    for each in items:
        entry = convergence.fold(entry, each)
    return entry


def test_classify_reads_zero_counts_as_passing_and_keeps_no_text():
    assert convergence.classify("120 passed, 0 failed in 3.2s") == (
        convergence.PASS,
        "",
    )
    assert convergence.classify("ruff: no errors; mypy errors: 0")[0] == (
        convergence.NONE
    )
    assert convergence.classify("Wrote the parser; see the diff") == (
        convergence.NONE,
        "",
    )
    outcome, mark = convergence.classify("ran it\n" + FAILURE)
    assert outcome == convergence.FAIL
    assert len(mark) == 16 and "test_split" not in mark


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("fixed error handling, all tests pass", convergence.PASS),
        ("ran without errors", convergence.NONE),
        ("the failed import is fixed", convergence.NONE),
        ("Found 0 errors; 12 passed", convergence.PASS),
        ("3 failed, 40 passed in 2.0s", convergence.FAIL),
        ("mypy: Found 1 error in 1 file", convergence.FAIL),
        ("errors: 2", convergence.FAIL),
        ("FAILED tests/test_a.py::test_b", convergence.FAIL),
        ("Traceback (most recent call last):", convergence.FAIL),
        ("not ok 3 - parser", convergence.FAIL),
        ("make: exit status 2", convergence.FAIL),
        ("exit code 0, ok", convergence.PASS),
    ],
)
def test_only_failure_counts_and_hard_markers_are_failures(text, outcome):
    assert convergence.classify(text)[0] == outcome


def test_ready_evidence_without_a_pass_word_is_not_a_pass():
    record = {
        "kind": "report",
        "state": "ready",
        "issue": "7",
        "claim_id": "c1",
        "commit": "a" * 40,
        "at": 5.0,
        "id": "r1",
    }
    prose = convergence.evidence({**record, "evidence": "Wrote it"}, {})
    gated = convergence.evidence({**record, "evidence": "48 passed"}, {})
    assert prose["outcome"] == convergence.NONE
    assert gated["outcome"] == convergence.PASS


def test_a_signature_ignores_line_numbers_paths_durations_and_hashes():
    first = convergence.signature(
        "/tmp/a1/x/test_p.py:12: AssertionError in 0.31s at 3e3f15d0"
    )
    second = convergence.signature(
        "/home/u/b/test_p.py:98: AssertionError in 4.07s at 9ab01cd2"
    )
    assert first == second
    assert first != convergence.signature("test_p.py:12: KeyError")


def test_repeated_failure_counts_across_changing_commits():
    mark = convergence.classify(FAILURE)[1]
    entry = folded(
        item(convergence.FAIL, "a" * 40, 1.0, mark),
        item(convergence.FAIL, "b" * 40, 2.0, mark),
        item(convergence.FAIL, "c" * 40, 3.0, mark),
    )
    assert entry["failures"] == 3 and entry["repeats"] == 3
    assert convergence.decide(entry, 4.0, 3, 14400) == convergence.APPROACH


def test_a_failure_restated_at_the_same_commit_counts_once():
    mark = convergence.classify(FAILURE)[1]
    entry = folded(
        item(convergence.FAIL, "a" * 40, 1.0, mark, "r1"),
        item(convergence.FAIL, "a" * 40, 2.0, mark, "r2"),
        item(convergence.FAIL, "a" * 40, 3.0, mark, "r3"),
    )
    assert entry["failures"] == 1 and entry["repeats"] == 1
    assert convergence.decide(entry, 4.0, 3, 14400) == ""


def test_alternating_failures_are_not_improvement():
    one = convergence.classify(FAILURE)[1]
    two = convergence.classify(OTHER)[1]
    entry = folded(
        *(
            item(convergence.FAIL, f"{n:040x}", float(n), (one, two)[n % 2])
            for n in range(1, 7)
        )
    )
    assert entry["repeats"] == 1 and entry["failures"] == 6
    assert convergence.decide(entry, 7.0, 3, 14400) == convergence.APPROACH


def test_a_passing_gate_is_the_only_reset():
    mark = convergence.classify(FAILURE)[1]
    entry = folded(
        item(convergence.FAIL, "a" * 40, 1.0, mark),
        item(convergence.FAIL, "b" * 40, 2.0, mark),
        item(convergence.NONE, "c" * 40, 3.0),
        item(convergence.PASS, "d" * 40, 4.0),
    )
    assert entry["failures"] == 0 and entry["stage"] == ""
    assert entry["milestone"] == {
        "at": 4.0,
        "commit": "d" * 40,
        "source": "d" * 40 + "-4.0",
    }
    assert convergence.view(entry, 10.0)["evidence"] == convergence.PASSING


def test_a_pass_verdict_on_a_report_older_than_a_failure_is_stale():
    reports = {
        "old": {
            "kind": "report",
            "issue": "7",
            "claim_id": "c1",
            "commit": "a" * 40,
            "at": 1.0,
            "id": "old",
        }
    }
    verdict = convergence.evidence(
        {"kind": "review", "report_id": "old", "verdict": "pass", "at": 9.0},
        reports,
    )
    assert verdict["subject_at"] == 1.0 and verdict["at"] == 9.0
    mark = convergence.classify(FAILURE)[1]
    entry = folded(
        item(convergence.FAIL, "b" * 40, 4.0, mark),
        item(convergence.FAIL, "c" * 40, 5.0, mark),
        {**verdict, "id": "v1"},
    )
    assert entry["failures"] == 2 and entry["milestone"]["source"] == "claim"
    assert "v1" in entry["ids"]
    fresh_verdict = {**verdict, "id": "v2", "subject_at": 6.0}
    assert convergence.fold(entry, fresh_verdict)["failures"] == 0


def test_missing_evidence_is_visible_and_never_a_failure():
    entry = folded(
        *(item(convergence.NONE, f"{n:040x}", float(n)) for n in range(9))
    )
    shown = convergence.view(entry, 1e9)
    assert shown["evidence"] == convergence.MISSING
    assert shown["unverified"] == 9 and shown["failures"] == 0
    assert convergence.decide(entry, 1e9, 3, 1) == ""
    assert convergence.view(None)["evidence"] == convergence.MISSING


def test_a_wait_freezes_decisions_and_its_time_is_not_counted():
    mark = convergence.classify(FAILURE)[1]
    entry = folded(item(convergence.FAIL, "a" * 40, 10.0, mark))
    entry = convergence.settle_wait(entry, "external check", 20.0)
    assert convergence.decide(entry, 1e6, 3, 100) == ""
    entry = convergence.settle_wait(entry, "", 1020.0)
    assert entry["waited"] == 1000.0
    assert convergence.age(entry, 1050.0) == 50.0
    assert convergence.decide(entry, 1050.0, 3, 100) == ""
    assert convergence.decide(entry, 1101.0, 3, 100) == convergence.APPROACH


def test_responses_are_bounded_and_end_at_escalation():
    mark = convergence.classify(FAILURE)[1]
    entry = folded(
        *(item(convergence.FAIL, f"{n:040x}", n, mark) for n in (1, 2, 3))
    )
    entry.update(stage=convergence.APPROACH, stage_at=4.0, stage_failures=3)
    assert convergence.decide(entry, 5.0, 3, 14400) == ""
    entry = folded(
        *(item(convergence.FAIL, f"{n:040x}", n, mark) for n in range(1, 7))
    )
    entry.update(stage=convergence.APPROACH, stage_at=4.0, stage_failures=3)
    assert convergence.decide(entry, 7.0, 3, 14400) == convergence.ESCALATED
    entry["stage"] = convergence.ESCALATED
    assert convergence.decide(entry, 1e9, 3, 1) == ""
    entry.update(stage="", responses=convergence.MAX_RESPONSES)
    assert convergence.decide(entry, 1e9, 3, 1) == ""


def test_the_thresholds_are_validated_supervision_settings():
    config = supervision.settings({})
    assert config["convergence_repeats"] == 3
    assert config["convergence_after"] == 14400
    with pytest.raises(BridgeError, match="convergence_repeats"):
        supervision.settings({"convergence_repeats": 0})
    with pytest.raises(BridgeError, match="convergence_after"):
        supervision.settings({"convergence_after": 90000})


def commit(lane, name):
    """Commits one new file in a lane and returns the new HEAD."""
    (Path(lane) / name).write_text(name + "\n")
    for arguments in (
        ["add", name],
        [
            "-c",
            "user.name=Bridge Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            name,
        ],
    ):
        subprocess.run(
            ["git", "-C", str(lane), *arguments],
            check=True,
            capture_output=True,
        )
    return subprocess.run(
        ["git", "-C", str(lane), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def claimed(bridge, paired):
    """Leaves claude holding issue 7 with both lanes registered."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "7")
    return {"lane": lane, "directory": lane.parent}


def run_pass(bridge, directory, **settings):
    """Runs one convergence pass the way the poll stage runs it."""
    manifest = json.loads((directory / "project.json").read_text())
    config = {**supervision.DEFAULTS, **settings}
    convergence.supervise(bridge.home, directory, manifest, config)
    return convergence.read(directory).get("7")


def mail(bridge, root):
    """Returns the subjects of every convergence message in the store."""
    with store.connect(bridge.home) as db:
        rows = db.execute(
            "SELECT m.subject FROM messages m JOIN projects p "
            "ON p.id=m.project_id WHERE p.human_key=? "
            "AND m.dedup_key LIKE 'convergence:%' ORDER BY m.id",
            (root,),
        ).fetchall()
    return [row["subject"] for row in rows]


def failing(bridge, lane, name, text=FAILURE):
    """Commits a change and reports the same gate failing on it."""
    commit(lane, name)
    bridge.report(lane, "partial", "Tried " + name, "fix it", text, issue="7")


def test_repeated_failure_escalates_once_while_the_lane_keeps_moving(
    bridge, paired, claimed
):
    lane, directory = claimed["lane"], claimed["directory"]
    manifest = json.loads((directory / "project.json").read_text())
    markers = []
    for name in ("one", "two", "three"):
        failing(bridge, lane, name)
        markers.append(
            supervision._lane_activity(
                bridge.home, directory, manifest, "claude"
            )
        )
    assert markers[0] != markers[1] != markers[2]

    entry = run_pass(bridge, directory)
    assert entry["stage"] == convergence.APPROACH
    assert mail(bridge, paired["root"]) == ["Issue #7 is not converging"]

    for name in ("four", "five", "six"):
        failing(bridge, lane, name)
    entry = run_pass(bridge, directory)
    assert entry["stage"] == convergence.ESCALATED
    for name in ("seven", "eight", "nine"):
        failing(bridge, lane, name)
    run_pass(bridge, directory)
    run_pass(bridge, directory)
    assert mail(bridge, paired["root"]) == [
        "Issue #7 is not converging",
        "Issue #7 still not converging",
    ]
    record = issues.snapshot(directory)["issues"]["7"]
    assert record["owner"] == "claude" and not record.get("offer")
    shown = bridge.issue_reading(lane, "7")["convergence"]
    assert shown["evidence"] == convergence.FAILING
    assert shown["failures"] == 9 and shown["repeats"] == 9
    stored = (directory / convergence.PUBLICATION).read_text()
    assert "AssertionError" not in stored and "test_split" not in stored


def test_useful_progress_records_a_milestone_and_sends_nothing(
    bridge, paired, claimed
):
    lane, directory = claimed["lane"], claimed["directory"]
    failing(bridge, lane, "one")
    failing(bridge, lane, "two")
    head = commit(lane, "three")
    bridge.report(
        lane, "partial", "Fixed split", "join", "48 passed in 2.1s", issue="7"
    )
    failing(bridge, lane, "four", OTHER)
    entry = run_pass(bridge, directory)
    assert entry["milestone"]["commit"] == head
    assert entry["failures"] == 1 and entry["stage"] == ""
    assert mail(bridge, paired["root"]) == []


def test_superficial_churn_is_neither_progress_nor_failure(
    bridge, paired, claimed
):
    lane, directory = claimed["lane"], claimed["directory"]
    for name in ("one", "two", "three", "four"):
        commit(lane, name)
        bridge.report(lane, "partial", "Moved " + name, "more", "", issue="7")
    entry = run_pass(bridge, directory, convergence_after=1)
    shown = convergence.view(entry)
    assert shown["evidence"] == convergence.MISSING
    assert shown["unverified"] == 4 and shown["failures"] == 0
    assert entry["stage"] == "" and mail(bridge, paired["root"]) == []


def test_an_external_wait_is_explicit_and_holds_every_response(
    bridge, paired, claimed
):
    lane, directory = claimed["lane"], claimed["directory"]
    for name in ("one", "two", "three"):
        failing(bridge, lane, name)
    bridge.report(
        lane, "blocked", "Waiting on CI", "CI run", FAILURE, issue="7"
    )
    entry = run_pass(bridge, directory)
    assert entry["wait"]["kind"] == "external check"
    assert entry["stage"] == "" and mail(bridge, paired["root"]) == []
    assert bridge.issue_reading(lane, "7")["convergence"]["waiting"] == (
        "external check"
    )
    bridge.report(lane, "partial", "CI done", "fix", "", issue="7")
    entry = run_pass(bridge, directory)
    assert entry["wait"] is None and entry["stage"] == convergence.APPROACH


def test_a_new_claim_generation_ignores_the_previous_ones_failures(
    bridge, paired, claimed
):
    lane, directory = claimed["lane"], claimed["directory"]
    for name in ("one", "two"):
        failing(bridge, lane, name)
    first = run_pass(bridge, directory)
    assert first["failures"] == 2
    bridge.issue(lane, "release", "7")
    bridge.issue(lane, "claim", "7")
    failing(bridge, lane, "three")
    second = run_pass(bridge, directory)
    assert second["claim_id"] != first["claim_id"]
    assert second["failures"] == 1 and second["stage"] == ""
    assert mail(bridge, paired["root"]) == []


def test_the_account_survives_a_restart_without_replaying_evidence(
    bridge, paired, claimed
):
    lane, directory = claimed["lane"], claimed["directory"]
    for name in ("one", "two"):
        failing(bridge, lane, name)
    before = run_pass(bridge, directory)
    stored = json.loads((directory / convergence.PUBLICATION).read_text())
    write_json(directory / convergence.PUBLICATION, stored)
    after = run_pass(bridge, directory)
    assert after["failures"] == before["failures"] == 2
    assert after["ids"] == before["ids"]
    failing(bridge, lane, "three")
    assert run_pass(bridge, directory)["stage"] == convergence.APPROACH


def test_a_fresh_account_starts_at_the_current_time_and_replays_nothing(
    bridge, paired, claimed
):
    lane, directory = claimed["lane"], claimed["directory"]
    for name in ("one", "two", "three"):
        failing(bridge, lane, name)
    manifest = json.loads((directory / "project.json").read_text())
    later = time.time() + 3600
    assert convergence.observe(directory, manifest, 3, 14400, later) == []
    entry = convergence.read(directory)["7"]
    assert entry["cursor"] == later
    assert entry["failures"] == 0 and entry["ids"] == []


def test_an_unchanged_report_log_is_not_read_again(
    bridge, paired, claimed, monkeypatch
):
    lane, directory = claimed["lane"], claimed["directory"]
    failing(bridge, lane, "one")
    assert run_pass(bridge, directory)["failures"] == 1
    from agent_parley import metrics

    reads = []
    original = metrics.report_records
    monkeypatch.setattr(
        metrics,
        "report_records",
        lambda *args: reads.append(args) or original(*args),
    )
    run_pass(bridge, directory)
    assert reads == []
    failing(bridge, lane, "two")
    assert run_pass(bridge, directory)["failures"] == 2
    assert len(reads) == 1


def test_the_escalation_names_the_notification_only_when_sent(
    bridge, paired, claimed, monkeypatch
):
    from agent_parley import notify

    assert "milestone" in notify.KEY_FIELDS[notify.Event.NON_CONVERGENCE]
    sent = []
    monkeypatch.setattr(
        notify,
        "deliver",
        lambda directory, owner, event, fields: (
            sent.append(fields) or event.value
        ),
    )
    lane, directory = claimed["lane"], claimed["directory"]
    for name in ("one", "two", "three", "four", "five", "six"):
        failing(bridge, lane, name)
        run_pass(bridge, directory)
    assert mail(bridge, paired["root"])[-1] == (
        "Issue #7 still not converging; operator notified"
    )
    entry = convergence.read(directory)["7"]
    assert sent[0]["milestone"] == int(entry["milestone"]["at"])
    action = {"issue": "7", "stage": convergence.ESCALATED, "entry": entry}
    quiet = convergence.message(action, False)
    assert "has been notified" not in quiet[1]
    assert "agent-parley problems" in quiet[1]
    assert "has been notified" in convergence.message(action, True)[1]


def test_accounts_are_kept_when_prompts_are_off(bridge, paired, claimed):
    lane, directory = claimed["lane"], claimed["directory"]
    write_json(bridge.home / "supervision.json", {"prompts": False})
    for name in ("one", "two", "three"):
        failing(bridge, lane, name)
    supervision.poll(bridge.home, directory)
    stages = json.loads((directory / supervision.POLL_RECORD).read_text())
    assert "convergence" in stages["stages"]
    entry = convergence.read(directory)["7"]
    assert entry["failures"] == 3 and entry["stage"] == ""
    assert mail(bridge, paired["root"]) == []
    assert bridge.issue_reading(lane, "7")["convergence"]["evidence"] == (
        convergence.FAILING
    )


def test_a_released_issue_leaves_no_account_behind(bridge, paired, claimed):
    lane, directory = claimed["lane"], claimed["directory"]
    failing(bridge, lane, "one")
    assert run_pass(bridge, directory) is not None
    bridge.issue(lane, "release", "7")
    assert run_pass(bridge, directory) is None


def test_the_poll_runs_the_convergence_stage(bridge, paired, claimed):
    directory = claimed["directory"]
    supervision.poll(bridge.home, directory)
    stages = json.loads((directory / supervision.POLL_RECORD).read_text())
    assert "convergence" in stages["stages"]
    assert convergence.read(directory)["7"]["owner"] == "claude"
    assert time.time() >= convergence.read(directory)["7"]["since"]
