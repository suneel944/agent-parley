"""Checks `run --issue`: claim, bounded hydration and the fallback note."""

import json
from pathlib import Path

import pytest

from agent_parley import (
    blueprints,
    cli,
    forge,
    issues,
    launch,
    store,
    terminal,
    unattended,
)
from agent_parley.state import BridgeError

BODY = "Parser drops the last token. See #7 and #8.\n" + "x" * 20000


@pytest.fixture
def launched(bridge, repo, paired, monkeypatch):
    """Stubs the service, the native clients and the forge; records argv."""
    monkeypatch.setattr(bridge, "up", lambda: None)

    async def identity(agent, data):
        store.initialize(bridge.home)
        return store.register(
            bridge.home, data["root"], data["participants"][agent]["display"]
        )

    monkeypatch.setattr(bridge, "identity", identity)
    original = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: (
            "/bin/true" if name in ("claude", "codex") else original(name)
        ),
    )
    commands = []
    for name in ("run", "call"):
        monkeypatch.setattr(
            terminal,
            name,
            lambda command, *args, **kwargs: commands.append(command) or 0,
        )
    monkeypatch.setattr(
        forge,
        "issue_details",
        lambda repo, number: {
            "title": "Parser drops the last token",
            "body": BODY,
            "comments": [{"author": "ann", "body": "Still seen on main."}],
            "linked": {"7": "Tokenizer rewrite", "8": ""},
            "command": f"gh issue view {number} --repo o/r --comments",
        },
    )
    return commands


def owner(bridge, repo, number="42"):
    """Reads the published owner of one issue."""
    record = issues.snapshot(bridge.project(repo)[1])["issues"][number]
    return record["owner"]


def test_run_issue_claims_and_opens_with_a_bounded_digest(
    bridge, repo, launched
):
    assert bridge.launch("claude", repo, "ignored", issue="#42") == 0
    assert owner(bridge, repo) == "claude"
    task = launched[0][-1]
    assert "ignored" not in task
    assert "Title: Parser drops the last token" in task
    assert "Parser drops the last token. See #7" in task
    assert "#7: Tokenizer rewrite" in task
    assert "ann: Still seen on main." in task
    assert "record a ready report" in task
    assert launch.QUOTE_START in task and launch.QUOTE_END in task
    quoted = task.split(launch.QUOTE_START)[1].split(launch.QUOTE_END)[0]
    assert len(quoted.encode()) <= launch.ISSUE_BUDGET + 200
    assert "gh issue view 42 --repo o/r --comments" in task


def test_a_second_lane_asking_for_the_same_issue_is_refused(
    bridge, repo, launched
):
    bridge.launch("claude", repo, "", issue="42")
    with pytest.raises(BridgeError, match="owned by claude"):
        bridge.launch("codex", repo, "", issue="42")
    assert owner(bridge, repo) == "claude"
    assert len(launched) == 1


def test_a_forge_error_still_launches_with_the_fallback_note(
    bridge, repo, launched, monkeypatch
):
    monkeypatch.setattr(forge, "issue_details", lambda repo, number: None)
    assert bridge.launch("claude", repo, "", issue="42") == 0
    assert owner(bridge, repo) == "claude"
    assert "Context hydration failed" in launched[0][-1]
    assert "#42" in launched[0][-1]


def test_an_unusable_issue_number_is_refused_before_launch(
    bridge, repo, launched
):
    with pytest.raises(BridgeError, match="positive number"):
        bridge.launch("claude", repo, "", issue="abc")
    assert launched == []


def test_the_digest_names_verify_and_the_self_service_outcome():
    data = {
        "verify": ["make", "check"],
        "pull_request": {"self_service": True},
    }
    task = launch.issue_task(
        "5",
        {
            "title": "T",
            "body": "short",
            "comments": [],
            "linked": {},
            "command": "gh issue view 5",
        },
        data,
    )
    assert "Verify command: make check" in task
    assert "open a pull request" in task
    assert "Cut to the" not in task


def test_older_comments_past_the_recent_window_are_named_as_cut():
    comments = [{"author": "a", "body": str(index)} for index in range(5)]
    task = launch.issue_task(
        "5",
        {
            "title": "T",
            "body": "",
            "comments": comments,
            "linked": {},
            "command": "gh issue view 5 --comments",
        },
        {"verify": []},
    )
    assert "a: 4" in task and "a: 2" in task and "a: 1" not in task
    assert "2 comment(s)" in task
    assert "Verify command: none recorded" in task


