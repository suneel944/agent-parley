"""Checks the conditions that now reach the operator through notification."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_parley import (
    blueprints,
    decisions,
    dialogs,
    issues,
    notify,
    plugins,
    problems,
    process,
    roster,
    status,
    store,
    supervision,
    timeouts,
    unattended,
)
from agent_parley.state import BridgeError, write_json


@pytest.fixture
def transport(monkeypatch):
    """Names a transport so the problem decisions are kept."""
    monkeypatch.setenv("AGENT_PARLEY_NOTIFY", "fake")


def rows(bridge, condition):
    """Derives the problem rows narrowed to one condition."""
    return [row for row in bridge.problems() if row["condition"] == condition]


def row(condition, project, seconds=30, actor="operator", **extra):
    """Shapes one problem row as the derivation reports it."""
    return {
        "condition": condition,
        "project": project,
        "participant": extra.pop("participant", ""),
        "seconds": seconds,
        "actor": actor,
        "detail": "observed",
        "command": "do it",
        **extra,
    }


def feed(monkeypatch, found, now):
    """Seeds the shared problem rows so the derivation is not repeated."""
    monkeypatch.setitem(notify._PROBLEMS, "at", now)
    monkeypatch.setitem(notify._PROBLEMS, "rows", found)


def second_project(directory):
    """Adds a project directory that sorts after the given one."""
    other = directory.parent / "zzz"
    other.mkdir()
    write_json(other / "project.json", {"root": "other"})
    return other


def test_every_new_condition_is_a_notifiable_problem():
    for condition in (
        "orphaned claims",
        "supervision failing",
        "forge refused",
        "unanswered offer",
        "blueprint blocked",
        "escalated plan revision",
        "plan revisions pending",
    ):
        assert condition in notify.PROBLEM_CONDITIONS
    assert notify.PROBLEM_DELAYS == {"unanswered offer": 600.0}


def test_a_young_offer_waits_and_an_old_one_opens_a_decision(
    bridge, repo, paired, transport, monkeypatch
):
    directory = bridge.project(repo)[1]
    root = paired["root"]
    now = time.time()
    feed(monkeypatch, [row("unanswered offer", root, 599)], now)
    assert notify.problem_decisions(bridge.home, directory, root, now) == []
    feed(monkeypatch, [row("unanswered offer", root, 601)], now)
    [kept] = notify.problem_decisions(bridge.home, directory, root, now)
    [record] = decisions.list_open(directory, now)
    assert record["id"] == kept
    assert record["kind"] == "unanswered offer"


def test_a_row_naming_no_project_is_carried_by_the_first_directory_only(
    bridge, repo, paired, transport, monkeypatch
):
    directory = bridge.project(repo)[1]
    other = second_project(directory)
    now = time.time()
    feed(monkeypatch, [row("supervision failing", "")], now)
    assert notify.problem_decisions(bridge.home, other, "other", now) == []
    assert not decisions.list_open(other, now)
    [kept] = notify.problem_decisions(
        bridge.home, directory, paired["root"], now
    )
    assert [record["id"] for record in decisions.list_open(directory, now)] == [
        kept
    ]


def test_an_orphaned_claim_already_asked_is_not_asked_again(
    bridge, repo, paired, transport, monkeypatch
):
    directory = bridge.project(repo)[1]
    root = paired["root"]
    now = time.time()
    decisions.open_or_refresh(
        directory,
        project=root,
        lane="claude",
        kind=notify.Event.ORPHAN_DECISION.value,
        key="orphan",
        question="Decide",
        options=("acknowledge",),
        now=now,
    )
    feed(
        monkeypatch,
        [
            row("orphaned claims", root, participant="claude"),
            row("orphaned claims", root, participant="codex"),
        ],
        now,
    )
    notify.problem_decisions(bridge.home, directory, root, now)
    lanes = {
        record["lane"]
        for record in decisions.list_open(directory, now)
        if record["kind"] == "orphaned claims"
    }
    assert lanes == {"codex"}


def test_a_dead_lane_woken_then_told_to_wait_is_asked_once(
    bridge, repo, paired, transport, monkeypatch
):
    directory = bridge.project(repo)[1]
    root = paired["root"]
    start = time.time()
    dead = [row("orphaned claims", root, participant="claude")]
    marker = {"owner": "claude", "reason": "dead", "created": start}
    ledger = {"revision": 1, "issues": {"7": {"owner": "claude"}}}
    ledger["issues"]["7"]["orphan"] = marker
    write_json(directory / "issues.json", ledger)

    def asked(found, now):
        """Polls once and returns the lane's orphaned claims decisions."""
        feed(monkeypatch, found, now)
        notify.problem_decisions(bridge.home, directory, root, now)
        return [
            record
            for record in decisions.list_open(directory, now, answered=True)
            if record["kind"] == "orphaned claims"
        ]

    [first] = asked(dead, start)
    for step, found in enumerate(([], dead, [], dead), 1):
        [again] = asked(found, start + 60 * step)
        assert again["created"] == first["created"]
    orphan = decisions.open_or_refresh(
        directory,
        project=root,
        lane="claude",
        kind=notify.Event.ORPHAN_DECISION.value,
        key="orphan",
        question="Decide",
        options=(notify.ORPHAN_WAIT,),
        now=start + 300,
    )
    decisions.answer(
        directory, orphan["id"], notify.ORPHAN_WAIT, "operator", start + 310
    )
    assert decisions.applied(directory, orphan["id"], start + 320)
    [after] = asked(dead, start + 360)
    assert after["created"] == first["created"]
    del ledger["issues"]["7"]["orphan"]
    write_json(directory / "issues.json", ledger)
    assert asked([], start + 420) == []


