"""Checks the suspected-flaky flag over real Git trees and report logs."""

import json
from pathlib import Path

import pytest

from agent_parley import flaky, followups, metrics, store, supervision
from agent_parley.worktrees import git

TEST = "tests/test_core.py::test_value"
FAILED = f"FAILED {TEST} - assert 1 == 2\n1 failed, 11 passed"
PASSED = f"{TEST} PASSED\n12 passed"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Builds a base checkout, a state directory and a mail recorder."""
    root = tmp_path / "base"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "lane@example.invalid")
    git(root, "config", "user.name", "Lane")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text("VALUE = 1\n")
    (root / "pkg" / "other.py").write_text("OTHER = 1\n")
    (root / "notes.txt").write_text("notes\n")
    (root / "tests" / "test_core.py").write_text(
        "from pkg.core import VALUE\n\n\ndef test_value():\n"
        "    assert VALUE == 1\n"
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    directory = tmp_path / "state"
    directory.mkdir()
    sent = []
    monkeypatch.setattr(
        store,
        "speak",
        lambda home, where, name, subject, body, key: sent.append(
            (name, subject, body, key)
        ),
    )
    manifest = {
        "root": str(root),
        "participants": {
            name: {"display": name} for name in ("one", "two", "three")
        },
    }
    return {
        "root": root,
        "directory": directory,
        "home": tmp_path,
        "sent": sent,
        "manifest": manifest,
    }


def change(project, branch, path, text):
    """Commits one file change on a new branch from main, returns its SHA."""
    root = project["root"]
    git(root, "switch", "-q", "-c", branch, "main")
    (root / path).write_text(text)
    git(root, "commit", "-q", "-am", f"change {path}")
    commit = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-q", "main")
    return commit


def report(project, lane, commit, evidence, state="partial", clean=True):
    """Appends one report record to a lane's durable report log."""
    metrics.record_report(
        project["directory"],
        lane,
        {
            "kind": "report",
            "state": state,
            "issue": "7",
            "claim_id": "c1",
            "commit": commit,
            "clean": clean,
            "evidence": evidence,
        },
    )


def supervise(project):
    """Runs one flaky-test pass of the supervision poll."""
    flaky.supervise(project["home"], project["directory"], project["manifest"])


def suggestions(project):
    """Lists the follow-up suggestions recorded for the operator."""
    path = project["directory"] / followups.RECORD_NAME
    if not path.exists():
        return []
    return list(json.loads(path.read_text())["suggestions"].values())


def held(project):
    """Reads the recorded suspicion of the test under check."""
    return flaky.read(project["directory"])["tests"][TEST]["suspected"]


def test_pytest_failures_and_passes_are_read_from_both_forms():
    failed, passed = flaky.tests_in(
        "\x1b[31mFAILED\x1b[0m tests/a.py::test_x[1-2] - boom\n"
        "tests/b/c.py::Case::test_y FAILED  [ 50%]\n"
        "ERROR tests/a.py::test_z\n"
        "tests/a.py::test_ok PASSED\n"
        "fixed error handling; 2 failed in 3.1s"
    )
    assert failed == [
        "tests/a.py::test_x[1-2]",
        "tests/b/c.py::Case::test_y",
        "tests/a.py::test_z",
    ]
    assert passed == ["tests/a.py::test_ok"]


def test_relative_and_from_imports_resolve_to_dotted_modules():
    found = flaky.imports(
        "import os\nfrom pkg.core import VALUE\nfrom . import helpers\n",
        "tests/unit/test_core.py",
    )
    assert {"os", "pkg.core", "pkg.core.VALUE", "tests.unit.helpers"} <= found
    assert flaky.touches(["src/pkg/core.py"], TEST, found)
    assert not flaky.touches(["pkg/other.py", "notes.txt"], TEST, found)
    assert flaky.touches(["tests/conftest.py"], TEST, found)
    assert flaky.touches(["conftest.py"], TEST, found)
    assert not flaky.touches(["pkg/conftest.py"], TEST, found)


def test_source_the_parser_cannot_hold_imports_nothing():
    for source in ("-" * 200000 + "1", "(" * 150 + ")" * 150, "[" * 100000):
        assert flaky.imports(source, "tests/test_deep.py") == set()


def test_a_damaged_test_record_is_dropped_on_read(tmp_path):
    event = {
        "lane": "one",
        "outcome": flaky.FAIL,
        "tree": "t" * 40,
        "source": flaky.REPORT,
        "at": 1.0,
        "touches": False,
    }
    (tmp_path / flaky.PUBLICATION).write_text(
        json.dumps(
            {
                "tests": {
                    TEST: {"events": [{"lane": "one"}], "suspected": None},
                    "tests/a.py::no_commit": {
                        "events": [event],
                        "suspected": None,
                    },
                    "tests/a.py::ok": {
                        "events": [{**event, "commit": "c" * 40}],
                        "suspected": None,
                    },
                }
            }
        )
    )
    assert list(flaky.read(tmp_path)["tests"]) == ["tests/a.py::ok"]


