"""Checks the flag for two lanes converging on one branch or path set."""

import json
from pathlib import Path

from agent_parley import cli, collisions, store
from agent_parley.cli import git


def lane(branch="", paths=(), files=()):
    """Shapes one lane reading as `collisions.find` takes it."""
    return {"branch": branch, "paths": list(paths), "files": set(files)}


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def told(bridge, holder):
    """Counts the convergence notices in one lane's inbox."""
    inbox = store.call(bridge.home, holder, "fetch_inbox", {})
    if isinstance(inbox, str):
        inbox = json.loads(inbox)
    return [
        item
        for item in inbox["messages"]
        if item["subject"].startswith("Converging with")
    ]


def test_a_shared_branch_flags_the_pair_once_whatever_else_they_share():
    pairs = collisions.find(
        {
            "codex": lane("feature/loop", ["a.py", "b.py", "c.py"], ["n.py"]),
            "claude": lane("feature/loop", ["a.py", "b.py", "c.py"], ["n.py"]),
            "gemini": lane("other"),
        }
    )
    assert pairs == [
        {
            "lanes": ["claude", "codex"],
            "kind": "branch",
            "item": "feature/loop",
            "count": 1,
        }
    ]
    assert collisions.line(pairs[0]) == (
        "Converging: claude and codex share branch feature/loop; one of "
        "them should rename or re-plan. Nothing was refused."
    )
    assert collisions.peers(pairs, "codex") == ["claude on branch feature/loop"]


def test_a_detached_or_unreadable_branch_is_never_shared():
    assert not collisions.find(
        {"one": lane("<detached HEAD>"), "two": lane("<detached HEAD>")}
    )
    assert not collisions.find(
        {
            "one": lane("an unavailable worktree"),
            "two": lane("an unavailable worktree"),
        }
    )


def test_path_overlap_flags_only_past_the_fixed_threshold():
    two = collisions.find(
        {"one": lane("a", ["x.py", "y.py"]), "two": lane("b", ["x.py", "y.py"])}
    )
    assert not two
    spread = collisions.find(
        {
            "one": lane("a", ["a", "b", "c"]),
            "two": lane("b", ["a", "b", "c", *"defghij"]),
        }
    )
    assert spread[0]["kind"] == "paths"
    assert collisions.shared(spread[0]) == ("3 reserved paths, such as a")
    thin = collisions.find(
        {
            "one": lane("a", ["a", "b", "c", *"defghij"]),
            "two": lane("b", ["a", "b", "c", *"klmnopq"]),
        }
    )
    assert not thin


def test_a_new_file_in_both_plans_flags_the_pair(tmp_path):
    (tmp_path / "old.py").write_text("")
    plan = {
        "items": [
            {"text": "add agent_parley/new.py and edit old.py", "status": ""},
            {"text": "wrote done.py", "status": "completed"},
        ]
    }
    assert collisions.plan_files(tmp_path, plan) == {"agent_parley/new.py"}
    pairs = collisions.find(
        {
            "one": lane("a", files=["agent_parley/new.py"]),
            "two": lane("b", files=["agent_parley/new.py"]),
        }
    )
    assert collisions.shared(pairs[0]) == "new file agent_parley/new.py"


def test_prose_versions_bare_names_and_unreadable_paths_are_not_files(
    tmp_path, monkeypatch
):
    long = "d/" + "x" * 300 + ".py"
    exists = Path.exists

    def refusing(path, *args, **kwargs):
        if path.name.startswith("xxx"):
            raise OSError(36, "File name too long")
        return exists(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", refusing)
    plan = {
        "items": [
            {
                "text": (
                    "ship 0.23.0, e.g. status.py, i.e. tidy; see "
                    f"docs/v0.23 and {long}"
                ),
                "status": "",
            }
        ]
    }
    assert collisions.plan_files(tmp_path, plan) == set()


def test_a_child_lane_is_never_paired_with_its_parent():
    paths = ["a.py", "b.py", "c.py"]
    child = {**lane("feature/x", paths), "parent": "lead"}
    assert not collisions.find({"lead": lane("feature/x", paths), "kid": child})
    assert collisions.find({"peer": lane("feature/x", paths), "kid": child})


def test_a_shifting_path_set_is_told_once_and_a_failed_send_retries_alone(
    tmp_path, monkeypatch
):
    manifest = {
        "root": "/project",
        "participants": {"one": {"lane": "/one"}, "two": {"lane": "/two"}},
    }
    pair = {"lanes": ["one", "two"], "kind": "paths", "count": 3}
    readings = iter([{**pair, "item": "a.py"}, {**pair, "item": "b.py"}] * 2)
    monkeypatch.setattr(
        collisions, "read", lambda *args, **kwargs: [next(readings)]
    )
    sent = []
    failing = {"two"}

    def speak(home, root, name, subject, body, key):
        if name in failing:
            failing.discard(name)
            raise store.BridgeError("store busy")
        sent.append((name, key))

    monkeypatch.setattr(store, "speak", speak)
    for _ in range(4):
        collisions.supervise(tmp_path, tmp_path, manifest)
    assert [name for name, _ in sent] == ["one", "two"]
    assert sent[0][1].split(":")[1] == sent[1][1].split(":")[1]
    kept = json.loads((tmp_path / collisions.PUBLICATION).read_text())
    assert kept["one|two|paths"]["told"] == ["one", "two"]


def test_two_lanes_on_one_branch_show_one_line_and_clear_on_rename(
    bridge, repo, paired, capsys
):
    root = paired["root"]
    data = paired
    names = {
        name: participant.get("display", name)
        for name, participant in data["participants"].items()
    }
    holders = {name: actor(bridge, root, names[name]) for name in names}
    for holder in holders.values():
        store.call(
            bridge.home,
            holder,
            "file_reservation_paths",
            {"paths": ["shared.txt"], "exclusive": False},
        )
    claude, codex = (Path(paired["lanes"][name]) for name in names)
    git(claude, "switch", "-c", "feature/game-loop")
    git(codex, "switch", "--ignore-other-worktrees", "feature/game-loop")

    bridge.status(cli.Selection())
    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if "Converging:" in line]
    assert lines == [
        "Converging: claude and codex share branch feature/game-loop; one "
        "of them should rename or re-plan. Nothing was refused."
    ]
    assert "converging with codex on branch feature/game-loop" in output

    directory = bridge.project(repo)[1]
    collisions.supervise(bridge.home, directory, data)
    collisions.supervise(bridge.home, directory, data)
    assert [len(told(bridge, holder)) for holder in holders.values()] == [1, 1]

    git(codex, "switch", "-c", "feature/scoring")
    bridge.status(cli.Selection())
    assert "Converging:" not in capsys.readouterr().out
    collisions.supervise(bridge.home, directory, data)
    assert json.loads((directory / collisions.PUBLICATION).read_text()) == {}