def test_a_service_row_never_opens_a_decision(
    bridge, repo, paired, transport, monkeypatch
):
    directory = bridge.project(repo)[1]
    now = time.time()
    found = [row("forge refused", paired["root"], actor="service")]
    feed(monkeypatch, found, now)
    assert (
        notify.problem_decisions(bridge.home, directory, paired["root"], now)
        == []
    )


def test_a_session_ended_event_has_a_title_and_reports_its_age():
    event = notify.Event.SESSION_ENDED
    assert event.value == "session_ended"
    assert notify.TITLES[event]
    assert notify.KEY_FIELDS[event] == ("since",)
    subject, body = notify.compose(
        event, {"repo": "demo", "since": 5, "lane": "claude"}
    )
    assert notify.TITLES[event] in subject
    assert "repo: demo" in body


def forge_cache(directory, kind, read_ago, failed_ago):
    """Writes a forge cache holding one failed reading."""
    now = time.time()
    write_json(
        directory / status.FORGE_ISSUES,
        {
            "read_at": now - read_ago if read_ago else 0,
            "failed_at": now - failed_ago,
            "issues": {},
            "failure": {"kind": kind, "detail": "no answer"},
        },
    )


def test_a_repeating_timeout_is_the_operators_and_a_first_one_is_not(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    forge_cache(directory, "timeout", 100, 30)
    assert not rows(bridge, problems.FORGE_REFUSED)
    horizon = status.FORGE_TTL + status.FORGE_RETRY
    forge_cache(directory, "timeout", horizon + 30, 20)
    [found] = rows(bridge, problems.FORGE_REFUSED)
    assert found["actor"] == problems.BY_OPERATOR
    assert found["project"] == paired["root"]
    assert "did not answer the supervisor" in found["detail"]
    assert found["command"].startswith("gh issue list --limit 1")
    forge_cache(directory, "error", 0, 20)
    [found] = rows(bridge, problems.FORGE_REFUSED)
    assert found["actor"] == problems.BY_OPERATOR


def test_a_rate_limit_is_the_services_own_row(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    forge_cache(directory, "rate", 900, 30)
    [found] = rows(bridge, problems.FORGE_REFUSED)
    assert found["actor"] == problems.BY_SERVICE
    assert "retries at" in found["command"]


@pytest.fixture
def claimed(bridge, repo, paired, monkeypatch):
    """Leaves the codex lane holding issue 42 in an operator shell."""
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda *args: None
    )
    monkeypatch.setattr("agent_parley.cli.forge.assign", lambda *args: True)
    store.initialize(bridge.home)
    for participant in ("claude", "codex"):
        store.register(bridge.home, paired["root"], participant)
    lane = Path(paired["lanes"]["codex"])
    bridge.issue(lane, "claim", "42")
    return lane


def test_a_blocked_blueprint_run_is_a_row_until_its_issue_ends(
    bridge, repo, claimed, tmp_path, monkeypatch
):
    path = tmp_path / "solo.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "name": "implement",
                        "kind": "agent",
                        "prompt": "Implement it.",
                    }
                ]
            }
        )
    )
    blueprints.define(bridge, repo, "solo", path)
    blueprints.start(bridge, repo, "codex", "solo", "42")
    _, directory = bridge.project(repo)
    manifest = roster.read(directory)
    assert not rows(bridge, problems.BLUEPRINT)
    bridge.issue(claimed, "release", "42")
    for thread in blueprints.supervise(bridge.home, directory, manifest):
        thread.join(timeout=10)
    [found] = rows(bridge, problems.BLUEPRINT)
    assert found["participant"] == "codex"
    assert found["project"] == manifest["root"]
    assert found["actor"] == problems.BY_OPERATOR
    assert "blueprint run codex solo --issue 42" in found["command"]
    monkeypatch.setattr(issues, "ended", lambda record: True)
    assert not rows(bridge, problems.BLUEPRINT)