def test_a_test_id_is_quoted_as_code_with_its_backticks_replaced():
    held = {
        "reason": flaky.SAME_TREE,
        "evidence": [],
        "followup": "",
    }
    subject, body = flaky.message("tests/a.py::t[`x`]", held)
    assert subject == "Suspected flaky test `tests/a.py::t['x']`"
    assert "Test `tests/a.py::t['x']` is suspected" in body


def test_a_same_tree_flip_tells_every_lane_once_and_suggests_one_issue(
    project,
):
    commit = change(project, "one", "notes.txt", "more notes\n")
    report(project, "one", commit, FAILED)
    supervise(project)
    assert project["sent"] == []
    report(project, "one", commit, PASSED, "ready")
    supervise(project)
    supervise(project)

    sent = project["sent"]
    assert sorted(name for name, *_ in sent) == ["one", "three", "two"]
    assert len({key.split(":")[1] for *_, key in sent}) == 1
    subject, body = sent[0][1], sent[0][2]
    assert subject == f"Suspected flaky test `{TEST}`"
    assert f"Test `{TEST}` is suspected" in body
    assert flaky.SAME_TREE in body
    assert "no test was skipped or disabled" in body
    assert held(project)["reason"] == flaky.SAME_TREE
    pending = suggestions(project)
    assert len(pending) == 1
    assert pending[0]["title"].startswith("test: ")
    assert TEST in pending[0]["title"]
    assert pending[0]["paths"] == ["tests/test_core.py"]
    assert pending[0]["issue"] == ""
    assert held(project)["followup"] == pending[0]["id"]

    later = change(project, "two", "notes.txt", "other notes\n")
    report(project, "two", later, FAILED)
    report(project, "three", commit, FAILED)
    supervise(project)
    assert len(project["sent"]) == 3
    assert len(suggestions(project)) == 1


def test_two_lanes_failing_off_their_diffs_mark_the_test(project):
    first = change(project, "one", "notes.txt", "more notes\n")
    second = change(project, "two", "pkg/other.py", "OTHER = 2\n")
    report(project, "one", first, FAILED)
    supervise(project)
    assert project["sent"] == []
    report(project, "two", second, FAILED)
    supervise(project)

    assert held(project)["reason"] == flaky.TWO_LANES
    assert sorted(name for name, *_ in project["sent"]) == [
        "one",
        "three",
        "two",
    ]
    assert first[:12] in project["sent"][0][2]
    assert second[:12] in project["sent"][0][2]
    assert len(suggestions(project)) == 1


def test_a_real_regression_in_the_changed_code_is_never_marked(project):
    broken = change(project, "one", "pkg/core.py", "VALUE = 2\n")
    edited = change(
        project,
        "two",
        "tests/test_core.py",
        "from pkg.core import VALUE\n\n\ndef test_value():\n"
        "    assert VALUE == 3\n",
    )
    report(project, "one", broken, FAILED)
    report(project, "one", broken, "pytest tests/test_other.py: 5 passed")
    report(project, "two", edited, FAILED)
    report(project, "two", edited, PASSED, clean=False)
    fixed = change(project, "fix", "pkg/core.py", "VALUE = 1\nFIXED = True\n")
    report(project, "one", fixed, PASSED, "ready")
    supervise(project)
    supervise(project)

    assert project["sent"] == []
    assert suggestions(project) == []
    assert held(project) is None


def test_a_failure_on_a_changed_tree_clears_and_new_evidence_retells(
    project,
):
    commit = change(project, "one", "notes.txt", "more notes\n")
    report(project, "one", commit, FAILED)
    report(project, "one", commit, PASSED, "ready")
    supervise(project)
    assert len(project["sent"]) == 3

    broken = change(project, "two", "pkg/core.py", "VALUE = 2\n")
    report(project, "two", broken, FAILED)
    supervise(project)
    assert held(project) is None
    assert len(project["sent"]) == 3

    again = change(project, "three", "pkg/other.py", "OTHER = 3\n")
    report(project, "three", again, FAILED)
    report(project, "three", again, PASSED, "ready")
    supervise(project)
    assert len(project["sent"]) == 6
    assert len({key.split(":")[1] for *_, key in project["sent"]}) == 2
    assert len(suggestions(project)) == 1


def test_a_green_forge_run_on_a_failed_tree_is_a_flip(project):
    commit = change(project, "one", "notes.txt", "more notes\n")
    assert (
        flaky.record(
            project["directory"],
            project["root"],
            "one",
            commit,
            f"python failed-step log:\n\x1b[31mFAILED\x1b[0m {TEST} - boom",
            flaky.FORGE,
        )
        == 1
    )
    assert (
        flaky.record(
            project["directory"],
            project["root"],
            "one",
            commit,
            "",
            flaky.FORGE,
            green=True,
        )
        == 1
    )
    supervise(project)
    assert held(project)["reason"] == flaky.SAME_TREE
    assert len(project["sent"]) == 3


