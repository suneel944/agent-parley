"""Checks selecting several lanes for one operator command."""

import argparse
import builtins
import sys
from pathlib import Path

import pytest

from agent_parley import cli, roster, store
from agent_parley.cli import git
from agent_parley.state import BridgeError


def selector(**filters):
    """Builds the parsed selector flags a bulk command carries."""
    flags = {
        "all": False,
        "provider": "",
        "outcome": "",
        "drifted": False,
        "idle": False,
    }
    return argparse.Namespace(**{**flags, **filters})


def run(bridge, monkeypatch, capsys, *arguments, expected=0):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    assert cli.main() == expected
    return capsys.readouterr().out


def registered(bridge, paired, *names):
    """Registers the named lanes with the coordination store."""
    store.initialize(bridge.home)
    for name in names:
        store.register(bridge.home, paired["root"], name)


def commit(worktree, message):
    """Records every pending change with a fixed identity."""
    git(worktree, "add", "--all")
    git(
        worktree,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )


def worked(bridge, paired, name, issue, filename):
    """Claims one issue, commits lane work and reports the lane ready."""
    lane = Path(paired["lanes"][name])
    bridge.issue(lane, "claim", issue)
    (lane / filename).write_text(f"{name} work\n")
    commit(lane, f"{name} work")
    bridge.report(lane, "ready", f"{name} finished", "", "make check")
    return lane


def state(bridge, repo):
    """Returns the private state directory and roster of the project."""
    _, directory = bridge.project(repo, create=False)
    return directory, roster.read(directory)


def test_a_selector_narrows_lanes_by_provider(bridge, repo, paired):
    directory, data = state(bridge, repo)

    matched = cli.matching_lanes(
        bridge.home, directory, data, selector(provider="codex")
    )

    assert matched == ["codex"]


def test_all_selects_every_lane_and_filters_narrow_it(bridge, repo, paired):
    directory, data = state(bridge, repo)

    assert cli.matching_lanes(
        bridge.home, directory, data, selector(all=True)
    ) == ["claude", "codex"]
    assert (
        cli.matching_lanes(
            bridge.home, directory, data, selector(all=True, outcome="ready")
        )
        == []
    )


def test_a_reported_outcome_and_a_drifted_branch_select_lanes(
    bridge, repo, paired
):
    worked(bridge, paired, "claude", "42", "first.txt")
    git(Path(paired["lanes"]["codex"]), "checkout", "-b", "elsewhere")
    directory, data = state(bridge, repo)

    assert cli.matching_lanes(
        bridge.home, directory, data, selector(outcome="ready")
    ) == ["claude"]
    assert cli.matching_lanes(
        bridge.home, directory, data, selector(drifted=True)
    ) == ["codex"]


def test_an_idle_selector_matches_no_freshly_started_lane(bridge, repo, paired):
    directory, data = state(bridge, repo)

    assert (
        cli.matching_lanes(bridge.home, directory, data, selector(idle=True))
        == []
    )


def test_one_message_reaches_every_selected_lane(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired, "claude", "codex")

    printed = run(
        bridge,
        monkeypatch,
        capsys,
        "say",
        "Freeze the branch",
        "--repo",
        str(repo),
        "--all",
        "--yes",
    )

    assert "Plan: send an operator message to 2 lanes." in printed
    assert "delivered to claude." in printed
    assert "delivered to codex." in printed
    assert "Done 2 of 2: claude, codex." in printed


def test_one_confirmation_covers_the_whole_selected_set(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired, "claude", "codex")
    asked = []
    monkeypatch.setattr(
        builtins, "input", lambda prompt: asked.append(prompt) or "y"
    )

    printed = run(
        bridge,
        monkeypatch,
        capsys,
        "say",
        "Freeze the branch",
        "--repo",
        str(repo),
        "--all",
    )

    assert len(asked) == 1
    assert "Done 2 of 2: claude, codex." in printed


