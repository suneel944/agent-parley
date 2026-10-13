"""Checks blueprints: ordered deterministic and agent steps for one claim."""

import json
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    blueprints,
    dashboard,
    roster,
    store,
    supervision,
    unattended,
    watch,
)
from agent_parley.cli import Selection, git, main
from agent_parley.state import BridgeError, write_json


def step(tmp_path: Path, name: str, status: int = 0) -> str:
    """Writes a step that records its name in a shared log and exits."""
    script = tmp_path / f"{name}.sh"
    log = tmp_path / "steps.log"
    script.write_text(
        f"#!/bin/sh\necho {name} >> '{log}'\necho {name} output\n"
        f"exit {status}\n"
    )
    script.chmod(0o755)
    return str(script)


def ran(tmp_path: Path) -> list[str]:
    """Reads the names of the steps that ran, in order."""
    log = tmp_path / "steps.log"
    return log.read_text().split() if log.exists() else []


def record(tmp_path: Path, repo: Path, name: str, nodes: list) -> Path:
    """Writes a blueprint file outside the repository."""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"nodes": nodes}))
    return path


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


def test_three_nodes_run_in_order_and_the_agent_node_waits_for_a_report(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "feature",
        [
            {"name": "format", "kind": "run", "command": [step(tmp_path, "f")]},
            {"name": "implement", "kind": "agent", "prompt": "Implement it."},
            {"name": "tests", "kind": "run", "command": [step(tmp_path, "t")]},
        ],
    )
    assert "format -> implement -> tests" in blueprints.define(
        bridge, repo, "feature", path
    )

    waiting = blueprints.start(bridge, repo, "codex", "feature", "#42")

    assert "waiting at implement; passed format" in waiting
    assert ran(tmp_path) == ["f"]
    assert "waiting at implement" in blueprints.advance(bridge, repo, "42")
    assert ran(tmp_path) == ["f"]
    found = bridge.mail(repo, "search", query="Implement", participant="codex")
    assert "Implement it." in json.dumps(found)

    bridge.report(claimed, "ready", "Implemented", "", "make check: passed")
    finished = blueprints.advance(bridge, repo, "42")

    assert "done at tests; passed format, implement, tests" in finished
    assert ran(tmp_path) == ["f", "t"]
    assert "done" in blueprints.progress(bridge, repo, "42")
    with pytest.raises(BridgeError, match="no blueprint run in progress"):
        blueprints.advance(bridge, repo, "42")


def test_a_failing_node_follows_its_failure_edge_until_its_retry_limit(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "fixing",
        [
            {
                "name": "tests",
                "kind": "run",
                "command": [step(tmp_path, "t", 3)],
                "on_failure": "fix",
                "retries": 1,
                "next": "done",
            },
            {
                "name": "fix",
                "kind": "agent",
                "prompt": "Fix the failing checks.",
                "next": "tests",
            },
            {"name": "done", "kind": "report"},
        ],
    )
    blueprints.define(bridge, repo, "fixing", path)

    assert "waiting at fix" in blueprints.start(
        bridge, repo, "codex", "fixing", "42"
    )
    found = bridge.mail(repo, "search", query="Fix", participant="codex")
    assert "t output" in json.dumps(found)

    bridge.report(claimed, "ready", "Fixed", "", "make check: passed")
    blocked = blueprints.advance(bridge, repo, "42")

    assert "blocked at tests" in blocked
    assert "past its retry limit of 1" in blocked
    assert ran(tmp_path) == ["t", "t"]


def test_a_node_without_a_failure_edge_retries_itself(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "flaky",
        [
            {
                "name": "tests",
                "kind": "run",
                "command": [step(tmp_path, "t", 1)],
                "retries": 2,
            }
        ],
    )
    blueprints.define(bridge, repo, "flaky", path)

    blocked = blueprints.start(bridge, repo, "codex", "flaky", "42")

    assert "blocked at tests; passed none" in blocked
    assert ran(tmp_path) == ["t", "t", "t"]


