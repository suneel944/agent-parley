"""Checks the co-change forecast and the merge forecast between lanes."""

import contextlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from agent_parley import cli, dashboard, forecast, forge, store
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


LINES = [f"line {number}\n" for number in range(1, 11)]


def spread(repo, manifest):
    """Commits a ten-line file to the base and fast-forwards every lane."""
    (repo / "big.txt").write_text("".join(LINES[:-1]))
    commit(repo, LINES[-1].strip(), "big.txt")
    base = git(repo, "symbolic-ref", "--short", "HEAD").strip()
    for checkout in manifest["lanes"].values():
        git(Path(checkout), "merge", "--ff-only", base)
    return base


def edit(checkout, *numbers):
    """Rewrites the named lines of big.txt in a lane and commits them."""
    checkout = Path(checkout)
    lines = list(LINES)
    for number in numbers:
        lines[number - 1] = f"{checkout.name} line {number}\n"
    (checkout / "big.txt").write_text("".join(lines))
    git(checkout, "add", "big.txt")
    git(
        checkout,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        f"edit {numbers}",
    )
    return git(checkout, "rev-parse", "HEAD").strip()


def untouched(manifest):
    """Reads every reference, index and worktree state the project has."""
    seen = [git(Path(manifest["root"]), "for-each-ref")]
    for checkout in [manifest["root"], *manifest["lanes"].values()]:
        seen.append(git(Path(checkout), "status", "--porcelain=v1"))
        seen.append(git(Path(checkout), "ls-files", "--stage"))
        seen.append(git(Path(checkout), "rev-parse", "HEAD"))
    return seen


def inbox(bridge, holder, subject):
    """Counts one lane's inbox messages carrying a subject."""
    messages = store.call(
        bridge.home, holder, "fetch_inbox", {"include_bodies": True}
    )["messages"]
    return sum(1 for message in messages if message["subject"] == subject)


def test_a_clean_pair_is_merged_and_predicts_nothing(bridge, repo, paired):
    spread(repo, paired)
    edit(paired["lanes"]["claude"], 1)
    edit(paired["lanes"]["codex"], 9)
    before = untouched(paired)
    directory = Path(paired["lanes"]["claude"]).parent
    taken = forecast.reading(paired["root"], paired, {}, directory)
    assert taken["pairs"] == []
    assert list(taken["merges"].values()) == [[]]
    assert taken["lanes"]["claude"]["paths"] == ["big.txt"]
    assert untouched(paired) == before


def test_a_conflicting_pair_is_shown_and_each_lane_told_once(
    bridge, repo, paired, capsys
):
    spread(repo, paired)
    claude = lane(bridge, paired["root"], "claude")
    codex = lane(bridge, paired["root"], "codex")
    edit(paired["lanes"]["claude"], 2)
    edit(paired["lanes"]["codex"], 2)
    directory = Path(paired["lanes"]["claude"]).parent
    before = untouched(paired)
    forecast.supervise(bridge.home, directory, paired, {})
    forecast.supervise(bridge.home, directory, paired, {})
    assert untouched(paired) == before
    published = forecast.read(directory)
    [pair] = published["pairs"]
    assert pair["lanes"] == ["claude", "codex"]
    assert pair["files"] == [{"path": "big.txt", "ranges": [[2, 6]]}]
    assert published["seconds"] >= 0
    subject = "Merge conflict predicted with codex"
    assert inbox(bridge, claude, subject) == 1
    assert inbox(bridge, codex, "Merge conflict predicted with claude") == 1
    edit(paired["lanes"]["codex"], 2, 3)
    forecast.supervise(bridge.home, directory, paired, {})
    assert inbox(bridge, claude, subject) == 1
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    marker = rows["claude"]["merge_conflict"]
    assert marker.startswith(
        "merge with codex predicted to conflict in big.txt lines "
    )
    assert marker.endswith("; nothing was merged")
    assert rows["codex"]["merge_conflict"].startswith("merge with claude")
    assert f"! claude  {marker}" in dashboard.render(view)
    bridge.status(cli.Selection(participant="claude"))
    assert marker in capsys.readouterr().out
    edit(paired["lanes"]["codex"], 9)
    forecast.supervise(bridge.home, directory, paired, {})
    assert forecast.read(directory)["pairs"] == []
    edit(paired["lanes"]["codex"], 2)
    forecast.supervise(bridge.home, directory, paired, {})
    assert inbox(bridge, claude, subject) == 2


