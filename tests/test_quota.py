import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    cli,
    issues,
    quota,
    roster,
    store,
    supervision,
)
from agent_parley.state import BridgeError

GROUP = {"credential": "shared", "adapter": "claude", "lanes": ["a", "b", "c"]}
WINDOW = {"hours": 5.0, "tokens": 1000, "threshold": 0.8}


@pytest.fixture
def shared(bridge, repo):
    roster.define_credential(
        bridge.home, "shared", os.environ["CLAUDE_CONFIG_DIR"], [], []
    )
    bridge.add_participant(repo, "claude-a", "claude", "shared")
    manifest = bridge.add_participant(repo, "claude-b", "claude", "shared")
    directory = Path(manifest["lanes"]["claude-a"]).parent
    return manifest, directory


def exhausted(directory, name, reset_at):
    supervision.record_capacity(
        directory,
        name,
        {
            "state": "exhausted",
            "observed_at": time.time(),
            "reset_at": reset_at,
            "source": "claude-session-record",
            "session_id": "session",
            "observation_id": f"refusal-{reset_at}",
        },
    )


def test_a_vendor_reset_parks_every_lane_on_the_credential_unestimated():
    parked = quota.decide(GROUP, {}, None, ("a", 2000.0), ["a", "b", "c"])
    assert sorted(parked) == ["a", "b", "c"]
    assert all(record["until"] == 2000.0 for record in parked.values())
    assert not any(record["estimated"] for record in parked.values())
    assert "a reached its rate limit" in parked["b"]["cause"]


def test_without_a_window_or_vendor_signal_nothing_parks():
    assert quota.decide(GROUP, {}, None, None, ["a", "b", "c"]) == {}


def test_the_soft_threshold_parks_the_lowest_priority_lanes_first():
    entry = quota.roll({}, WINDOW, {"a": 0, "b": 0, "c": 0}, 100.0)
    entry = quota.roll(entry, WINDOW, {"a": 500, "b": 300, "c": 50}, 200.0)
    assert entry["used"] == 850
    parked = quota.decide(GROUP, entry, WINDOW, None, ["c", "a", "b"])
    assert sorted(parked) == ["a", "c"]
    assert parked["a"]["estimated"] is True
    assert parked["a"]["until"] == 100.0 + 5 * 3600


def test_reaching_the_window_limit_parks_every_lane():
    entry = quota.roll({}, WINDOW, {"a": 0}, 100.0)
    entry = quota.roll(entry, WINDOW, {"a": 1200}, 200.0)
    parked = quota.decide(GROUP, entry, WINDOW, None, ["a", "b", "c"])
    assert sorted(parked) == ["a", "b", "c"]


def test_a_falling_count_carries_what_was_charged():
    entry = quota.roll({}, WINDOW, {"a": 100}, 100.0)
    entry = quota.roll(entry, WINDOW, {"a": 400}, 200.0)
    entry = quota.roll(entry, WINDOW, {"a": 50}, 300.0)
    assert entry["used"] == 300
    entry = quota.roll(entry, WINDOW, {"a": 80}, 400.0)
    assert entry["used"] == 330


def test_the_window_restarts_after_its_length_and_at_a_vendor_reset():
    entry = quota.roll({}, WINDOW, {"a": 0}, 100.0)
    entry = quota.roll(entry, WINDOW, {"a": 900}, 200.0)
    later = quota.roll(entry, WINDOW, {"a": 950}, 100.0 + 5 * 3600)
    assert later["started"] == 100.0 + 5 * 3600
    assert later["used"] == 50
    seen = quota.roll({**entry, "vendor_reset": 250.0}, WINDOW, {"a": 920}, 300)
    assert seen["started"] == 250.0
    assert seen["used"] == 20


def test_headroom_marks_derived_figures_estimated_and_vendor_ones_not():
    entry = quota.roll({}, WINDOW, {"a": 0}, 100.0)
    entry = quota.roll(entry, WINDOW, {"a": 300}, 200.0)
    ledger = {
        "credentials": {
            "claude:shared": {
                **GROUP,
                "window": WINDOW,
                "entry": entry,
                "reported": None,
            },
            "codex:other": {
                "credential": "other",
                "adapter": "codex",
                "lanes": ["x"],
                "window": None,
                "entry": {},
                "reported": ["x", 5000.0],
            },
            "codex:plain": {
                "credential": "plain",
                "adapter": "codex",
                "lanes": ["y"],
                "window": None,
                "entry": {},
                "reported": None,
            },
        },
        "parked": {"x": {"until": 5000.0, "cause": "c"}},
    }
    rows = {row["credential"]: row for row in quota.headroom(ledger, 300.0)}
    assert rows["shared"]["estimated"] is True
    assert rows["shared"]["remaining"] == 700
    assert rows["shared"]["state"] == "available"
    assert rows["other"]["estimated"] is False
    assert rows["other"]["remaining"] == 0
    assert rows["other"]["state"] == "exhausted"
    assert rows["other"]["parked"] == ["x"]
    assert rows["plain"]["remaining"] is None
    assert rows["plain"]["state"] == "unknown"
    text = quota.lines(list(rows.values()))
    assert any("estimated 700 tokens left of 1000" in line for line in text)
    after = {row["credential"]: row for row in quota.headroom(ledger, 6000)}
    assert after["other"]["state"] == "unknown"
    assert after["other"]["parked"] == []