def test_a_lane_cannot_set_start_or_advance_a_blueprint(
    bridge, repo, claimed, tmp_path, monkeypatch
):
    path = record(
        tmp_path,
        repo,
        "plant",
        [{"name": "x", "kind": "run", "command": [step(tmp_path, "x")]}],
    )
    with pytest.raises(BridgeError, match="operator shell"):
        blueprints.define(bridge, claimed, "plant", path)
    monkeypatch.setenv(unattended.LANE_TOKEN, "lane-token")
    for attempt in (
        lambda: blueprints.define(bridge, repo, "plant", path),
        lambda: blueprints.start(bridge, repo, "codex", "plant", "42"),
        lambda: blueprints.advance(bridge, repo, "42"),
    ):
        with pytest.raises(BridgeError, match="operator shell"):
            attempt()
    monkeypatch.delenv(unattended.LANE_TOKEN)
    assert "no blueprints" in blueprints.describe(bridge, repo)
    assert ran(tmp_path) == []


def test_a_run_needs_the_lane_to_hold_the_issue(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "one",
        [{"name": "x", "kind": "run", "command": [step(tmp_path, "x")]}],
    )
    blueprints.define(bridge, repo, "one", path)
    with pytest.raises(BridgeError, match="does not hold issue #42"):
        blueprints.start(bridge, repo, "claude", "one", "42")
    with pytest.raises(BridgeError, match="No blueprint named"):
        blueprints.start(bridge, repo, "codex", "missing", "42")


def test_a_manifest_without_blueprints_reads_and_behaves_as_before(
    bridge, repo, claimed, tmp_path
):
    _, directory = bridge.project(repo)
    stored = json.loads((directory / "project.json").read_text())
    assert "blueprints" not in stored
    write_json(directory / "project.json", stored)
    assert "blueprints" not in roster.read(directory)
    assert blueprints.progress(bridge, repo) == (
        "No blueprint runs are recorded."
    )
    bridge.verification(repo, step(tmp_path, "gate"))
    path = record(
        tmp_path,
        repo,
        "kept",
        [{"name": "x", "kind": "push"}],
    )
    blueprints.define(bridge, repo, "kept", path)
    data = roster.read(directory)
    assert data["blueprints"]["kept"][0]["kind"] == "push"
    assert data["verify"] == [step(tmp_path, "gate")]
    assert "Removed blueprint kept." == blueprints.define(
        bridge, repo, "kept", None
    )
    assert "blueprints" not in roster.read(directory)


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([{"name": "m", "kind": "merge"}], "kind must be one of"),
        (
            [{"name": "a", "kind": "push", "next": "nowhere"}],
            "unknown next node",
        ),
        ([{"name": "a", "kind": "run"}], "needs a command"),
        ([{"name": "a", "kind": "agent"}], "needs a prompt"),
        (
            [{"name": "a", "kind": "push"}, {"name": "a", "kind": "push"}],
            "unique",
        ),
        ([{"name": "a", "kind": "push", "retries": -1}], "retries"),
        ([{"name": "a", "kind": "push", "shell": True}], "unknown fields"),
        ([], "1 to"),
    ],
)
def test_an_invalid_blueprint_is_refused(nodes, message):
    with pytest.raises(BridgeError, match=message):
        blueprints.parse(json.dumps({"nodes": nodes}))


def test_the_command_line_sets_and_shows_a_blueprint(
    bridge, repo, claimed, tmp_path, monkeypatch, capsys
):
    path = record(
        tmp_path,
        repo,
        "cli",
        [
            {"name": "lint", "kind": "run", "command": "ruff check ."},
            {"name": "stop", "kind": "report", "outcome": "blocked"},
        ],
    )
    home = ["agent-parley", "--home", str(bridge.home), "blueprint"]
    monkeypatch.setattr(
        sys, "argv", [*home, "set", "cli", str(path), "--repo", str(repo)]
    )
    assert main() == 0
    monkeypatch.setattr(
        sys, "argv", [*home, "show", "cli", "--repo", str(repo)]
    )
    assert main() == 0
    shown = capsys.readouterr().out
    assert "lint [run] ruff check . ok->stop fail->lint retries=0" in shown
    assert "stop [report] blocked" in shown