def test_a_failing_service_upkeep_is_an_estate_row_until_cleared(
    bridge, repo, paired
):
    assert not any(
        found["detail"].startswith("service upkeep failing")
        for found in bridge.problems()
    )
    issues.note_supervision_error(bridge.home, "prune store: disk full")
    [found] = [
        found
        for found in rows(bridge, problems.SUPERVISING)
        if found["detail"].startswith("service upkeep failing")
    ]
    assert found["project"] == ""
    assert found["actor"] == problems.BY_OPERATOR
    assert "prune store: disk full" in found["detail"]
    (bridge.home / issues.SUPERVISION_ERROR).unlink()
    assert not rows(bridge, problems.SUPERVISING)


def test_a_forge_upkeep_failure_is_raised_until_that_step_succeeds(
    bridge, repo, paired, monkeypatch
):
    directory = bridge.project(repo)[1]
    key = (str(directory), status.FORGE_ISSUES)
    monkeypatch.setitem(supervision._UPKEEP_FAILED, key, {})
    monkeypatch.setattr(
        supervision,
        "_start_refresh",
        lambda mark, thread: thread.run(),
    )
    outcome = {"error": True}

    def refresh(*args):
        """Fails or succeeds as the test decides."""
        if outcome["error"]:
            raise BridgeError("forge down")

    monkeypatch.setattr(supervision, "refresh_forge_issues", refresh)
    monkeypatch.setattr(supervision, "release_forge_marks", lambda *args: None)
    supervision.forge_upkeep(directory, paired, ())
    with pytest.raises(BridgeError, match="forge refresh.*forge down"):
        supervision.forge_upkeep(directory, paired, ())
    outcome["error"] = False
    with pytest.raises(BridgeError, match="forge down"):
        supervision.forge_upkeep(directory, paired, ())
    supervision.forge_upkeep(directory, paired, ())
    assert not supervision._UPKEEP_FAILED[key]


def overdue(bridge, directory, number="42"):
    """Moves an offer's deadline into the past."""
    state = issues.snapshot(directory)
    state["issues"][number]["offer"]["deadline"] = time.time() - 120
    state["revision"] += 1
    write_json(directory / "issues.json", state)


