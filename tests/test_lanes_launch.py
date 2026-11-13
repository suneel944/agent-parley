"""Checks `run --lanes`: distinct milestone claims and one window per lane."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import cli, forge, issues, lanes_launch, process, roster
from agent_parley.state import BridgeError, lock

FAKE_TMUX = """#!{python}
import json
import os
import sys

with open(os.environ["FAKE_TMUX_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "has-session":
    sys.exit(int(os.environ.get("FAKE_TMUX_SESSION", "1")))
if sys.argv[1] == "list-windows":
    print("\\n".join(os.environ.get("FAKE_TMUX_WINDOWS", "").split(",")))
if "-n" in sys.argv:
    if sys.argv[sys.argv.index("-n") + 1] == os.environ.get("FAKE_TMUX_FAIL"):
        sys.stderr.write("no space for new window\\n")
        sys.exit(1)
"""

FAKE_GH = """#!{python}
import json
import os
import sys

with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps([{{"number": 43}}, {{"number": 44}}]))
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


def owners(bridge, repo):
    """Reads every issue's published owner."""
    ledger = issues.snapshot(bridge.project(repo)[1])["issues"]
    return {number: record["owner"] for number, record in ledger.items()}


def typed(bridge, name, *rest):
    """Builds the run command printed for a lane under a test home."""
    return shlex.join(
        ["agent-parley", "--home", str(bridge.home), "run", name, *rest]
    )


def lanes_argv(bridge, seeded, lanes="claude,codex", *extra):
    """Builds the `run --lanes` argument vector for one test project."""
    return [
        "agent-parley",
        "--home",
        str(bridge.home),
        "run",
        "--lanes",
        lanes,
        "--milestone",
        "0.23.0",
        "--repo",
        str(seeded),
        *extra,
    ]


def opened_windows(log):
    """Returns the window-opening calls the fake tmux recorded."""
    return [
        call for call in calls(log) if call[0] in ("new-session", "new-window")
    ]


def test_two_lanes_claim_distinct_issues_in_their_own_windows(
    bridge, seeded, tmux, monkeypatch
):
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "do-not-forward")
    opened = lanes_launch.open_lanes(
        bridge, seeded, ["claude", "codex"], "0.23.0"
    )
    claimed = {lane["participant"]: lane["issue"] for lane in opened["lanes"]}
    assert set(claimed.values()) == {"43", "44"}
    for name, number in claimed.items():
        assert owner(bridge, seeded, number) == name
    session = lanes_launch.session_name(opened["root"])
    assert session.startswith("project-with-spaces-")
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
        assert f"AGENT_PARLEY_HOME={bridge.home}" in window
        assert f"PATH={os.environ['PATH']}" in window
        assert not any("do-not-forward" in word for word in window)
        assert not any("TOKEN" in word for word in window)
    assert lanes_launch.lines(opened)[-2:] == [
        "Started: claude, codex; failed: none",
        f"Attach with: tmux attach -t {session}",
    ]


def test_an_existing_session_only_gains_windows(
    bridge, seeded, tmux, monkeypatch
):
    monkeypatch.setenv("FAKE_TMUX_SESSION", "0")
    lanes_launch.open_lanes(bridge, seeded, ["claude", "codex"], "0.23.0")
    assert [call[0] for call in calls(tmux)] == [
        "has-session",
        "list-windows",
        "new-window",
        "new-window",
    ]


def test_a_failed_window_releases_its_claim_and_later_lanes_start(
    bridge, seeded, tmux, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_TMUX_FAIL", "claude")
    monkeypatch.setattr(sys, "argv", lanes_argv(bridge, seeded))
    assert cli.main() == 1
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].startswith("claude: failed: tmux new-session failed")
    assert "no space for new window" in printed[0]
    assert "released #" in printed[0]
    assert printed[1].startswith("codex: window codex, issue #")
    assert "Started: codex; failed: claude" in printed
    held = owners(bridge, seeded)
    assert "claude" not in held.values()
    assert list(held.values()).count("codex") == 1
    assert [call[0] for call in opened_windows(tmux)] == [
        "new-session",
        "new-session",
    ]