def checks(directory, branch, head, verdict, **extra):
    """Records the supervision poll's reading of the lane's pull request."""
    reading = {
        "number": 7,
        "url": "https://github.com/example/agent-parley/pull/7",
        "branch": branch,
        "sha": head,
        "checks": verdict,
        "failing": [],
        "failed": [],
        "rounds": 1,
        "exhausted": 0,
        "red_attempts": 1,
        **extra,
    }
    write_json(
        directory / supervision.PULL_REQUEST_RECORD,
        {"read_at": time.time(), "pull_requests": {"7": reading}},
    )


def ci_blueprint(bridge, repo, tmp_path):
    """Records a blueprint that waits on CI and hands red checks back."""
    path = record(
        tmp_path,
        repo,
        "ci",
        [
            {
                "name": "ci",
                "kind": "wait-ci",
                "next": "done",
                "on_failure": "fix",
                "retries": 2,
            },
            {
                "name": "fix",
                "kind": "agent",
                "prompt": "Fix the red checks.",
                "next": "ci",
            },
            {"name": "done", "kind": "report"},
        ],
    )
    blueprints.define(bridge, repo, "ci", path)


def test_wait_ci_hands_red_checks_back_with_logs_and_passes_on_green(
    bridge, repo, paired, claimed, tmp_path, monkeypatch
):
    ci_blueprint(bridge, repo, tmp_path)
    _, directory = bridge.project(repo)
    branch = paired["branches"]["codex"]
    head = git(repo, "rev-parse", branch)
    logs = []

    def failed_log(root, run, job):
        logs.append((run, job))
        return f"gh run view {run} --job {job} --log-failed", "assert 1 == 2"

    monkeypatch.setattr(blueprints.forge, "failed_log", failed_log)
    assert "waiting at ci; passed none" in blueprints.start(
        bridge, repo, "codex", "ci", "42"
    )
    checks(directory, branch, "0" * 40, "green")
    assert "waiting at ci" in blueprints.advance(bridge, repo, "42")

    checks(
        directory,
        branch,
        head,
        "red",
        failing=["test", "lint"],
        failed=[
            {"name": "test", "conclusion": "failure", "run": 11, "job": 22},
            {"name": "lint", "conclusion": "cancelled", "run": 11, "job": 23},
        ],
    )
    assert "waiting at fix; passed none" in blueprints.advance(
        bridge, repo, "42"
    )
    assert logs == [("11", "22")]
    found = json.dumps(
        bridge.mail(repo, "search", query="Fix", participant="codex")
    )
    assert "checks are red" in found
    assert "assert 1 == 2" in found
    assert "gh run view 11 --job 22 --log-failed" in found

    bridge.report(claimed, "ready", "Fixed", "", "make check: passed")
    assert "waiting at ci; passed fix" in blueprints.advance(bridge, repo, "42")
    assert logs == [("11", "22")]

    checks(directory, branch, head, "green")
    assert "done at done; passed fix, ci, done" in blueprints.advance(
        bridge, repo, "42"
    )


def test_wait_ci_blocks_once_the_pull_request_used_its_ci_rounds(
    bridge, repo, paired, claimed, tmp_path
):
    ci_blueprint(bridge, repo, tmp_path)
    _, directory = bridge.project(repo)
    branch = paired["branches"]["codex"]
    blueprints.start(bridge, repo, "codex", "ci", "42")
    checks(
        directory,
        branch,
        git(repo, "rev-parse", branch),
        "red",
        failing=["test"],
        exhausted=3,
    )

    blocked = blueprints.advance(bridge, repo, "42")

    assert "blocked at ci; passed none" in blocked
    assert "used 3 CI rounds" in blocked