def test_lanes_forked_either_side_of_a_merged_change_are_not_two_lanes(
    project,
):
    first = change(project, "one", "notes.txt", "more notes\n")
    root = project["root"]
    (root / "pkg" / "other.py").write_text("OTHER = 5\n")
    git(root, "commit", "-q", "-am", "merged change")
    second = change(project, "two", "notes.txt", "other notes\n")
    report(project, "one", first, FAILED)
    report(project, "two", second, FAILED)
    supervise(project)

    assert held(project) is None
    assert project["sent"] == []


def test_a_refused_suggestion_still_tells_the_lanes(project, monkeypatch):
    def refuse(*args):
        raise ValueError("bad record")

    monkeypatch.setattr(followups, "suggest", refuse)
    commit = change(project, "one", "notes.txt", "more notes\n")
    report(project, "one", commit, FAILED)
    report(project, "one", commit, PASSED, "ready")
    supervise(project)

    assert held(project)["followup"] == ""
    assert len(project["sent"]) == 3


def pull_request(bridge, paired, monkeypatch, log):
    """Wakes the Claude lane on a red pull request with a quoting review."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    monkeypatch.setattr(supervision, "PULL_REQUEST_SECONDS", 0.0)
    directory = Path(paired["lanes"]["claude"]).parent
    manifest = supervision.roster.read(directory)
    review = {"author": "alice", "state": "CHANGES_REQUESTED", "at": "t1"}
    reading = {
        "number": 7,
        "url": "https://example.test/pull/7",
        "branch": manifest["participants"]["claude"]["branch"],
        "sha": git(Path(paired["root"]), "rev-parse", "HEAD"),
        "checks": "red",
        "failing": ["test"],
        "failed": [
            {"name": "test", "run": "1", "job": "2", "conclusion": "failure"}
        ]
        if log
        else [],
        "reviews": [review],
        "mergeable": "MERGEABLE",
        "issues": [],
    }
    details = [
        {
            "author": "alice",
            "at": "t1",
            "body": "FAILED tests/x.py::t and tests/x.py::u PASSED",
            "comments": [
                {"path": "a.py", "line": 3, "body": "tests/y.py::v FAILED"}
            ],
        }
    ]
    monkeypatch.setattr(
        supervision.forge, "open_pull_requests", lambda root: [reading]
    )
    monkeypatch.setattr(
        supervision.forge,
        "review_feedback",
        lambda root, number: ("gh pr view 7", details),
    )
    monkeypatch.setattr(
        supervision.forge,
        "failed_log",
        lambda root, run, job: ("gh run view 1", log),
    )
    supervision.pull_request_wakes(bridge.home, directory, manifest)
    with store.connect(bridge.home) as db:
        bodies = [
            row[0]
            for row in db.execute(
                "SELECT body_md FROM messages WHERE subject LIKE 'Pull%'"
            )
        ]
    return directory, bodies


def test_review_text_on_a_pull_request_records_nothing(
    bridge, paired, monkeypatch
):
    directory, bodies = pull_request(bridge, paired, monkeypatch, "")
    assert "FAILED tests/x.py::t" in bodies[0]
    assert flaky.read(directory)["tests"] == {}


def test_only_the_failed_step_log_of_a_pull_request_is_recorded(
    bridge, paired, monkeypatch
):
    directory, bodies = pull_request(
        bridge, paired, monkeypatch, "FAILED tests/z.py::w - boom"
    )
    assert "FAILED tests/x.py::t" in bodies[0]
    assert list(flaky.read(directory)["tests"]) == ["tests/z.py::w"]


def test_the_supervision_poll_tells_every_lane_of_a_flip(bridge, paired):
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    directory = Path(paired["lanes"]["claude"]).parent
    commit = git(Path(paired["root"]), "rev-parse", "HEAD")
    for state, evidence in (("partial", FAILED), ("ready", PASSED)):
        metrics.record_report(
            directory,
            "claude",
            {
                "kind": "report",
                "state": state,
                "commit": commit,
                "clean": True,
                "evidence": evidence,
            },
        )
    supervision.poll(bridge.home, directory)
    with store.connect(bridge.home) as db:
        told = sorted(
            row[0]
            for row in db.execute(
                "SELECT a.name FROM messages m "
                "JOIN message_recipients r ON r.message_id=m.id "
                "JOIN agents a ON a.id=r.agent_id "
                "WHERE m.subject = ?",
                (f"Suspected flaky test `{TEST}`",),
            )
        )
    assert told == ["claude", "codex"]
    assert flaky.read(directory)["tests"][TEST]["suspected"]["reason"] == (
        flaky.SAME_TREE
    )
