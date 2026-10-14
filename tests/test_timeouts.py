"""Checks the recommended default applied to an unanswered decision."""

import sys
from pathlib import Path

import pytest

from agent_parley import (
    cli,
    decisions,
    metrics,
    notify,
    roster,
    timeouts,
    unattended,
)
from agent_parley.state import BridgeError

ASKED = 1_000.0
CLAIM = {"issue": "42", "lane": "codex"}


def question(kind: str, **extra: object) -> dict:
    """Builds one open decision asked at `ASKED`."""
    return {
        "kind": kind,
        "asked_at": ASKED,
        "fields": CLAIM,
        "evidence": ["lane codex stopped 40 minutes ago"],
        **extra,
    }


def test_every_kind_declares_a_class_and_a_default():
    for kind in timeouts.KINDS.values():
        assert kind.reversibility in (
            timeouts.REVERSIBLE,
            timeouts.IRREVERSIBLE,
        )
        assert kind.recommended
        assert bool(kind.undo) == (kind.reversibility == timeouts.REVERSIBLE)
    irreversible = {
        name
        for name, kind in timeouts.KINDS.items()
        if kind.reversibility == timeouts.IRREVERSIBLE
    }
    assert irreversible == {
        "merge_default",
        "release_tag",
        "discard_work",
        "delete_branch",
        "native_permission",
    }


def test_a_reversible_decision_times_out_and_applies_the_default():
    open_question = question("orphan_claim")
    waited = ASKED + timeouts.TIMEOUT_SECONDS
    assert timeouts.settle({}, open_question, waited - 1) is None

    applied = timeouts.settle({}, open_question, waited)

    assert applied == {
        "decision": "orphan_claim",
        "option": "reassign",
        "outcome": timeouts.APPLIED,
        "evidence": ["lane codex stopped 40 minutes ago"],
        "fields": CLAIM,
        "undo": "agent-parley issue assign 42 codex",
    }
    notice = timeouts.line(applied)
    assert notice.startswith("applied by timeout: reassign a dead lane's")
    assert "`agent-parley issue assign 42 codex`" in notice


@pytest.mark.parametrize(
    "kind",
    [
        name
        for name, entry in timeouts.KINDS.items()
        if entry.reversibility == timeouts.IRREVERSIBLE
    ]
    + ["unknown_kind"],
)
def test_an_irreversible_decision_never_times_out(kind):
    assert timeouts.timeout({}, kind) is None
    waited = ASKED + timeouts.MAX_TIMEOUT_SECONDS * 10
    assert timeouts.settle({}, question(kind), waited) is None
    conclusive = question(kind, conclusive=True)
    assert timeouts.settle({}, conclusive, waited) is None


def test_conclusive_evidence_skips_the_question():
    dead = question(
        "orphan_claim",
        conclusive=True,
        evidence=["lane codex worktree removed"],
    )

    applied = timeouts.settle({}, dead, ASKED)

    assert applied is not None
    assert applied["outcome"] == timeouts.CONCLUDED
    assert applied["option"] == "reassign"
    assert applied["evidence"] == ["lane codex worktree removed"]


def test_every_reversible_kind_has_a_producer():
    reversible = {
        name
        for name, kind in timeouts.KINDS.items()
        if kind.reversibility == timeouts.REVERSIBLE
    }
    assert reversible <= set(timeouts.EVENTS.values())


def test_a_project_policy_override_is_honoured():
    manifest = {
        timeouts.POLICY: {
            "orphan_claim": {timeouts.ASK: True},
        }
    }
    late = ASKED + timeouts.MAX_TIMEOUT_SECONDS
    assert timeouts.settle(manifest, question("orphan_claim"), late) is None
    conclusive = question("orphan_claim", conclusive=True)
    assert timeouts.settle(manifest, conclusive, ASKED) is None
    longer = {timeouts.POLICY: {"orphan_claim": {timeouts.SECONDS: 7200}}}
    orphan = question("orphan_claim")
    assert timeouts.settle(longer, orphan, ASKED + 7199) is None
    applied = timeouts.settle(longer, orphan, ASKED + 7200)
    assert applied is not None
    assert applied["undo"] == "agent-parley issue assign 42 codex"


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"merge_default": {timeouts.SECONDS: 3600}}, "irreversible"),
        ({"orphan_claim": {timeouts.SECONDS: 60}}, "only raise"),
        ({"orphan_claim": {timeouts.ASK: False}}, "invalid"),
    ],
)
def test_no_policy_makes_a_kind_less_cautious(entry, match):
    with pytest.raises(BridgeError, match=match):
        timeouts.timeout({timeouts.POLICY: entry}, "orphan_claim")


def test_a_missing_undo_field_refuses_to_apply():
    orphan = question("orphan_claim", fields={"issue": "42"})
    with pytest.raises(BridgeError, match="needs lane"):
        timeouts.settle({}, orphan, ASKED + timeouts.TIMEOUT_SECONDS)