def test_wait_ci_waits_out_supervisions_automatic_rerun_of_a_red_head(
    bridge, repo, paired, claimed, tmp_path
):
    ci_blueprint(bridge, repo, tmp_path)
    _, directory = bridge.project(repo)
    branch = paired["branches"]["codex"]
    head = git(repo, "rev-parse", branch)
    blueprints.start(bridge, repo, "codex", "ci", "42")
    cancelled = {
        "failing": ["lint"],
        "failed": [
            {"name": "lint", "conclusion": "cancelled", "run": 11, "job": 23}
        ],
        "rerun": {
            "at": 1.0,
            "checks": "lint cancelled",
            "attempt": 1,
            "commands": ["gh run rerun 11 --job 23"],
            "accepted": True,
        },
    }

    checks(directory, branch, head, "red", red_attempts=1, **cancelled)
    assert "waiting at ci; passed none" in blueprints.advance(
        bridge, repo, "42"
    )

    checks(directory, branch, head, "red", red_attempts=2, **cancelled)
    assert "waiting at fix; passed none" in blueprints.advance(
        bridge, repo, "42"
    )


def test_a_run_whose_lane_released_the_issue_ends_blocked_on_the_next_poll(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "solo",
        [{"name": "implement", "kind": "agent", "prompt": "Implement it."}],
    )
    blueprints.define(bridge, repo, "solo", path)
    blueprints.start(bridge, repo, "codex", "solo", "42")
    _, directory = bridge.project(repo)
    manifest = roster.read(directory)
    assert blueprints.supervise(bridge.home, directory, manifest) == []

    bridge.issue(claimed, "release", "42")
    for thread in blueprints.supervise(bridge.home, directory, manifest):
        thread.join(timeout=10)

    run = blueprints.for_lane(directory, "codex")["42"]
    assert run["status"] == "blocked"
    assert "no longer holds issue #42 open" in run["message"]
    assert blueprints.supervise(bridge.home, directory, manifest) == []


def test_wait_ci_blocks_when_its_pull_request_closes_without_a_verdict(
    bridge, repo, paired, claimed, tmp_path
):
    ci_blueprint(bridge, repo, tmp_path)
    _, directory = bridge.project(repo)
    branch = paired["branches"]["codex"]
    blueprints.start(bridge, repo, "codex", "ci", "42")
    checks(directory, branch, git(repo, "rev-parse", branch), "pending")
    assert "waiting at ci" in blueprints.advance(bridge, repo, "42")

    write_json(
        directory / supervision.PULL_REQUEST_RECORD,
        {"read_at": time.time(), "pull_requests": {}},
    )
    blocked = blueprints.advance(bridge, repo, "42")

    assert "blocked at ci; passed none" in blocked
    assert "Pull request #7" in blocked
    assert "no longer open" in blocked


def test_a_failing_supervised_move_is_recorded_and_resumes_after_its_save(
    bridge, repo, claimed, tmp_path, monkeypatch
):
    path = record(
        tmp_path,
        repo,
        "pair",
        [
            {"name": "implement", "kind": "agent", "prompt": "Implement it."},
            {"name": "a", "kind": "run", "command": [step(tmp_path, "a")]},
            {"name": "b", "kind": "run", "command": [step(tmp_path, "b")]},
        ],
    )
    blueprints.define(bridge, repo, "pair", path)
    blueprints.start(bridge, repo, "codex", "pair", "42")
    _, directory = bridge.project(repo)
    manifest = roster.read(directory)
    execute = blueprints._execute

    def broken(command, *args):
        if command[0].endswith("b.sh"):
            raise RuntimeError("step b exploded")
        return execute(command, *args)

    def poll():
        for thread in blueprints.supervise(bridge.home, directory, manifest):
            thread.join(timeout=10)
        return blueprints.for_lane(directory, "codex")["42"]

    monkeypatch.setattr(blueprints, "_execute", broken)
    bridge.report(claimed, "ready", "Done", "", "make check: passed")
    run = poll()

    assert (run["node"], run["passed"]) == ("b", ["implement", "a"])
    error = blueprints.issues.supervision_error(directory)
    assert error is not None
    assert "blueprint #42" in error["detail"]
    assert "step b exploded" in error["detail"]

    monkeypatch.setattr(blueprints, "_execute", execute)
    run = poll()

    assert run["status"] == "done"
    assert ran(tmp_path) == ["a", "b"]


