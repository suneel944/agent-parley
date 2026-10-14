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
RETIRED = ("orphan_claim", "closed_claim", "idle_key", "failed_ci")
FAKE = "test_claim"


@pytest.fixture
def reversible(monkeypatch) -> str:
    """Declares one reversible kind, since no shipped kind is reversible."""
    monkeypatch.setitem(
        timeouts.KINDS,
        FAKE,
        timeouts.Kind(
            timeouts.REVERSIBLE,
            "resolve",
            "agent-parley issue assign {issue} {lane}",
            "end a test claim",
        ),
    )
    return FAKE


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


def test_a_reversible_decision_times_out_and_applies_the_default(reversible):
    open_question = question(reversible)
    waited = ASKED + timeouts.TIMEOUT_SECONDS
    assert timeouts.settle({}, open_question, waited - 1) is None

    applied = timeouts.settle({}, open_question, waited)

    assert applied == {
        "decision": reversible,
        "option": "resolve",
        "outcome": timeouts.APPLIED,
        "evidence": ["lane codex stopped 40 minutes ago"],
        "fields": CLAIM,
        "undo": "agent-parley issue assign 42 codex",
    }
    notice = timeouts.line(applied)
    assert notice.startswith("applied by timeout: end a test claim (resolve)")
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


def test_conclusive_evidence_skips_the_question(reversible):
    dead = question(
        reversible,
        conclusive=True,
        evidence=["lane codex worktree removed"],
    )

    applied = timeouts.settle({}, dead, ASKED)

    assert applied is not None
    assert applied["outcome"] == timeouts.CONCLUDED
    assert applied["option"] == "resolve"
    assert applied["evidence"] == ["lane codex worktree removed"]


def test_every_reversible_kind_has_a_producer():
    reversible = {
        name
        for name, kind in timeouts.KINDS.items()
        if kind.reversibility == timeouts.REVERSIBLE
    }
    assert reversible <= set(timeouts.EVENTS.values())


def test_a_project_policy_override_is_honoured(reversible):
    asking = {timeouts.POLICY: {reversible: {timeouts.ASK: True}}}
    late = ASKED + timeouts.MAX_TIMEOUT_SECONDS
    assert timeouts.settle(asking, question(reversible), late) is None
    conclusive = question(reversible, conclusive=True)
    assert timeouts.settle(asking, conclusive, ASKED) is None
    longer = {timeouts.POLICY: {reversible: {timeouts.SECONDS: 7200}}}
    claim = question(reversible)
    assert timeouts.settle(longer, claim, ASKED + 7199) is None
    applied = timeouts.settle(longer, claim, ASKED + 7200)
    assert applied is not None
    assert applied["undo"] == "agent-parley issue assign 42 codex"


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"merge_default": {timeouts.SECONDS: 3600}}, "irreversible"),
        ({FAKE: {timeouts.SECONDS: 60}}, "only raise"),
        ({FAKE: {timeouts.ASK: False}}, "invalid"),
    ],
)
def test_no_policy_makes_a_kind_less_cautious(reversible, entry, match):
    with pytest.raises(BridgeError, match=match):
        timeouts.timeout({timeouts.POLICY: entry}, reversible)


def test_a_missing_undo_field_refuses_to_apply(reversible):
    orphan = question(reversible, fields={"issue": "42"})
    with pytest.raises(BridgeError, match="needs lane"):
        timeouts.settle({}, orphan, ASKED + timeouts.TIMEOUT_SECONDS)


