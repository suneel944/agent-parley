"""Checks that hook notices inject only what the lane was not yet given."""

import json
import re
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    children,
    convergence,
    issues,
    merges,
    protocol,
    recommend,
    store,
    supervision,
)
from agent_parley.state import write_json

OFFER = (
    "Continue authorized work already assigned to you: #1 (resume). Resume "
    "the current claim generation; delivery does not mark progress. "
    "Waiting, no need to re-check: #2."
)


@pytest.fixture
def lanes(bridge, paired):
    """Registers both lanes and names the codex lane the hooks decide for."""
    lane = Path(paired["lanes"]["codex"])
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    write_json(lane.parent / "codex-identity.json", {"name": "codex"})
    return {
        "lane": lane,
        "peer": Path(paired["lanes"]["claude"]),
        "directory": lane.parent,
    }


def hook(bridge, lanes, event, **extra):
    """Runs one codex checkpoint decision and returns its output."""
    payload = {
        "hook_event_name": event,
        "cwd": str(lanes["lane"]),
        "session_id": "s1",
        **extra,
    }
    return checkpoints.checkpoint(
        bridge.home, lanes["directory"], "codex", payload
    )


def text(output):
    """Returns the context or block reason a hook output injects."""
    details = output.get("hookSpecificOutput") or {}
    return str(output.get("reason") or details.get("additionalContext", ""))


def bump(lanes):
    """Advances the ledger revision without changing any issue."""
    path = lanes["directory"] / "issues.json"
    ledger = issues.snapshot(lanes["directory"])
    ledger["revision"] += 1
    write_json(path, ledger)


def offer(lanes, identifier):
    """Republishes the same continuation text under a new offer id."""
    write_json(
        lanes["directory"] / "codex-work.json",
        {"offer": {"id": identifier, "kind": "continue", "text": OFFER}},
    )


def activity(lanes):
    """Reads the codex lane's activity record."""
    return json.loads((lanes["directory"] / "codex-activity.json").read_text())


def test_an_unchanged_claim_set_injects_nothing_new_on_the_next_stop(
    bridge, lanes
):
    bridge.issue(lanes["lane"], "claim", "1")
    first = hook(bridge, lanes, "Stop")
    assert first["decision"] == "block"
    assert "#1: codex" in first["reason"]

    bump(lanes)
    assert hook(bridge, lanes, "Stop") == {}
    bump(lanes)
    assert hook(bridge, lanes, "UserPromptSubmit", prompt="go") == {}
    assert activity(lanes)["injections"] == 1


def test_a_change_injects_only_the_changed_entries(bridge, lanes):
    bridge.issue(lanes["lane"], "claim", "1")
    bridge.issue(lanes["lane"], "claim", "2")
    first = text(hook(bridge, lanes, "UserPromptSubmit", prompt="go"))
    assert "#1: codex" in first and "#2: codex" in first

    bridge.issue(lanes["peer"], "claim", "3")
    changed = text(hook(bridge, lanes, "UserPromptSubmit", prompt="go"))

    assert "#3: claude" in changed
    assert "#1: codex" not in changed
    assert "#2: codex" not in changed
    assert "unchanged notice entries omitted" in changed
    assert f"`{protocol.cli_command()} status`" in changed
    assert "Run agent-parley" not in changed


def test_a_compaction_resends_everything_once(bridge, lanes):
    bridge.issue(lanes["lane"], "claim", "1")
    offer(lanes, "a")
    assert "#1: codex" in text(
        hook(bridge, lanes, "UserPromptSubmit", prompt="go")
    )
    bump(lanes)
    offer(lanes, "b")
    assert hook(bridge, lanes, "UserPromptSubmit", prompt="go") == {}

    resent = text(hook(bridge, lanes, "SessionStart", source="compact"))

    assert "#1: codex" in resent
    assert OFFER in resent
    assert hook(bridge, lanes, "UserPromptSubmit", prompt="go") == {}


def test_entries_past_the_budget_are_named_and_delivered_next():
    sections = [("issues", "\n".join(f"#{n}: codex" for n in range(1, 9)))]

    parts, record, kinds = checkpoints.fresh_notices(sections, {}, 40)

    assert kinds == ["issues"]
    assert parts[0].split("\n") == [f"#{n}: codex" for n in range(1, 5)]
    assert parts[-1].startswith("4 more notice entries past")
    command = protocol.cli_command()
    assert (
        f"`{command} status` and `{command} issue list` report them"
        in parts[-1]
    )
    later, record, _ = checkpoints.fresh_notices(sections, record, 40)
    assert later[0].split("\n") == [f"#{n}: codex" for n in range(5, 9)]
    assert "4 unchanged notice entries omitted" in later[1]
    assert len(record["issues"]) == 8