def test_disabled_prompts_publish_the_forecast_and_tell_nobody(
    bridge, repo, paired
):
    spread(repo, paired)
    claude = lane(bridge, paired["root"], "claude")
    edit(paired["lanes"]["claude"], 2)
    edit(paired["lanes"]["codex"], 2)
    directory = Path(paired["lanes"]["claude"]).parent
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    assert len(forecast.read(directory)["pairs"]) == 1
    assert inbox(bridge, claude, "Merge conflict predicted with codex") == 0


def test_the_plan_lands_the_order_that_avoids_a_second_conflict(
    bridge, repo, paired, monkeypatch, capsys
):
    manifest = bridge.add_participant(repo, "gemini", "gemini")
    spread(repo, manifest)
    lanes = manifest["lanes"]
    heads = {
        "claude": edit(lanes["claude"], 2, 9),
        "codex": edit(lanes["codex"], 2),
        "gemini": edit(lanes["gemini"], 9),
    }
    root = manifest["root"]
    directory = Path(lanes["claude"]).parent
    with contextlib.ExitStack() as stack:
        env = forecast.scratch(root, directory, stack)
        clean = {
            (ours, theirs): forecast.merge(
                root, heads[ours], heads[theirs], env
            )["clean"]
            for ours, theirs in (
                ("claude", "codex"),
                ("claude", "gemini"),
                ("codex", "gemini"),
            )
        }
    assert list(clean.values()) == [False, False, True]
    before = untouched(manifest)
    planned = forecast.plan(root, manifest, set(), [], directory)
    assert [entry["lane"] for entry in planned["order"]] == [
        "codex",
        "gemini",
        "claude",
    ]
    assert [entry["clean"] for entry in planned["order"]] == [
        True,
        True,
        False,
    ]
    assert planned["conflicts"] == 1
    assert planned["order"][2]["conflicts_with"] == ["codex", "gemini"]
    assert "predicted to conflict" in planned["order"][2]["reason"]
    assert untouched(manifest) == before
    command = ["agent-parley", "--home", str(bridge.home), "merge", "plan"]
    monkeypatch.setattr(sys, "argv", [*command, "--repo", str(repo)])
    assert cli.main() == 0
    text = capsys.readouterr().out
    assert "this claims and merges nothing" in text
    assert "1. codex on " in text
    assert "Predicted conflicts in this order: 1." in text
    monkeypatch.setattr(sys, "argv", [*command, "--repo", str(repo), "--json"])
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "merge_plan"
    assert document["conflicts"] == 1
    assert untouched(manifest) == before


def test_eight_lanes_cost_two_git_calls_once_nothing_moved(
    bridge, repo, paired, monkeypatch
):
    manifest = paired
    for index in range(6):
        manifest = bridge.add_participant(repo, f"claude-{index}", "claude")
    assert len(manifest["lanes"]) == 8
    spread(repo, manifest)
    for number, checkout in enumerate(manifest["lanes"].values(), 1):
        edit(checkout, number)
    directory = Path(manifest["lanes"]["claude"]).parent
    calls = []
    real_run = forecast.subprocess.run

    def record(command, **kwargs):
        calls.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(forecast.subprocess, "run", record)
    forecast.supervise(bridge.home, directory, manifest, {"prompts": False})
    first = forecast.read(directory)
    assert len(first["merges"]) == 28
    assert len(first["pairs"]) == 7
    assert Counter(command[3] for command in calls) == {
        "rev-parse": 2,
        "for-each-ref": 1,
        "merge-base": 8,
        "diff": 8,
        "merge-tree": 28,
        "cat-file": 7,
    }
    assert len(calls) == 54
    calls.clear()
    forecast.supervise(bridge.home, directory, manifest, {"prompts": False})
    assert [command[3] for command in calls] == ["rev-parse", "for-each-ref"]
    assert forecast.read(directory)["merges"] == first["merges"]


def objects_in(repo):
    """Lists every file in the target repository's object store."""
    return sorted(
        path
        for path in (repo / ".git" / "objects").rglob("*")
        if path.is_file()
    )


def conflicting(bridge, repo, manifest, *names):
    """Adds the named lanes and commits one line 2 edit in every lane."""
    for name in names:
        manifest = bridge.add_participant(repo, name, name)
    spread(repo, manifest)
    for checkout in manifest["lanes"].values():
        edit(checkout, 2)
    return manifest, Path(manifest["lanes"]["claude"]).parent