def test_an_overdue_operator_offer_on_an_unowned_issue_is_withdrawn(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    bridge.issue_assign(repo, "42", "codex", reason="Parser work")
    supervision.expire_offers(directory, paired)
    assert issues.snapshot(directory)["issues"]["42"]["offer"]
    overdue(bridge, directory)
    supervision.expire_offers(directory, paired)
    assert issues.snapshot(directory)["issues"]["42"]["offer"] is None


def test_a_failing_prune_is_noted_and_the_first_clean_pass_clears_it(
    bridge, monkeypatch
):
    home = bridge.home
    stopped = threading.Event()
    passes = []

    def prune(*args):
        """Fails on the first pass only."""
        if not passes:
            raise OSError("disk full")

    def reap():
        """Ends the loop after its second pass."""
        passes.append(1)
        if len(passes) == 1:
            assert "prune store" in issues.supervision_error(home)["detail"]
        else:
            stopped.set()

    monkeypatch.setattr(store, "prune", prune)
    monkeypatch.setattr(plugins, "repair", lambda path: [])
    monkeypatch.setattr(supervision, "reap_launchers", reap)
    monkeypatch.setattr(supervision, "PRUNE_INTERVAL", 0)
    supervision.run(home, stopped)
    assert len(passes) == 2
    assert issues.supervision_error(home) is None
    assert not (home / issues.SUPERVISION_ERROR).exists()


def test_a_rejected_plan_proposal_is_mailed_to_the_lane_once(
    bridge, repo, paired
):
    store.initialize(bridge.home)
    token = store.register(bridge.home, paired["root"], "claude")[
        "registration_token"
    ]
    plan_file = repo.parent / "plan.toml"
    plan_file.write_text(
        '[dependencies]\n"42" = ["17"]\n\n[revisions]\n'
        'scope = ["17", "42"]\nmax_changes = 2\nmax_revisions = 5\n'
    )
    bridge.work_plan(repo, "apply", plan_file)
    base = bridge.plan_revision(repo, "proposals")["revision"]
    record = bridge.plan_revision(
        paired["lanes"]["claude"],
        "propose",
        base=base,
        add=["42:60"],
        remove=[],
        reason="found it",
        evidence=["tests fail"],
    )
    assert record["status"] == "pending"
    actor = store.authenticate(bridge.home, token)

    def mail():
        """Reads the lane's inbox with bodies."""
        return store.call(
            bridge.home, actor, "fetch_inbox", {"include_bodies": True}
        )["messages"]

    assert not mail()
    bridge.plan_revision(repo, "reject", record["id"], reason="not now")
    [message] = mail()
    assert message["subject"] == f"Plan proposal {record['id']} rejected"
    assert "Note: not now" in message["body_md"]
    bridge.plan_revision(repo, "reject", record["id"], reason="not now")
    assert len(mail()) == 1


@pytest.fixture
def session():
    """Runs a process standing in for the lane's headless launcher."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    yield child
    child.kill()
    child.wait()


def test_a_headless_session_ended_at_a_prompt_notifies_the_operator(
    bridge, repo, paired, session, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        notify, "deliver", lambda *args, **kwargs: sent.append(args)
    )
    directory = Path(paired["lanes"]["claude"]).parent
    prompt = (
        "\x1b[2J ╭──────────╮\r\n│ Bash command │\r\n│   ls\r\n│\r\n"
        "│ Do you want to proceed?\r\n│ ❯ 1. Yes\r\n│   3. No\r\n"
        "Esc to cancel · Tab to amend"
    )
    write_json(
        directory / "claude-activity.json",
        {
            "activity": "working",
            "attached": False,
            "session_id": "native-1",
            "session_pid": session.pid,
            "session_ticks": process.start_ticks(session.pid),
            "updated": time.time(),
        },
    )
    watch = dialogs.Watch(directory, "claude")
    watch.advance(prompt.encode(), 0.0)
    watch.advance(b"", 0.5)
    path = directory / "claude-activity.json"
    state = json.loads(path.read_text())
    state["dialog"]["at"] -= timeouts.TIMEOUT_SECONDS + 1
    write_json(path, state)
    ended = supervision.headless_prompts(
        bridge.home,
        directory,
        supervision.roster.read(directory),
        supervision.settings({}),
    )
    assert ended == ["claude"]
    [call] = [args for args in sent if args[2] is notify.Event.SESSION_ENDED]
    assert call[1] == "claude"
    assert call[3]["repo"] == paired["root"]
    assert "start it in a terminal" in call[3]["detail"]