def test_a_run_another_lane_left_in_progress_is_replaced_on_a_new_claim(
    bridge, repo, claimed, tmp_path
):
    ci_blueprint(bridge, repo, tmp_path)
    _, directory = bridge.project(repo)
    blueprints.start(bridge, repo, "codex", "ci", "42")
    bridge.issue(claimed, "release", "42")
    runs = json.loads((directory / blueprints.RUNS).read_text())
    runs["42"]["lane"] = "claude"
    write_json(directory / blueprints.RUNS, runs)
    bridge.issue(claimed, "claim", "42")

    started = blueprints.start(bridge, repo, "codex", "ci", "42")

    assert "waiting at ci; passed none" in started
    assert blueprints.for_lane(directory, "codex")["42"]["lane"] == "codex"


def test_a_pull_request_node_fails_while_the_self_service_policy_is_off(
    bridge, repo, claimed, tmp_path
):
    path = record(
        tmp_path, repo, "open", [{"name": "open", "kind": "pull-request"}]
    )
    blueprints.define(bridge, repo, "open", path)

    blocked = blueprints.start(bridge, repo, "codex", "open", "42")

    assert "blocked at open; passed none" in blocked
    _, directory = bridge.project(repo)
    run = blueprints.for_lane(directory, "codex")["42"]
    assert "self_service policy is off" in run["output"]
    assert git(repo, "branch", "--remotes") == ""


def test_a_label_default_names_the_blueprint_a_claim_starts(
    bridge, repo, claimed, tmp_path, monkeypatch, capsys
):
    path = record(
        tmp_path,
        repo,
        "bugfix",
        [{"name": "x", "kind": "run", "command": [step(tmp_path, "x")]}],
    )
    blueprints.define(bridge, repo, "bugfix", path)
    blueprints.define(bridge, repo, "other", path)
    labels = {"42": ["bug", "p1"], "43": ["docs"]}
    monkeypatch.setattr(
        blueprints.forge,
        "issue_labels",
        lambda root, number: labels.get(number),
    )
    _, directory = bridge.project(repo)
    assert blueprints.chosen(bridge, repo, "42", "") == ""

    home = ["agent-parley", "--home", str(bridge.home), "blueprint"]
    monkeypatch.setattr(
        sys, "argv", [*home, "default", "bug", "bugfix", "--repo", str(repo)]
    )
    assert main() == 0
    assert "labelled bug starts blueprint bugfix" in capsys.readouterr().out
    assert roster.read(directory)["blueprint_defaults"] == {"bug": "bugfix"}
    assert blueprints.chosen(bridge, repo, "42", "") == "bugfix"
    assert blueprints.chosen(bridge, repo, "43", "") == ""
    with pytest.raises(BridgeError, match="labels of issue #44 could not"):
        blueprints.chosen(bridge, repo, "44", "")
    assert blueprints.chosen(bridge, repo, "44", "bugfix") == "bugfix"
    assert "label bug defaults to bugfix" in blueprints.describe(bridge, repo)
    with pytest.raises(BridgeError, match="default for label bug"):
        blueprints.define(bridge, repo, "bugfix", None)

    blueprints.default(bridge, repo, "p1", "other")
    with pytest.raises(BridgeError, match="name one with --blueprint"):
        blueprints.chosen(bridge, repo, "42", "")
    assert blueprints.chosen(bridge, repo, "42", "other") == "other"
    with pytest.raises(BridgeError, match="No blueprint named"):
        blueprints.default(bridge, repo, "docs", "missing")
    monkeypatch.setenv(unattended.LANE_TOKEN, "lane-token")
    with pytest.raises(BridgeError, match="operator shell"):
        blueprints.default(bridge, repo, "docs", "other")
    monkeypatch.delenv(unattended.LANE_TOKEN)

    monkeypatch.setattr(
        sys, "argv", [*home, "default", "bug", "--repo", str(repo)]
    )
    assert main() == 0
    blueprints.default(bridge, repo, "p1", None)
    assert "blueprint_defaults" not in roster.read(directory)
    with pytest.raises(BridgeError, match="selects no blueprint"):
        blueprints.default(bridge, repo, "p1", None)