def test_an_elapsed_age_alone_is_not_a_change():
    assert checkpoints.item_digest(
        "Handoff reminder unanswered 12s: Issue #639: ended."
    ) == checkpoints.item_digest(
        "Handoff reminder unanswered 950s: Issue #639: ended."
    )
    assert checkpoints.item_digest("#1: codex") != checkpoints.item_digest(
        "#1: claude"
    )


def test_injected_bytes_are_reported_per_hour():
    state: dict = {}
    checkpoints.count_injection(state, 600, now=3600 * 10)
    checkpoints.count_injection(state, 300, now=3600 * 11 + 5)
    checkpoints.count_injection(state, 50, now=3600 * 40)

    assert state["injected_bytes"] == 950
    assert list(state["injected_hourly"]) == ["40"]
    assert checkpoints.hourly_rate(state, now=3600 * 40) == 50
    assert checkpoints.hourly_rate({}, now=0) == 0
    spread = {"injected_hourly": {"10": 600, "11": 300}}
    assert checkpoints.hourly_rate(spread, now=3600 * 12) == 300


def test_a_replayed_night_injects_under_half_of_a_full_resend(bridge, lanes):
    bridge.issue(lanes["lane"], "claim", "1")
    bridge.issue(lanes["lane"], "claim", "2")
    events = [("UserPromptSubmit", {"prompt": "go"}), ("Stop", {})] * 6

    path = lanes["directory"] / "codex-activity.json"

    def replay(full):
        path.unlink(missing_ok=True)
        injected = []
        for index, (event, extra) in enumerate(events):
            bump(lanes)
            offer(lanes, f"{full}-{index}")
            if full and path.exists():
                state = json.loads(path.read_text())
                state.pop("delivered_items", None)
                write_json(path, state)
            injected.append(text(hook(bridge, lanes, event, **extra)))
        return injected

    resent = replay(True)
    deltas = replay(False)

    full_bytes = sum(len(item.encode()) for item in resent)
    delta_bytes = sum(len(item.encode()) for item in deltas)
    assert delta_bytes * 2 <= full_bytes
    given = {
        checkpoints.item_digest(entry)
        for entry in checkpoints.notice_items("\n".join(deltas))
    }
    for entry in checkpoints.notice_items("\n".join(resent)):
        assert checkpoints.item_digest(entry) in given, entry


CONVERGENCE = {
    "issue": "7",
    "stage": "escalate",
    "entry": {
        "failures": 3,
        "signature": "abc",
        "repeats": 3,
        "milestone": {"commit": ""},
    },
}
LANE_FACING = {
    "full state": lambda: checkpoints.FULL_STATE,
    "outage": lambda: checkpoints.OUTAGE_CHECK,
    "issue state": lambda: checkpoints.ISSUE_STATE,
    "pull": lambda: supervision._pull_text(["7"], ["codex"]),
    "rebalance": lambda: supervision._rebalance_text(["7"], ["codex"]),
    "split": lambda: supervision._split_text("7", 3, ["codex"]),
    "continue": lambda: supervision._continue_text(
        {"issues": {}},
        [],
        [],
        {"next": "7", "stalled": {"issue": "8", "owner": "a"}},
    ),
    "capped": lambda: supervision._continue_text(
        {"issues": {}}, [], [], {"capped": ["9"]}
    ),
    "retire": lambda: supervision._continue_text({"issues": {}}, [], [], {}),
    "orphan remedy": lambda: supervision.orphan_remedy("7", "/r", True),
    "convergence": lambda: convergence.message(CONVERGENCE)[1],
    "dependency": lambda: merges.dependency_refusal(
        {"issues": {"7": {"blocked_by": ["8"]}, "8": {"owner": "a"}}},
        "7",
        set(),
    ),
    "recommend": lambda: recommend.render(
        {"candidates": [{"issue": "7", "title": "", "reasons": ["free"]}]}
    ),
    "overdue": lambda: supervision._overdue_text("7", "is overdue by 9s"),
    "bounced share": lambda: supervision._bounce_text(
        {
            "message_id": 3,
            "subject": "review",
            "blocked": [{"recipient": "codex", "reason": "has no session"}],
        }
    ),
    "dead mail": lambda: supervision._dead_mail_text(
        "codex has been dead for 9s and no wake revived it",
        "- message 3 (review)",
    ),
    "child brief": lambda: children.brief("claude", "b-c1", ["src"], "go"),
}


@pytest.mark.parametrize("builder", LANE_FACING.values(), ids=LANE_FACING)
def test_a_lane_facing_notice_names_no_bare_bridge_command(builder):
    text = builder()
    assert text
    assert not re.search(
        r"(?<![\w/.-])agent-parley (issue|status|report|participant)\b", text
    )
    assert protocol.cli_command() in text or "next_issues" in text