def test_a_window_needs_a_defined_profile_and_usable_values(bridge):
    with pytest.raises(BridgeError):
        quota.set_window(bridge.home, "missing", 5, 1000)
    roster.define_credential(
        bridge.home, "shared", os.environ["CLAUDE_CONFIG_DIR"], [], []
    )
    with pytest.raises(BridgeError):
        quota.set_window(bridge.home, "shared", 5, 1000, threshold=1.5)
    quota.set_window(bridge.home, "shared", 5, 1000, threshold=0.9)
    assert quota.windows(bridge.home)["shared"]["tokens"] == 1000
    quota.set_window(bridge.home, "shared", 0, 0)
    assert quota.windows(bridge.home) == {}


def test_a_rate_limit_on_one_lane_parks_its_credential_peer_in_place(
    bridge, shared
):
    manifest, directory = shared
    before = copy.deepcopy(manifest["participants"])
    store.initialize(bridge.home)
    actors = {}
    for name in ("claude-a", "claude-b"):
        display = manifest["participants"][name]["display"]
        token = store.register(bridge.home, manifest["root"], display)[
            "registration_token"
        ]
        actors[name] = store.authenticate(bridge.home, token)
    held = store.call(
        bridge.home,
        actors["claude-b"],
        "file_reservation_paths",
        {"paths": ["shared.txt"]},
    )
    assert held["granted"]
    issues.change(
        directory,
        "claude-b",
        "claim",
        "7",
        participants={"claude-a", "claude-b"},
    )
    reset_at = time.time() + 900
    exhausted(directory, "claude-a", reset_at)

    quota.account(
        bridge.home, directory, manifest, supervision.published_capacity
    )

    parked = quota.held(directory, "claude-b")
    assert parked is not None
    assert parked["until"] == reset_at
    assert parked["estimated"] is False
    blocked, ready_at = supervision._wake_block(
        directory, "claude-b", {}, {}, 60
    )
    assert "parked on credential shared" in blocked
    assert ready_at == reset_at
    assert checkpoints.continuation_refused(directory, manifest, "claude-b")
    fitness = supervision.fit(bridge.home, directory, manifest, "claude-b", 60)
    assert fitness["checks"]["capacity"] is False
    current = json.loads((directory / "project.json").read_text())
    assert current["participants"] == before
    for name in ("claude-a", "claude-b"):
        assert current["participants"][name]["credential"] == "shared"
        assert current["participants"][name]["provider"] == "claude"
    assert issues.snapshot(directory)["issues"]["7"]["owner"] == "claude-b"
    active = store.active_reservations(bridge.home, manifest["root"])
    assert active[actors["claude-b"]["name"]] == ["shared.txt"]
    headroom = quota.headroom(quota.read(directory), time.time())
    assert headroom[0]["state"] == "exhausted"
    assert headroom[0]["parked"] == ["claude-a", "claude-b"]


def test_a_parked_lane_resumes_at_the_reset_without_an_operator(bridge, shared):
    manifest, directory = shared
    reset_at = time.time() + 900
    exhausted(directory, "claude-a", reset_at)
    quota.account(
        bridge.home, directory, manifest, supervision.published_capacity
    )
    assert quota.held(directory, "claude-b") is not None
    assert quota.held(directory, "claude-b", now=reset_at + 1) is None
    exhausted(directory, "claude-a", time.time() - 1)
    quota.account(
        bridge.home, directory, manifest, supervision.published_capacity
    )
    assert quota.held(directory, "claude-b") is None
    blocked, _ = supervision._wake_block(
        directory, "claude-b", {"process_alive": True}, {}, 60
    )
    assert "parked" not in blocked
    assert not checkpoints.continuation_refused(directory, manifest, "claude-b")


def test_default_account_lanes_share_no_quota(bridge, repo, paired):
    directory = Path(paired["lanes"]["claude"]).parent
    assert quota.accounts(bridge.home, paired) == {}
    ledger = quota.account(
        bridge.home, directory, paired, supervision.published_capacity
    )
    assert ledger["parked"] == {}


def test_quota_commands_record_a_window_and_show_headroom(
    bridge, shared, repo, monkeypatch, capsys
):
    manifest, directory = shared
    home = ["agent-parley", "--home", str(bridge.home)]
    monkeypatch.setattr(
        sys,
        "argv",
        [*home, "quota", "set", "shared", "--hours", "5", "--tokens", "900"],
    )
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["shared"]["tokens"] == 900
    quota.account(
        bridge.home, directory, manifest, supervision.published_capacity
    )
    monkeypatch.setattr(
        sys, "argv", [*home, "quota", "show", "--repo", str(repo), "--json"]
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "quota"
    [row] = document["credentials"]
    assert row["credential"] == "shared"
    assert row["limit"] == 900
    assert row["estimated"] is True
    monkeypatch.setattr(
        sys, "argv", [*home, "quota", "show", "--repo", str(repo)]
    )
    assert cli.main() == 0
    assert "shared (claude): available" in capsys.readouterr().out