def test_status_top_and_watch_show_a_run_and_nothing_without_one(
    bridge, repo, claimed, tmp_path, capsys
):
    _, directory = bridge.project(repo)

    def streamed():
        return [
            entry
            for entry in watch.collect(
                bridge.home, directory, roster.read(directory), "codex"
            )
            if entry["kind"] == "blueprint"
        ]

    def row():
        view = dashboard.collect(bridge.home, False, {})
        return {
            entry["participant"]: entry for entry in view["projects"][0]["rows"]
        }["codex"]

    bridge.status()
    assert "Blueprint" not in capsys.readouterr().out
    assert "blueprint" not in row()
    assert streamed() == []

    path = record(
        tmp_path,
        repo,
        "feature",
        [
            {"name": "format", "kind": "run", "command": [step(tmp_path, "f")]},
            {"name": "implement", "kind": "agent", "prompt": "Implement it."},
        ],
    )
    blueprints.define(bridge, repo, "feature", path)
    blueprints.start(bridge, repo, "codex", "feature", "42")
    expected = "#42 codex feature: waiting at implement; passed format"

    bridge.status()
    assert f"Blueprint {expected}" in capsys.readouterr().out
    bridge.status(Selection(participant="codex"))
    assert expected in capsys.readouterr().out
    shown = row()
    assert shown["blueprint"] == expected
    assert expected in dashboard.notes(shown)
    assert [entry["description"] for entry in streamed()] == [
        f"blueprint {expected}"
    ]
    assert streamed()[0]["issue"] == 42


def test_supervision_advances_a_run_on_a_lane_report_and_a_ci_verdict(
    bridge, repo, paired, claimed, tmp_path
):
    path = record(
        tmp_path,
        repo,
        "auto",
        [
            {"name": "implement", "kind": "agent", "prompt": "Implement it."},
            {"name": "tests", "kind": "run", "command": [step(tmp_path, "t")]},
            {"name": "ci", "kind": "wait-ci"},
        ],
    )
    blueprints.define(bridge, repo, "auto", path)
    blueprints.start(bridge, repo, "codex", "auto", "42")
    _, directory = bridge.project(repo)
    manifest = roster.read(directory)

    def poll():
        for thread in blueprints.supervise(bridge.home, directory, manifest):
            thread.join(timeout=10)
        return blueprints.for_lane(directory, "codex")["42"]

    assert blueprints.supervise(bridge.home, directory, manifest) == []
    bridge.report(claimed, "ready", "Done", "", "make check: passed")
    run = poll()
    assert (run["node"], run["status"]) == ("ci", "waiting")
    assert run["passed"] == ["implement", "tests"]
    assert ran(tmp_path) == ["t"]

    branch = paired["branches"]["codex"]
    checks(directory, branch, git(repo, "rev-parse", branch), "green")
    run = poll()
    assert run["status"] == "done"
    assert run["passed"] == ["implement", "tests", "ci"]