def test_an_untested_pair_keeps_its_prediction_and_is_not_mailed_again(
    bridge, repo, paired, monkeypatch
):
    manifest, directory = conflicting(bridge, repo, paired, "gemini")
    holders = {
        name: lane(bridge, paired["root"], name)
        for name in ("claude", "codex", "gemini")
    }
    forecast.supervise(bridge.home, directory, manifest, {})
    first = forecast.read(directory)
    assert len(first["pairs"]) == 3
    monkeypatch.setattr(forecast, "MAX_MERGES", 1 + forecast.MAX_FILES)
    edit(manifest["lanes"]["codex"], 2, 3)
    edit(manifest["lanes"]["gemini"], 2, 4)
    forecast.supervise(bridge.home, directory, manifest, {})
    second = forecast.read(directory)
    assert [pair["lanes"] for pair in second["pairs"]] == [
        ["claude", "codex"],
        ["claude", "gemini"],
        ["codex", "gemini"],
    ]
    assert len(second["merges"]) == 1
    assert second["pairs"][0]["heads"] != first["pairs"][0]["heads"]
    assert second["pairs"][0]["since"] == first["pairs"][0]["since"]
    assert second["pairs"][1:] == first["pairs"][1:]
    for name, holder in holders.items():
        for peer in holders:
            subject = f"Merge conflict predicted with {peer}"
            assert inbox(bridge, holder, subject) == (peer != name)


def test_a_merge_git_cannot_answer_ends_the_pass_and_is_not_retried(
    bridge, repo, paired, monkeypatch
):
    manifest, directory = conflicting(bridge, repo, paired, "gemini")
    attempts = []
    monkeypatch.setattr(
        forecast,
        "merge",
        lambda root, ours, theirs, env: attempts.append(ours),
    )
    forecast.supervise(bridge.home, directory, manifest, {"prompts": False})
    assert len(attempts) == 1
    published = forecast.read(directory)
    assert published["pairs"] == [] and published["merges"] == {}
    assert len(published["failed"]) == 3
    forecast.supervise(bridge.home, directory, manifest, {"prompts": False})
    assert len(attempts) == 1
    commit(repo, "base moves", "other.txt")
    forecast.supervise(bridge.home, directory, manifest, {"prompts": False})
    assert len(attempts) == 2


def test_a_moved_base_rereads_every_lane_and_keeps_merges(
    bridge, repo, paired, monkeypatch
):
    spread(repo, paired)
    edit(paired["lanes"]["claude"], 2)
    edit(paired["lanes"]["codex"], 9)
    directory = Path(paired["lanes"]["claude"]).parent
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    first = forecast.read(directory)
    commit(repo, "base moves", "other.txt")
    calls = []
    real_run = forecast.subprocess.run

    def record(command, **kwargs):
        calls.append(command[3])
        return real_run(command, **kwargs)

    monkeypatch.setattr(forecast.subprocess, "run", record)
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    second = forecast.read(directory)
    assert second["base"] != first["base"]
    assert {entry["base"] for entry in second["lanes"].values()} == {
        second["base"]
    }
    assert Counter(calls) == {
        "rev-parse": 1,
        "for-each-ref": 1,
        "merge-base": 2,
        "diff": 2,
    }
    assert list(first["merges"].values()) == [[]]
    assert second["merges"] == first["merges"]


def test_a_retired_lane_is_never_read(bridge, repo, paired):
    spread(repo, paired)
    edit(paired["lanes"]["claude"], 2)
    edit(paired["lanes"]["codex"], 2)
    participants = paired["participants"]
    retired = {
        **paired,
        "participants": {
            **participants,
            "codex": {**participants["codex"], "retired": 1.0},
        },
    }
    directory = Path(paired["lanes"]["claude"]).parent
    taken = forecast.reading(paired["root"], retired, {}, directory)
    assert list(taken["lanes"]) == ["claude"]
    assert taken["merges"] == {} and taken["pairs"] == []
    assert len(forecast.reading(paired["root"], paired, {}, directory)["pairs"])