def test_an_applied_default_is_recorded_and_announced(
    tmp_path: Path, monkeypatch, reversible
):
    applied = timeouts.settle(
        {}, question(reversible), ASKED + timeouts.TIMEOUT_SECONDS
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


@pytest.mark.parametrize("name", RETIRED)
def test_a_stored_policy_for_a_retired_kind_is_ignored(tmp_path: Path, name):
    manifest = {timeouts.POLICY: {name: {timeouts.SECONDS: 7200}}}
    assert name not in timeouts.KINDS
    assert timeouts.policy(manifest) == {}
    assert name not in timeouts.describe(manifest)
    assert timeouts.timeout(manifest, name) is None
    open_decision(tmp_path, notify.Event.ORPHAN_DECISION)
    due = ASKED + timeouts.TIMEOUT_SECONDS
    assert timeouts.sweep(tmp_path, tmp_path, manifest, due) == []


def test_the_sweep_answers_only_a_due_reversible_decision(
    tmp_path: Path, monkeypatch
):
    sent = []
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "telegram")
    monkeypatch.setattr(notify, "settings", lambda: {"transports": []})
    monkeypatch.setattr(
        notify, "send", lambda config, subject, body: sent.append(body) or []
    )
    monkeypatch.setitem(
        timeouts.KINDS,
        "idle_lane",
        timeouts.Kind(
            timeouts.REVERSIBLE,
            "retire",
            "agent-parley run {lane} --resume",
            "wake an idle lane",
        ),
    )
    monkeypatch.setitem(timeouts.EVENTS, notify.Event.LANE_IDLE, "idle_lane")
    idle = open_decision(tmp_path, notify.Event.LANE_IDLE)
    prompt = open_decision(tmp_path, notify.Event.PERMISSION_PROMPT)
    due = ASKED + timeouts.TIMEOUT_SECONDS

    assert timeouts.sweep(tmp_path, tmp_path, {}, due - 1) == []
    assert decisions.get(tmp_path, idle["id"])["state"] == decisions.OPEN
    asking = {timeouts.POLICY: {"idle_lane": {timeouts.ASK: True}}}
    assert timeouts.sweep(tmp_path, tmp_path, asking, due) == []

    applied = timeouts.sweep(tmp_path, tmp_path, {}, due)

    assert [entry["decision"] for entry in applied] == ["idle_lane"]
    answered = decisions.get(tmp_path, idle["id"])
    assert answered["state"] == decisions.ANSWERED
    assert answered["answer"] == idle["recommended"] == "wake"
    assert answered["answered_by"] == timeouts.ANSWERED_BY
    assert applied[0]["option"] == "wake"
    record = decisions.get(tmp_path, prompt["id"])
    assert record["state"] == decisions.OPEN
    report = metrics.report_records(tmp_path, "codex")[-1]
    assert report["outcome"] == timeouts.APPLIED
    assert report["option"] == "wake"
    assert report["undo"] == "agent-parley run codex --resume"
    assert sent == [timeouts.line(applied[0])]
    later = ASKED + timeouts.TIMEOUT_SECONDS * 10
    assert timeouts.sweep(tmp_path, tmp_path, {}, later) == []
    assert decisions.get(tmp_path, prompt["id"])["state"] == decisions.OPEN


def test_an_unanswered_orphan_decision_waits_for_the_operator(
    tmp_path: Path, monkeypatch
):
    sent = []
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "telegram")
    monkeypatch.setattr(notify, "settings", lambda: {"transports": []})
    monkeypatch.setattr(
        notify, "send", lambda config, subject, body: sent.append(body) or []
    )
    orphan = open_decision(tmp_path, notify.Event.ORPHAN_DECISION)
    later = ASKED + timeouts.TIMEOUT_SECONDS * 10

    assert timeouts.sweep(tmp_path, tmp_path, {}, later) == []

    assert decisions.get(tmp_path, orphan["id"])["state"] == decisions.OPEN
    assert metrics.report_records(tmp_path, "codex") == []
    assert sent == []
    assert "orphan_claim" not in timeouts.KINDS


def test_the_command_line_sets_shows_and_resets_a_kind(
    bridge, repo, paired, monkeypatch, capsys, reversible
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
        "timeout", "set", reversible, "--after", "2h", "--repo", str(repo)
    )
    assert f"{reversible}: reversible, default resolve, 7200s" in shown
    shown = run("timeout", "show", "--repo", str(repo))
    assert "merge_default: irreversible" in shown
    assert "failed_ci" not in shown
    _, directory = bridge.project(repo, create=False)
    assert roster.read(directory)[timeouts.POLICY] == {
        reversible: {timeouts.SECONDS: 7200}
    }
    run("timeout", "set", reversible, "--repo", str(repo))
    assert not roster.read(directory).get(timeouts.POLICY)


def test_a_lane_cannot_set_a_timeout(
    bridge, repo, paired, monkeypatch, reversible
):
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    lane = Path(paired["lanes"]["codex"])
    with pytest.raises(BridgeError, match="never from an assigned worktree"):
        timeouts.configure(bridge, lane, reversible, seconds=7200)
    with pytest.raises(BridgeError, match="irreversible"):
        timeouts.configure(bridge, repo, "merge_default", seconds=7200)


@pytest.mark.parametrize("name", RETIRED)
def test_a_retired_kind_timeout_is_refused(
    bridge, repo, paired, monkeypatch, name
):
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    for options in ({"ask": True}, {"seconds": 7200}, {}):
        with pytest.raises(BridgeError, match="not a decision kind"):
            timeouts.configure(bridge, repo, name, **options)