def test_declining_the_plan_does_nothing(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired, "claude", "codex")
    monkeypatch.setattr(builtins, "input", lambda prompt: "n")

    printed = run(
        bridge,
        monkeypatch,
        capsys,
        "say",
        "Freeze the branch",
        "--repo",
        str(repo),
        "--all",
    )

    assert "Declined: nothing was done." in printed
    with store.connect(bridge.home) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_selector_matching_nothing_does_nothing_and_says_so(
    bridge, repo, paired, monkeypatch, capsys
):
    printed = run(
        bridge,
        monkeypatch,
        capsys,
        "participant",
        "stop",
        "--repo",
        str(repo),
        "--outcome",
        "ready",
        "--yes",
    )

    assert "Selector matched no lane, so nothing was done." in printed


def test_an_independent_refusal_leaves_the_other_lanes_done(
    bridge, repo, paired, monkeypatch, capsys
):
    registered(bridge, paired, "claude")

    printed = run(
        bridge,
        monkeypatch,
        capsys,
        "say",
        "Freeze the branch",
        "--repo",
        str(repo),
        "--all",
        "--yes",
        expected=1,
    )

    assert "delivered to claude." in printed
    assert "codex: refused." in printed
    assert "Done 1 of 2: claude. Refused or failed: codex." in printed


def test_a_name_and_a_selector_together_are_refused(bridge, repo, paired):
    with pytest.raises(BridgeError, match="never both"):
        cli.participant_lanes(
            bridge,
            repo,
            selector(all=True, action="stop", name="claude", yes=True),
        )


def test_one_key_cannot_cover_several_lanes(bridge, repo, paired):
    arguments = selector(
        all=True, participant="", text="Freeze", key="daily", yes=True
    )

    with pytest.raises(BridgeError, match="cannot cover several lanes"):
        cli.spoken_lanes(bridge, repo, arguments)


def test_a_selector_stands_in_for_one_assigned_lane(
    bridge, repo, paired, monkeypatch, capsys
):
    printed = run(
        bridge,
        monkeypatch,
        capsys,
        "issue",
        "assign",
        "42",
        "--repo",
        str(repo),
        "--provider",
        "codex",
        "--yes",
    )

    assert "Plan: offer #42 to 1 lane." in printed
    assert "Done 1 of 1: codex." in printed


def test_an_issue_is_never_offered_to_several_lanes(bridge, repo, paired):
    arguments = selector(
        all=True, name="", unassign=False, number="42", reason="", yes=True
    )

    with pytest.raises(BridgeError, match="One issue is offered to one lane"):
        cli.assigned_lanes(bridge, repo, arguments)


def test_the_merge_plan_names_prerequisites_outside_the_selection(
    bridge, repo, paired
):
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "test@example.com")
    worked(bridge, paired, "claude", "42", "first.txt")
    codex = worked(bridge, paired, "codex", "43", "second.txt")
    bridge.issue(codex, "block", "43", on="42")

    proposed = bridge.integration_plan(repo, lanes=["codex"])

    assert proposed["subject"] == "Selected ready lanes"
    assert proposed["sequence"] == ["codex"]
    assert proposed["outside"] == [
        "#42 is a prerequisite outside this selection, held by claude, so it "
        "is not satisfied here."
    ]


def test_a_selected_merge_integrates_only_the_matched_lanes(
    bridge, repo, paired
):
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "test@example.com")
    worked(bridge, paired, "claude", "42", "first.txt")
    worked(bridge, paired, "codex", "43", "second.txt")

    report = bridge.integrate(repo, lanes=["claude"])

    assert "Selected ready lanes: 1 lanes" in report
    assert "Integrated 1 of 1 lanes: claude." in report
    assert (repo / "first.txt").exists()
    assert not (repo / "second.txt").exists()


def test_a_declined_merge_plan_merges_nothing(
    bridge, repo, paired, monkeypatch, capsys
):
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "test@example.com")
    worked(bridge, paired, "claude", "42", "first.txt")
    monkeypatch.setattr(builtins, "input", lambda prompt: "")

    printed = run(
        bridge,
        monkeypatch,
        capsys,
        "participant",
        "merge",
        "--repo",
        str(repo),
        "--provider",
        "claude",
    )

    assert "Plan: integrate 1 lane." in printed
    assert "Declined: nothing was merged." in printed
    assert not (repo / "first.txt").exists()