def test_issue_details_reads_title_body_comments_and_linked_titles(
    monkeypatch,
):
    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "o/r")
    monkeypatch.setattr(
        forge,
        "_run",
        lambda args, timeout: json.dumps(
            {
                "title": "Main",
                "body": "Needs #3, not #9 again #3 or a&#4; #9",
                "comments": [{"author": {"login": "ann"}, "body": "hi"}],
            }
        ),
    )
    monkeypatch.setattr(
        forge, "issue_title", lambda repo, number: f"title {number}"
    )
    details = forge.issue_details(Path("."), "9")
    assert details["title"] == "Main"
    assert details["comments"] == [{"author": "ann", "body": "hi"}]
    assert details["linked"] == {"3": "title 3"}
    assert details["command"] == "gh issue view 9 --repo o/r --comments"


def test_issue_details_reports_absence_on_a_failed_read(monkeypatch):
    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "o/r")
    monkeypatch.setattr(forge, "_run", lambda args, timeout: None)
    assert forge.issue_details(Path("."), "9") is None
    monkeypatch.setattr(forge, "_implementation", lambda repo: "null")
    assert forge.issue_details(Path("."), "9") is None


def implement(bridge, repo, tmp_path, monkeypatch):
    """Records a one-node blueprint that hands the lane its prompt."""
    monkeypatch.delenv(unattended.LANE_TOKEN, raising=False)
    path = tmp_path / "implement.json"
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
    blueprints.define(bridge, repo, "implement", path)
    return bridge.project(repo)[1]


def test_run_issue_with_a_blueprint_claims_then_starts_the_run(
    bridge, repo, launched, tmp_path, monkeypatch
):
    directory = implement(bridge, repo, tmp_path, monkeypatch)

    assert (
        bridge.launch("claude", repo, "", issue="42", blueprint="implement")
        == 0
    )

    assert owner(bridge, repo) == "claude"
    run = blueprints.for_lane(directory, "claude")["42"]
    assert (run["node"], run["status"]) == ("implement", "waiting")


def test_a_label_default_starts_its_blueprint_without_the_flag(
    bridge, repo, launched, tmp_path, monkeypatch
):
    directory = implement(bridge, repo, tmp_path, monkeypatch)
    monkeypatch.setattr(forge, "issue_labels", lambda root, number: ["bug"])
    blueprints.default(bridge, repo, "bug", "implement")

    assert bridge.launch("claude", repo, "", issue="42") == 0

    assert blueprints.for_lane(directory, "claude")["42"]["node"] == (
        "implement"
    )


def test_a_relaunch_on_the_same_issue_keeps_the_run_in_progress(
    bridge, repo, launched, tmp_path, monkeypatch, capsys
):
    directory = implement(bridge, repo, tmp_path, monkeypatch)
    bridge.launch("claude", repo, "", issue="42", blueprint="implement")
    first = blueprints.for_lane(directory, "claude")["42"]

    assert (
        bridge.launch("claude", repo, "", issue="42", blueprint="implement")
        == 0
    )

    assert len(launched) == 2
    assert blueprints.for_lane(directory, "claude")["42"] == first
    assert "Kept blueprint run" in capsys.readouterr().err


def test_unread_labels_with_label_defaults_refuse_the_launch(
    bridge, repo, launched, tmp_path, monkeypatch
):
    directory = implement(bridge, repo, tmp_path, monkeypatch)
    blueprints.default(bridge, repo, "bug", "implement")
    monkeypatch.setattr(forge, "issue_labels", lambda root, number: None)

    with pytest.raises(BridgeError, match="could not be read.*--blueprint"):
        bridge.launch("claude", repo, "", issue="42")

    assert launched == []
    assert blueprints.for_lane(directory, "claude") == {}


def test_a_blueprint_launch_keeps_the_refusal_rules(
    bridge, repo, launched, tmp_path, monkeypatch
):
    directory = implement(bridge, repo, tmp_path, monkeypatch)

    with pytest.raises(BridgeError, match="--issue N"):
        bridge.launch("claude", repo, "", blueprint="implement")
    with pytest.raises(BridgeError, match="No blueprint named"):
        bridge.launch("claude", repo, "", issue="42", blueprint="missing")
    monkeypatch.setenv(unattended.LANE_TOKEN, "lane-token")
    with pytest.raises(BridgeError, match="operator shell"):
        bridge.launch("claude", repo, "", issue="42", blueprint="implement")

    assert launched == []
    assert "42" not in issues.snapshot(directory)["issues"]
    assert blueprints.for_lane(directory, "claude") == {}


def test_run_without_a_blueprint_or_label_default_is_unchanged(
    bridge, repo, launched, tmp_path, monkeypatch
):
    directory = implement(bridge, repo, tmp_path, monkeypatch)
    monkeypatch.setattr(
        forge,
        "issue_labels",
        lambda root, number: pytest.fail("labels read without a default"),
    )

    assert bridge.launch("claude", repo, "", issue="42") == 0

    assert blueprints.for_lane(directory, "claude") == {}
