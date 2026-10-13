"""Checks that filing and editing surface a peer's overlapping work first."""

import shlex
from pathlib import Path

import pytest

from agent_parley import checkpoints, store, supervision
from agent_parley.state import write_json


@pytest.fixture
def lanes(bridge, paired):
    """Registers claude and codex and names the codex lane that edits."""
    lane = Path(paired["lanes"]["codex"])
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
    return {
        "actors": actors,
        "lane": lane,
        "directory": lane.parent,
        "root": paired["root"],
    }


def reserve(bridge, lanes, name, path):
    """Makes one lane hold a shared reservation on one path."""
    assert store.call(
        bridge.home,
        lanes["actors"][name],
        "file_reservation_paths",
        {"paths": [path], "ttl_seconds": 300, "exclusive": False},
    )["granted"]


def hook(bridge, lanes, tool, tool_input, key="call"):
    """Runs one codex PreToolUse decision."""
    return checkpoints.checkpoint(
        bridge.home,
        lanes["directory"],
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(lanes["lane"]),
            "session_id": "s1",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": key,
        },
    ).get("hookSpecificOutput", {})


def write(bridge, lanes, key="call"):
    """Runs the decision for a codex write to src/parser.py."""
    target = str(lanes["lane"] / "src" / "parser.py")
    return hook(
        bridge, lanes, "Write", {"file_path": target, "content": "x"}, key
    )


def file_issue(bridge, lanes, title, body, key="call"):
    """Runs the decision for a codex `gh issue create` call."""
    command = shlex.join(
        ["gh", "issue", "create", "--title", title, "--body", body]
    )
    return hook(bridge, lanes, "Bash", {"command": command}, key)


def test_a_write_on_a_shared_peer_reservation_is_surfaced_to_both_lanes(
    bridge, lanes
):
    reserve(bridge, lanes, "claude", "src/")

    first = write(bridge, lanes, "one")

    assert "permissionDecision" not in first
    assert "Before editing src/parser.py" in first["additionalContext"]
    assert "claude reserves src" in first["additionalContext"]
    told = store.call(
        bridge.home, lanes["actors"]["claude"], "fetch_inbox", {}
    )["messages"]
    assert any("codex is editing src/parser.py" in m["subject"] for m in told)
    again = write(bridge, lanes, "two")
    assert "Before editing" not in again.get("additionalContext", "")


def test_an_unreserved_write_is_reminded_once_and_never_refused(bridge, lanes):
    first = write(bridge, lanes, "one")

    assert "permissionDecision" not in first
    assert "not reserved by this lane" in first["additionalContext"]
    assert "not reserved" not in write(bridge, lanes, "two").get(
        "additionalContext", ""
    )


def test_a_reserved_write_carries_no_reminder(bridge, lanes):
    reserve(bridge, lanes, "codex", "src/parser.py")

    assert "not reserved" not in write(bridge, lanes).get(
        "additionalContext", ""
    )


def test_a_peer_pull_request_changing_the_file_is_named(bridge, lanes):
    write_json(
        lanes["directory"] / supervision.PULL_REQUEST_RECORD,
        {
            "read_at": 0,
            "pull_requests": {
                "9": {
                    "number": 9,
                    "branch": "fix/7-other",
                    "files": ["src/parser.py"],
                    "issues": ["7"],
                }
            },
        },
    )

    context = write(bridge, lanes)["additionalContext"]

    assert "pull request #9 (fix/7-other) changes src/parser.py" in context
    assert "closes #7" in context


def test_filing_an_issue_on_a_reserved_file_is_refused_once(bridge, lanes):
    reserve(bridge, lanes, "claude", "src/parser.py")
    body = "The guard in `src/parser.py:12` accepts a stale token."

    refused = file_issue(bridge, lanes, "bug: stale token", body, "one")

    assert refused["permissionDecision"] == "deny"
    reason = refused["permissionDecisionReason"]
    assert "Before filing this issue: claude reserves src/parser.py" in reason
    assert "Run the same command again" in reason
    summary = checkpoints.event_summary(lanes["directory"], "codex")
    assert any(
        entry["reason"] == "filing_overlap" for entry in summary["denied_by"]
    )
    again = file_issue(bridge, lanes, "bug: stale token", body, "two")
    assert "permissionDecision" not in again


def test_filing_an_issue_with_no_overlap_is_not_refused(bridge, lanes):
    result = file_issue(bridge, lanes, "docs: typo", "Fix a typo.")

    assert "permissionDecision" not in result


def test_named_paths_keep_only_lane_files(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("")

    found = checkpoints.named_paths(
        "See pkg/mod.py:81, /etc/passwd, ../x.py, 0.14.0 and pkg/mod.py.",
        tmp_path.resolve(),
    )

    assert found == ["pkg/mod.py"]


def test_a_filing_title_matching_a_peer_claim_names_it():
    snapshot = {
        "issues": {
            "601": {
                "title": "lane token refusal in integration base guard",
                "owner": "claude",
            },
            "602": {"title": "unrelated parser work", "owner": "codex"},
        }
    }

    found = checkpoints.claimed_matches(
        "integration base guard ignores the lane token", snapshot, "codex"
    )

    assert [item["key"] for item in found] == ["claim:601"]
    assert "held by claude" in found[0]["text"]
