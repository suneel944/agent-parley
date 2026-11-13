"""Checks the signal for lanes that message more than they make progress."""

import json
from pathlib import Path

from agent_parley import cli, metrics, stagnation, store
from agent_parley.cli import git

NAMES = [f"lane-{index}" for index in range(1, 7)]


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def six_lanes(bridge, repo):
    """Adds six lanes and has each send two messages, committing nothing."""
    for name in NAMES:
        data = bridge.add_participant(repo, name, "claude")
    holders = {
        name: actor(
            bridge,
            data["root"],
            data["participants"][name].get("display", name),
        )
        for name in NAMES
    }
    for index, name in enumerate(NAMES):
        peer = NAMES[(index + 1) % len(NAMES)]
        for turn in range(2):
            store.call(
                bridge.home,
                holders[name],
                "send_message",
                {
                    "to": [data["participants"][peer].get("display", peer)],
                    "subject": f"status {turn}",
                    "body_md": "still waiting",
                    "idempotency_key": f"{name}-{turn}",
                },
            )
    return data


def stagnation_lines(output):
    """Keeps the stagnation lines of a printed report."""
    return [line for line in output.splitlines() if "Stagnation:" in line]


def test_six_talking_lanes_show_one_line_and_one_commit_each_clears_it(
    bridge, repo, capsys
):
    data = six_lanes(bridge, repo)

    bridge.status(cli.Selection())
    lines = stagnation_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert "12 mail per 0 progress events" in lines[0]
    assert "lane-1 2/0" in lines[0]
    assert lines[0].endswith("Nothing was sent to lanes.")

    for name in NAMES:
        lane = Path(data["participants"][name]["lane"])
        git(
            lane,
            "-c",
            "user.name=Bridge Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            f"work on {name}",
        )
    bridge.status(cli.Selection())
    assert not stagnation_lines(capsys.readouterr().out)

    reading = metrics.throughput(bridge.home, 0)
    totals = reading["projects"][0]["totals"]
    assert totals["mail_sent"] == 12
    assert totals["progress"] == 6
    assert totals["mail_ratio"] == 2
    assert reading["projects"][0]["lanes"]["lane-1"]["mail_ratio"] == 2
    assert "Lane mail: 12 sent per 6 progress events, ratio 2" in (
        metrics.describe_throughput(reading)
    )
    snapshot = json.loads(json.dumps(bridge.status_snapshot()))
    assert snapshot["projects"][0]["stagnation"]["stagnant"] is False


def test_rising_progress_or_little_mail_never_reads_as_stagnant(
    tmp_path, monkeypatch
):
    windows = iter(
        [
            {"one": {"mail_sent": 30, "progress": 2}},
            {"one": {"mail_sent": 0, "progress": 1}},
        ]
    )
    monkeypatch.setattr(stagnation, "counts", lambda *args: next(windows))
    manifest = {"root": "/p", "participants": {"one": {}}}
    rising = stagnation.reading(None, tmp_path, manifest, {}, now=10_000)
    assert rising["ratio"] == 15
    assert not rising["stagnant"]
    assert stagnation.line(rising) == ""

    windows = iter(
        [
            {"one": {"mail_sent": 9, "progress": 0}},
            {"one": {"mail_sent": 0, "progress": 0}},
        ]
    )
    quiet = stagnation.reading(None, tmp_path, manifest, {}, now=10_000)
    assert not quiet["stagnant"]


def test_falling_progress_names_the_lanes_with_the_most_mail_per_event(
    tmp_path, monkeypatch
):
    windows = iter(
        [
            {
                "a": {"mail_sent": 20, "progress": 1},
                "b": {"mail_sent": 4, "progress": 0},
                "c": {"mail_sent": 0, "progress": 0},
            },
            {"a": {"mail_sent": 3, "progress": 2}},
        ]
    )
    monkeypatch.setattr(stagnation, "counts", lambda *args: next(windows))
    manifest = {"root": "/p", "participants": {"a": {}, "b": {}, "c": {}}}
    signal = stagnation.reading(None, tmp_path, manifest, {}, now=10_000)
    assert signal["stagnant"]
    assert [lane["lane"] for lane in signal["lanes"]] == ["a", "b"]
    assert stagnation.line(signal) == (
        "Stagnation: last 60 min, 24 mail per 1 progress events (ratio 24, "
        "threshold 10; previous 60 min 2 events); most mail per progress: "
        "a 20/1, b 4/0. Nothing was sent to lanes."
    )