def test_a_branch_only_on_origin_is_read_from_there(bridge, repo, paired):
    base = spread(repo, paired)
    claude = edit(paired["lanes"]["claude"], 2)
    codex = edit(paired["lanes"]["codex"], 2)
    branch = paired["participants"]["claude"]["branch"]
    git(repo, "update-ref", "refs/remotes/origin/remote-only", codex)
    git(repo, "update-ref", f"refs/remotes/origin/{branch}", base)
    git(repo, "update-ref", "refs/tags/remote-only", base)
    participants = paired["participants"]
    moved = {
        **paired,
        "participants": {
            **participants,
            "codex": {**participants["codex"], "branch": "remote-only"},
        },
    }
    directory = Path(paired["lanes"]["claude"]).parent
    taken = forecast.reading(paired["root"], moved, {}, directory)
    assert taken["lanes"]["codex"]["head"] == codex
    assert taken["lanes"]["codex"]["branch"] == "remote-only"
    assert taken["lanes"]["claude"]["head"] == claude
    [pair] = taken["pairs"]
    assert pair["branches"] == [branch, "remote-only"]


def test_a_failed_merge_base_is_not_cached(bridge, repo, paired, monkeypatch):
    spread(repo, paired)
    edit(paired["lanes"]["claude"], 2)
    old = edit(paired["lanes"]["codex"], 2)
    directory = Path(paired["lanes"]["claude"]).parent
    real_git = forecast._git

    def failing(root, *args):
        return None if args[0] == "merge-base" else real_git(root, *args)

    monkeypatch.setattr(forecast, "_git", failing)
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    assert forecast.read(directory)["lanes"] == {}
    monkeypatch.setattr(forecast, "_git", real_git)
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    published = forecast.read(directory)
    assert sorted(published["lanes"]) == ["claude", "codex"]
    assert len(published["pairs"]) == 1
    edit(paired["lanes"]["codex"], 2, 3)
    monkeypatch.setattr(forecast, "_git", failing)
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    kept = forecast.read(directory)
    assert kept["lanes"]["codex"]["head"] == old
    assert kept["pairs"] == published["pairs"]
    monkeypatch.setattr(forecast, "_git", real_git)
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    assert forecast.read(directory)["lanes"]["codex"]["head"] != old


def test_the_plan_takes_pull_requests_and_reuses_the_forecast(
    bridge, repo, paired, tmp_path, monkeypatch
):
    base = spread(repo, paired)
    edit(paired["lanes"]["claude"], 2)
    edit(paired["lanes"]["codex"], 9)
    feature = tmp_path / "feature"
    git(repo, "worktree", "add", "-b", "pr-feature", str(feature), base)
    edit(feature, 2)
    directory = Path(paired["lanes"]["claude"]).parent
    forecast.supervise(bridge.home, directory, paired, {"prompts": False})
    pulls = [
        {"number": 5, "branch": paired["participants"]["claude"]["branch"]},
        {"number": 7, "branch": "pr-feature"},
        {"number": 9, "branch": "gone"},
    ]
    calls = []
    real_run = forecast.subprocess.run

    def record(command, **kwargs):
        calls.append(command[3])
        return real_run(command, **kwargs)

    monkeypatch.setattr(forecast.subprocess, "run", record)
    before = objects_in(repo)
    planned = forecast.plan(paired["root"], paired, {"codex"}, pulls, directory)
    assert [
        (entry["lane"], entry["pull_request"], entry["clean"])
        for entry in planned["order"]
    ] == [("codex", None, True), (None, 7, True), ("claude", 5, False)]
    assert planned["order"][2]["conflicts_with"] == ["#7"]
    assert planned["skipped"] == [
        {
            "pull_request": 9,
            "branch": "gone",
            "reason": "head branch not available locally",
        }
    ]
    assert calls.count("merge-base") == 1
    assert calls.count("merge-tree") == 2 + 3
    assert calls.count("commit-tree") == 2
    assert objects_in(repo) == before
    monkeypatch.setattr(forecast, "MAX_MERGES", 0)
    capped = forecast.plan(paired["root"], paired, {"codex"}, pulls, directory)
    assert capped["order"][2]["conflicts_with"] == []


def test_no_object_is_written_into_the_target_repository(bridge, repo, paired):
    manifest, directory = conflicting(bridge, repo, paired)
    before = objects_in(repo)
    forecast.supervise(bridge.home, directory, manifest, {"prompts": False})
    assert len(forecast.read(directory)["pairs"]) == 1
    forecast.plan(manifest["root"], manifest, set(), [], directory)
    assert objects_in(repo) == before
    assert list((directory / forecast.OBJECTS).iterdir()) == []