def test_a_tmux_that_hangs_or_is_missing_is_a_refusal(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("tmux", 10)

    monkeypatch.setattr(lanes_launch.subprocess, "run", hang)
    with pytest.raises(BridgeError, match="tmux new-window failed"):
        lanes_launch._tmux(["new-window"])

    def missing(*args, **kwargs):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(lanes_launch.subprocess, "run", missing)
    with pytest.raises(BridgeError, match="tmux new-session failed"):
        lanes_launch._tmux(["new-session"])


def test_a_rerun_claims_nothing_new_and_opens_no_duplicate_window(
    bridge, seeded, tmux, monkeypatch
):
    first = lanes_launch.open_lanes(
        bridge, seeded, ["claude", "codex"], "0.23.0"
    )
    held = owners(bridge, seeded)
    monkeypatch.setenv("FAKE_TMUX_SESSION", "0")
    monkeypatch.setenv("FAKE_TMUX_WINDOWS", "claude,codex")
    tmux.write_text("")
    second = lanes_launch.open_lanes(
        bridge, seeded, ["claude", "codex"], "0.23.0"
    )
    assert owners(bridge, seeded) == held
    assert opened_windows(tmux) == []
    assert [lane["state"] for lane in second["lanes"]] == ["open", "open"]
    assert lanes_launch.lines(second)[0] == (
        "claude: skipped: window claude is already open"
    )
    assert [lane["state"] for lane in first["lanes"]] == [
        "started",
        "started",
    ]


def test_a_rerun_without_windows_reopens_the_kept_claims(bridge, seeded, tmux):
    first = lanes_launch.open_lanes(
        bridge, seeded, ["claude", "codex"], "0.23.0"
    )
    held = owners(bridge, seeded)
    tmux.write_text("")
    second = lanes_launch.open_lanes(
        bridge, seeded, ["claude", "codex"], "0.23.0"
    )
    assert owners(bridge, seeded) == held
    for before, after in zip(first["lanes"], second["lanes"], strict=True):
        assert after["issue"] == before["issue"]
        assert not after["claimed"]
        assert after["state"] == "started"
    assert len(opened_windows(tmux)) == 2


def test_a_live_lane_is_skipped_without_a_claim(bridge, seeded, tmux):
    directory = bridge.project(seeded)[1]
    lane = Path(roster.read(directory)["participants"]["claude"]["lane"])
    with lock(lane.parent / "claude.session.lock"):
        opened = lanes_launch.open_lanes(
            bridge, seeded, ["claude", "codex"], "0.23.0"
        )
    claude, codex = opened["lanes"]
    assert claude["state"] == "live" and claude["issue"] == ""
    assert "claude" not in owners(bridge, seeded).values()
    assert codex["state"] == "started" and codex["issue"]
    assert [call[call.index("-n") + 1] for call in opened_windows(tmux)] == [
        "codex"
    ]


def test_a_refusing_run_gate_leaves_no_claim(bridge, seeded, tmux, monkeypatch):
    held = owners(bridge, seeded)
    monkeypatch.setattr(
        process, "host_load", lambda system="": process.HostLoad(99.0, 1)
    )
    with pytest.raises(BridgeError, match="launch threshold"):
        lanes_launch.open_lanes(bridge, seeded, ["claude", "codex"], "0.23.0")
    assert owners(bridge, seeded) == held
    assert not tmux.exists()


def test_milestone_issues_reads_the_open_issues_through_gh(
    tmp_path, monkeypatch
):
    directory = tmp_path / "gh-bin"
    directory.mkdir()
    script = directory / "gh"
    script.write_text(FAKE_GH.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "slug", lambda repo: "owner/name")
    assert forge.milestone_issues(tmp_path, "0.23.0") == {"43", "44"}
    called = calls(log)[0]
    assert called[:2] == ["issue", "list"]
    assert called[called.index("--milestone") + 1] == "0.23.0"
    assert called[called.index("--repo") + 1] == "owner/name"


@pytest.mark.parametrize(
    ("lanes", "extra", "words"),
    [
        ("claude,", [], "a name between commas"),
        ("claude,,codex", [], "a name between commas"),
        ("claude", ["--provider", "codex"], "cannot take --provider"),
        ("claude", ["--credentials", "work"], "cannot take --credentials"),
        ("claude", ["--issue", "43"], "cannot take --issue"),
        ("claude", ["--task", "x"], "cannot take --task"),
        ("claude", ["--resume"], "cannot take --resume"),
        ("claude", ["--blueprint", "b"], "cannot take --blueprint"),
    ],
)
def test_lanes_refuses_empty_names_and_single_lane_flags(
    bridge, seeded, monkeypatch, capsys, lanes, extra, words
):
    monkeypatch.setattr(sys, "argv", lanes_argv(bridge, seeded, lanes, *extra))
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    assert words in capsys.readouterr().err
    assert "claude" not in owners(bridge, seeded).values()


def test_session_names_differ_for_same_named_checkouts(tmp_path):
    first = tmp_path / "a" / "repo"
    second = tmp_path / "b" / "repo"
    named = lanes_launch.session_name(str(first))
    assert named.startswith("repo-")
    assert len(named) == len("repo-") + lanes_launch.HASH_DIGITS
    assert named != lanes_launch.session_name(str(second))
    assert named == lanes_launch.session_name(str(first))


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
    assert second["command"] == typed(bridge, "codex", "--repo", str(seeded))


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
    monkeypatch.setattr(sys, "argv", lanes_argv(bridge, seeded))
    assert cli.main() == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].startswith("No terminal multiplexer on PATH")
    commands = printed[1:]
    assert len(commands) == 2
    numbers = set()
    for line, name in zip(commands, ("claude", "codex"), strict=True):
        assert line.startswith(typed(bridge, name, "--issue") + " ")
        assert line.endswith(f"--repo '{seeded}'")
        numbers.add(line.split("--issue ")[1].split()[0])
    assert numbers == {"43", "44"}


def test_status_names_the_running_session(tmux, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_SESSION", "0")
    session = lanes_launch.session_name("/work/my.repo")
    assert session.startswith("my-repo-")
    assert lanes_launch.attach_line("/work/my.repo") == (
        f"Lanes session: tmux attach -t {session}"
    )
    monkeypatch.setenv("FAKE_TMUX_SESSION", "1")
    assert lanes_launch.attach_line("/work/my.repo") == ""
