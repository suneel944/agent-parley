"""Checks reservation overlap on checkouts that ignore case in file names.

Every case passes the flag explicitly, so the results do not depend on
whether the host file system is case-sensitive.
"""

import json
from pathlib import Path

import pytest

from agent_parley import checkpoints, roster, store
from agent_parley.state import write_json
from agent_parley.worktrees import git


@pytest.fixture
def lanes(bridge, paired):
    """Registers claude and codex and names the codex lane that edits.

    The manifest starts without ``case_insensitive``, because Git on a
    case-insensitive host such as macOS reports ``core.ignorecase`` true
    and setup records the flag there.
    """
    lane = Path(paired["lanes"]["codex"])
    path = lane.parent / "project.json"
    manifest = json.loads(path.read_text())
    manifest.pop("case_insensitive", None)
    write_json(path, manifest)
    store.initialize(bridge.home)
    actors = {
        name: store.authenticate(
            bridge.home,
            store.register(bridge.home, paired["root"], name)[
                "registration_token"
            ],
        )
        for name in ("claude", "codex")
    }
    write_json(lane.parent / "codex-identity.json", {"name": "codex"})
    (lane / "src").mkdir(exist_ok=True)
    (lane / "src" / "parser.py").write_text("")
    return {"actors": actors, "lane": lane, "directory": lane.parent}


def fold_case(lanes):
    """Records the project as case-insensitive in its manifest."""
    path = lanes["directory"] / "project.json"
    manifest = json.loads(path.read_text())
    write_json(path, {**manifest, "case_insensitive": True})


def reserve(bridge, lanes, name, path, exclusive=False):
    """Asks for one reservation as one lane and returns the result."""
    return store.call(
        bridge.home,
        lanes["actors"][name],
        "file_reservation_paths",
        {"paths": [path], "ttl_seconds": 300, "exclusive": exclusive},
    )


def write(bridge, lanes):
    """Runs the codex PreToolUse decision for a write to src/parser.py."""
    return checkpoints.checkpoint(
        bridge.home,
        lanes["directory"],
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(lanes["lane"]),
            "session_id": "s1",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(lanes["lane"] / "src" / "parser.py"),
                "content": "x",
            },
            "tool_use_id": "call",
        },
    ).get("hookSpecificOutput", {})


@pytest.mark.parametrize(
    ("pattern", "other"),
    [
        ("README.md", "readme.md"),
        ("src/", "SRC/x.py"),
        ("SRC/*.py", "src/parser.py"),
    ],
)
def test_folded_keys_overlap_and_exact_keys_do_not(pattern, other):
    assert store.overlapping(pattern, other, fold=True)
    assert store.overlapping(other, pattern, fold=True)
    assert not store.overlapping(pattern, other)


def test_named_resources_stay_exact_when_folding():
    assert not store.overlapping("port:5432", "PORT:5432", fold=True)
    assert store.overlapping("port:5432", "port:5432", fold=True)


def test_folded_paths_report_the_spelling_given():
    paths = ["README.md", "SRC/x.py", "docs/a.md", "port:1"]
    patterns = ["readme.md", "src/", "PORT:1"]

    assert store.overlapping_paths(paths, patterns, fold=True) == {
        "README.md",
        "SRC/x.py",
    }
    assert store.overlapping_paths(paths, patterns) == set()


def test_a_case_variant_lane_prefix_maps_to_a_key_only_when_folding(
    tmp_path,
):
    lane = (tmp_path / "Lane").resolve()
    variant = lane.parent / "lANE" / "src" / "Parser.py"
    payload = {"tool_input": {"file_path": str(variant)}}

    assert checkpoints.touched_path(payload, lane, fold=True) == (
        "src/Parser.py"
    )
    assert checkpoints.touched_path(payload, lane) == ""


def test_a_case_variant_reservation_conflicts_only_when_folding(bridge, lanes):
    assert reserve(bridge, lanes, "claude", "src/Parser.py", True)["granted"]
    assert reserve(bridge, lanes, "codex", "src/parser.py", True)["granted"]
    store.call(
        bridge.home, lanes["actors"]["codex"], "release_file_reservations", {}
    )
    fold_case(lanes)

    refused = reserve(bridge, lanes, "codex", "src/parser.py", True)

    assert refused["granted"] == []
    assert refused["conflicts"][0]["owner"] == "claude"


def test_a_case_variant_peer_reservation_is_surfaced_when_folding(
    bridge, lanes
):
    assert reserve(bridge, lanes, "claude", "SRC/")["granted"]
    fold_case(lanes)

    notice = write(bridge, lanes)

    assert "permissionDecision" not in notice
    assert "claude reserves SRC" in notice["additionalContext"]


def test_a_new_manifest_records_what_git_reports_for_case(bridge, repo):
    git(repo, "config", "core.ignorecase", "true")
    added = bridge.add_participant(repo, "claude", "claude")
    directory = Path(added["lanes"]["claude"]).parent

    assert roster.case_insensitive(roster.read(directory))


def test_a_case_sensitive_repository_records_no_flag(bridge, repo):
    git(repo, "config", "core.ignorecase", "false")
    added = bridge.add_participant(repo, "claude", "claude")
    directory = Path(added["lanes"]["claude"]).parent

    assert "case_insensitive" not in roster.read(directory)