def test_an_applied_default_is_recorded_and_announced(
    tmp_path: Path, monkeypatch
):
    applied = timeouts.settle(
        {}, question("orphan_claim"), ASKED + timeouts.TIMEOUT_SECONDS
    )
    assert applied is not None
    sent = []
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "telegram")
    monkeypatch.setattr(notify, "settings", lambda: {"transports": []})
    monkeypatch.setattr(
        notify, "send", lambda config, subject, body: sent.append(body) or []
    )

    record = timeouts.record(tmp_path, "codex", applied)
    timeouts.announce(applied)

    assert record["kind"] == timeouts.RECORD
    assert record["outcome"] == timeouts.APPLIED
    assert metrics.report_records(tmp_path, "codex")[-1]["undo"] == (
        "agent-parley issue assign 42 codex"
    )
    assert sent == [timeouts.line(applied)]


def open_decision(directory: Path, event: notify.Event) -> dict:
    """Records one notification's decision for lane codex at `ASKED`."""
    options, reversibility = notify.ANSWERS[event]
    return decisions.open_or_refresh(
        directory,
        project="/repo",
        lane="codex",
        kind=event.value,
        key=event.value,
        question=notify.TITLES[event],
        options=options,
        issue="42",
        reversibility=reversibility,
        now=ASKED,
    )


@pytest.mark.parametrize("name", ["closed_claim", "idle_key", "failed_ci"])
def test_a_stored_policy_for_a_retired_kind_is_ignored(tmp_path: Path, name):
    manifest = {timeouts.POLICY: {name: {timeouts.SECONDS: 7200}}}
    assert timeouts.policy(manifest) == {}
    assert name not in timeouts.describe(manifest)
    open_decision(tmp_path, notify.Event.ORPHAN_DECISION)
    due = ASKED + timeouts.TIMEOUT_SECONDS
    applied = timeouts.sweep(tmp_path, tmp_path, manifest, due)
    assert [entry["decision"] for entry in applied] == ["orphan_claim"]


def test_the_sweep_answers_only_a_due_reversible_decision(
    tmp_path: Path, monkeypatch
):
    sent = []
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "telegram")
    monkeypatch.setattr(notify, "settings", lambda: {"transports": []})
    monkeypatch.setattr(
        notify, "send", lambda config, subject, body: sent.append(body) or []
    )
    orphan = open_decision(tmp_path, notify.Event.ORPHAN_DECISION)
    prompt = open_decision(tmp_path, notify.Event.PERMISSION_PROMPT)
    idle = open_decision(tmp_path, notify.Event.LANE_IDLE)
    due = ASKED + timeouts.TIMEOUT_SECONDS

    assert timeouts.sweep(tmp_path, tmp_path, {}, due - 1) == []
    assert decisions.get(tmp_path, orphan["id"])["state"] == decisions.OPEN
    asking = {timeouts.POLICY: {"orphan_claim": {timeouts.ASK: True}}}
    assert timeouts.sweep(tmp_path, tmp_path, asking, due) == []

    applied = timeouts.sweep(tmp_path, tmp_path, {}, due)

    assert [entry["decision"] for entry in applied] == ["orphan_claim"]
    answered = decisions.get(tmp_path, orphan["id"])
    assert answered["state"] == decisions.ANSWERED
    assert answered["answer"] == orphan["recommended"]
    assert answered["answered_by"] == timeouts.ANSWERED_BY
    for waiting in (prompt, idle):
        record = decisions.get(tmp_path, waiting["id"])
        assert record["state"] == decisions.OPEN
    report = metrics.report_records(tmp_path, "codex")[-1]
    assert report["outcome"] == timeouts.APPLIED
    assert report["undo"] == "agent-parley issue assign 42 codex"
    assert sent == [timeouts.line(applied[0])]
    later = ASKED + timeouts.TIMEOUT_SECONDS * 10
    assert timeouts.sweep(tmp_path, tmp_path, {}, later) == []
    assert decisions.get(tmp_path, prompt["id"])["state"] == decisions.OPEN


def test_the_command_line_sets_shows_and_resets_a_kind(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)

    def run(*arguments):
        monkeypatch.setattr(
            sys,
            "argv",
            ["agent-parley", "--home", str(bridge.home), *arguments],
        )
        assert cli.main() == 0
        return capsys.readouterr().out

    shown = run(
        "timeout", "set", "orphan_claim", "--after", "2h", "--repo", str(repo)
    )
    assert "orphan_claim: reversible, default reassign, 7200s" in shown
    shown = run("timeout", "show", "--repo", str(repo))
    assert "merge_default: irreversible" in shown
    assert "failed_ci" not in shown
    with pytest.raises(BridgeError, match="not a decision kind"):
        timeouts.configure(bridge, repo, "failed_ci", seconds=7200)
    _, directory = bridge.project(repo, create=False)
    assert roster.read(directory)[timeouts.POLICY] == {
        "orphan_claim": {timeouts.SECONDS: 7200}
    }
    run("timeout", "set", "orphan_claim", "--repo", str(repo))
    assert not roster.read(directory).get(timeouts.POLICY)


def test_a_lane_cannot_set_a_timeout(bridge, repo, paired, monkeypatch):
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    lane = Path(paired["lanes"]["codex"])
    with pytest.raises(BridgeError, match="never from an assigned worktree"):
        timeouts.configure(bridge, lane, "orphan_claim", seconds=7200)
    with pytest.raises(BridgeError, match="irreversible"):
        timeouts.configure(bridge, repo, "merge_default", seconds=7200)
