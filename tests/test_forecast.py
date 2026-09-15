"""Checks the co-change forecast a reservation and a claim carry."""

import json
import subprocess
from pathlib import Path

from agent_parley import forecast, forge, store
from agent_parley.cli import git


def commit(repo: Path, message: str, *names: str) -> None:
    """Writes and commits the named files as one change in the fixture repo."""
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(f"{message}\n")
    git(repo, "add", "--", *names)
    git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )


def seed(repo: Path) -> None:
    """Commits a history where api.py and schema.sql move together."""
    for index in range(3):
        commit(repo, f"api and schema {index}", "src/api.py", "db/schema.sql")
    commit(repo, "api alone", "src/api.py", "docs/notes.md")


def lane(bridge, root, name):
    """Registers one served identity with the store."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    holder = store.authenticate(bridge.home, token)
    assert holder is not None
    return holder


def reserve(bridge, holder, *keys):
    """Reserves keys through the served tool path."""
    return store.call(
        bridge.home, holder, "file_reservation_paths", {"paths": list(keys)}
    )


def test_history_builds_the_co_change_table_from_known_commits(repo, tmp_path):
    seed(repo)
    commits = forecast.history(str(repo), tmp_path / "state")
    assert commits[0] == ["docs/notes.md", "src/api.py"]
    assert commits.count(["db/schema.sql", "src/api.py"]) == 3
    assert commits[-1] == ["shared.txt"]
    counts = forecast.cochanges(commits, ["src/api.py"], store.overlapping)
    assert counts == {"db/schema.sql": 3}
    assert forecast.cochanges(
        commits, ["src/api.py"], store.overlapping, threshold=1
    ) == {"db/schema.sql": 3, "docs/notes.md": 1}
    assert (
        forecast.cochanges(commits, ["src/*"], store.overlapping, threshold=4)
        == {}
    )
    assert forecast.cochanges(commits, ["port:5432"], store.overlapping) == {}


def test_the_cache_is_reused_until_the_base_commit_moves(
    repo, tmp_path, monkeypatch
):
    seed(repo)
    directory = tmp_path / "state"
    first = forecast.history(str(repo), directory)
    cached = json.loads((directory / forecast.CACHE).read_text())
    assert cached["base"] == git(repo, "rev-parse", "HEAD")
    assert cached["commits"] == first
    executed = []
    real_git = forecast._git

    def record(root, *args):
        executed.append(args[0])
        return real_git(root, *args)

    monkeypatch.setattr(forecast, "_git", record)
    assert forecast.history(str(repo), directory) == first
    assert executed == ["rev-parse"]
    commit(repo, "schema alone", "db/schema.sql")
    assert forecast.history(str(repo), directory)[0] == ["db/schema.sql"]
    assert executed == ["rev-parse", "rev-parse", "log"]


def test_a_git_failure_or_timeout_yields_no_forecast(
    repo, tmp_path, monkeypatch
):
    seed(repo)

    def timing_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 0))

    def missing(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(forecast.subprocess, "run", timing_out)
    assert forecast.history(str(repo), tmp_path / "state") == []
    monkeypatch.setattr(forecast.subprocess, "run", missing)
    assert forecast.history(str(repo), tmp_path / "state") == []
    assert forecast.history(str(tmp_path / "absent"), tmp_path / "state") == []


def test_a_peer_reserved_co_changed_path_is_forecast_on_grant(
    bridge, repo, paired
):
    seed(repo)
    claude = lane(bridge, paired["root"], "claude")
    codex = lane(bridge, paired["root"], "codex")
    assert reserve(bridge, codex, "db/schema.sql")["granted"]
    result = reserve(bridge, claude, "src/api.py")
    assert [entry["path"] for entry in result["granted"]] == ["src/api.py"]
    assert result["conflicts"] == []
    assert result["forecast"] == [
        {"path": "db/schema.sql", "peer": "codex", "count": 3}
    ]
    store.call(bridge.home, codex, "release_file_reservations", {})
    assert reserve(bridge, codex, "docs/notes.md")["granted"]
    unreserved = reserve(bridge, claude, "src/api.py")
    assert unreserved["granted"] and "forecast" not in unreserved


def test_the_forecast_is_bounded(monkeypatch):
    counts = {f"path{i:03}.py": 5 for i in range(40)}
    held = {"codex": ["path*.py"]}
    likely = forecast.collisions(counts, held, store.overlapping)
    assert len(likely) == forecast.MAX_FORECAST
    assert likely[0] == {"path": "path000.py", "peer": "codex", "count": 5}
    monkeypatch.setattr(forecast, "MAX_FORECAST", 2)
    assert len(forecast.collisions(counts, held, store.overlapping)) == 2


def test_a_claim_forecasts_from_earlier_pull_request_paths(
    bridge, repo, paired, monkeypatch
):
    seed(repo)
    claude_lane = Path(paired["lanes"]["claude"])
    codex = lane(bridge, paired["root"], "codex")
    assert reserve(bridge, codex, "db/schema.sql")["granted"]
    asked = []
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_title", lambda directory, number: None
    )
    monkeypatch.setattr(
        "agent_parley.cli.forge.assign", lambda directory, number: True
    )
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_pull_request_paths",
        lambda directory, number: asked.append(number) or ["src/api.py"],
    )
    record = bridge.issue(claude_lane, "claim", "#433")
    assert asked == ["433"]
    assert record["owner"] == "claude"
    assert record["forecast"] == [
        {"path": "db/schema.sql", "peer": "codex", "count": 3}
    ]
    assert "forecast" not in bridge.issue(repo, "list")["issues"]["433"]
    monkeypatch.setattr(
        "agent_parley.cli.forge.issue_pull_request_paths",
        lambda directory, number: [],
    )
    silent = bridge.issue(claude_lane, "claim", "434")
    assert silent["owner"] == "claude" and "forecast" not in silent


def test_a_claim_without_a_forge_reads_nothing(
    bridge, repo, paired, monkeypatch
):
    monkeypatch.setattr(forge.shutil, "which", lambda command: None)
    executed = []
    real_run = subprocess.run

    def record(command, **kwargs):
        executed.append(command[0])
        return real_run(command, **kwargs)

    monkeypatch.setattr(forge.subprocess, "run", record)
    assert forge.issue_pull_request_paths(repo, "433") == []
    assert executed == ["git"]
    record_ = bridge.issue(Path(paired["lanes"]["claude"]), "claim", "433")
    assert "forecast" not in record_
    assert "gh" not in executed
