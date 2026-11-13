"""Checks `run --lanes`: distinct milestone claims and one window per lane."""

import json
import os
import sys
from pathlib import Path

import pytest

from agent_parley import cli, forge, issues, lanes_launch
from agent_parley.state import BridgeError

FAKE_TMUX = """#!{python}
import json
import os
import sys

with open(os.environ["FAKE_TMUX_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "has-session":
    sys.exit(int(os.environ.get("FAKE_TMUX_SESSION", "1")))
"""


@pytest.fixture
def seeded(bridge, repo, paired, monkeypatch):
    """Frees three issues, two of them in the milestone, and stubs clients."""
    original = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: (
            "/bin/true" if name in ("claude", "codex") else original(name)
        ),
    )
    codex = Path(paired["lanes"]["codex"])
    for number in ("43", "44", "46"):
        bridge.issue(codex, "claim", number)
        bridge.issue(codex, "release", number)
    monkeypatch.setattr(
        forge, "milestone_issues", lambda repo, title: {"43", "44"}
    )
    return repo


@pytest.fixture
def tmux(tmp_path, monkeypatch):
    """Puts a fake tmux recording its argument vectors first on PATH."""
    directory = tmp_path / "fake-bin"
    directory.mkdir()
    script = directory / "tmux"
    script.write_text(FAKE_TMUX.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "tmux.log"
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return log


def calls(log):
    """Reads the argument vectors the fake tmux recorded."""
    return [json.loads(line) for line in log.read_text().splitlines()]


def owner(bridge, repo, number):
    """Reads the published owner of one issue."""
    return issues.snapshot(bridge.project(repo)[1])["issues"][number]["owner"]


def test_two_lanes_claim_distinct_issues_in_their_own_windows(
    bridge, seeded, tmux
):
    opened = lanes_launch.open_lanes(
        bridge, seeded, ["claude", "codex"], "0.23.0"
    )
    claimed = {lane["participant"]: lane["issue"] for lane in opened["lanes"]}
    assert set(claimed.values()) == {"43", "44"}
    for name, number in claimed.items():
        assert owner(bridge, seeded, number) == name
    session = "project-with-spaces"
    assert opened["session"] == session
    recorded = calls(tmux)
    assert recorded[0] == ["has-session", "-t", f"={session}"]
    windows = recorded[1:]
    assert len(windows) == 2
    assert windows[0][:4] == ["new-session", "-d", "-s", session]
    assert windows[1][:4] == ["new-window", "-d", "-t", f"={session}:"]
    for window, (name, number) in zip(windows, claimed.items(), strict=True):
        assert window[window.index("-n") + 1] == name
        run = window[window.index("run") :]
        assert run == ["run", name, "--issue", number, "--repo", str(seeded)]
    assert lanes_launch.lines(opened)[-1] == (
        f"Attach with: tmux attach -t {session}"
    )


def test_an_existing_session_only_gains_windows(
    bridge, seeded, tmux, monkeypatch
):
    monkeypatch.setenv("FAKE_TMUX_SESSION", "0")
    lanes_launch.open_lanes(bridge, seeded, ["claude", "codex"], "0.23.0")
    assert [call[0] for call in calls(tmux)] == [
        "has-session",
        "new-window",
        "new-window",
    ]


def test_a_lane_left_without_a_free_issue_opens_with_the_reason(
    bridge, seeded, monkeypatch
):
    monkeypatch.setattr(forge, "milestone_issues", lambda repo, title: {"44"})
    opened = lanes_launch.open_lanes(
        bridge, seeded, ["claude", "codex"], "M", which=lambda name: None
    )
    first, second = opened["lanes"]
    assert first["issue"] == "44" and not first["reason"]
    assert second["issue"] == ""
    assert "no unclaimed issue of 'M' is free" in second["reason"]
    assert second["command"] == f"agent-parley run codex --repo '{seeded}'"


def test_a_refused_claim_opens_the_lane_without_an_issue(
    bridge, seeded, monkeypatch
):
    def refuse(lane, action, number=""):
        raise BridgeError(f"Issue {number} is owned by gemini.")

    monkeypatch.setattr(bridge, "issue", refuse)
    opened = lanes_launch.open_lanes(
        bridge, seeded, ["claude"], "M", which=lambda name: None
    )
    lane = opened["lanes"][0]
    assert lane["issue"] == ""
    assert "the claim of #" in lane["reason"]
    assert "owned by gemini" in lane["reason"]


def test_an_unlisted_milestone_opens_lanes_without_issues(
    bridge, seeded, monkeypatch
):
    monkeypatch.setattr(forge, "milestone_issues", lambda repo, title: None)
    opened = lanes_launch.open_lanes(
        bridge, seeded, ["claude"], "M", which=lambda name: None
    )
    assert opened["lanes"][0]["issue"] == ""
    assert "did not list" in opened["lanes"][0]["reason"]


def test_repeated_names_are_refused(bridge, seeded):
    with pytest.raises(BridgeError, match="distinct"):
        lanes_launch.open_lanes(bridge, seeded, ["claude", "claude"], "M")


def test_without_tmux_the_command_prints_one_run_line_per_lane(
    bridge, seeded, monkeypatch, capsys
):
    original = cli.shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: None if name == "tmux" else original(name),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "run",
            "--lanes",
            "claude,codex",
            "--milestone",
            "0.23.0",
            "--repo",
            str(seeded),
        ],
    )
    assert cli.main() == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].startswith("No terminal multiplexer on PATH")
    commands = printed[1:]
    assert len(commands) == 2
    numbers = set()
    for line, name in zip(commands, ("claude", "codex"), strict=True):
        assert line.startswith(f"agent-parley run {name} --issue ")
        assert line.endswith(f"--repo '{seeded}'")
        numbers.add(line.split("--issue ")[1].split()[0])
    assert numbers == {"43", "44"}


def test_status_names_the_running_session(tmux, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_SESSION", "0")
    assert lanes_launch.attach_line("/work/my.repo") == (
        "Lanes session: tmux attach -t my-repo"
    )
    monkeypatch.setenv("FAKE_TMUX_SESSION", "1")
    assert lanes_launch.attach_line("/work/my.repo") == ""
