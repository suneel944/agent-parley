"""Checks queued reservation requests and the grant a release performs."""

import time
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, dashboard, store
from agent_parley.state import BridgeError, write_json


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def reserve(bridge, holder, *keys, **arguments):
    """Reserves keys as one lane through the served tool path."""
    return store.call(
        bridge.home,
        holder,
        "file_reservation_paths",
        {"paths": list(keys), **arguments},
    )


def request(bridge, lane, *keys, **arguments):
    """Asks for keys, queueing behind whichever lane holds them."""
    return store.call(
        bridge.home,
        lane,
        "request_reservation",
        {"paths": list(keys), **arguments},
    )


def release(bridge, lane):
    """Releases every lease one lane holds."""
    return store.call(bridge.home, lane, "release_file_reservations", {})


def inbox(bridge, lane):
    """Reads one lane's own mail with bodies."""
    return store.call(
        bridge.home, lane, "fetch_inbox", {"include_bodies": True}
    )["messages"]


def expire(bridge, holder, seconds):
    """Backdates one lane's lease deadlines by a number of seconds."""
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts=datetime('now',?) "
            "WHERE agent_id=? AND released_ts IS NULL",
            (f"-{seconds} seconds", holder["id"]),
        )


def unreachable(bridge, holder):
    """Records the last observation of a lane as a gone session process."""
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "INSERT INTO participant_presence(agent_id,state,process_alive,"
            "observed_ts,last_active) VALUES (?,'stopped',0,?,NULL)",
            (holder["id"], time.time()),
        )


def held(bridge, holder):
    """Reports how many leases a lane holds and how many have expired."""
    with store.connect(bridge.home) as db:
        return dict(
            db.execute(
                "SELECT count(*) AS leases,coalesce(sum(expires_ts IS NOT "
                "NULL AND expires_ts<=CURRENT_TIMESTAMP),0) AS expired "
                "FROM file_reservations WHERE agent_id=? "
                "AND released_ts IS NULL",
                (holder["id"],),
            ).fetchone()
        )


def hook(bridge, paired, name):
    """Runs one lifecycle checkpoint for a lane and reports its output."""
    directory = Path(paired["lanes"][name]).parent
    write_json(directory / f"{name}-identity.json", {"name": name})
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "cwd": paired["lanes"][name],
        "tool_name": "Bash",
    }
    return checkpoints.checkpoint(bridge.home, directory, name, payload)


def test_a_free_key_is_granted_without_queueing(bridge, repo, paired):
    lane = actor(bridge, paired["root"], "claude")
    result = request(bridge, lane, "src/engine.py")
    assert [lease["path"] for lease in result["granted"]] == ["src/engine.py"]
    assert result["queued"] == []


def test_a_held_key_queues_behind_its_holder(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", reason="refactor")
    result = request(bridge, peer, "src/engine.py")
    assert result["granted"] == []
    assert result["conflicts"] == [
        {"path": "src/engine.py", "owner": "claude", "reason": "refactor"}
    ]
    assert result["queued"] == [
        {
            "id": result["queued"][0]["id"],
            "path": "src/engine.py",
            "owner": "claude",
            "position": 1,
        }
    ]
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "claude": ["src/engine.py"]
    }


def test_a_release_grants_and_notifies_the_first_queued_lane(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", "docs/guide.md")
    request(bridge, peer, "src/engine.py", reason="finish the rename")
    released = release(bridge, holder)
    assert released["released"] == 2
    assert released["granted"] == [
        {
            "agent": "codex",
            "paths": ["src/engine.py"],
            "message_id": released["granted"][0]["message_id"],
        }
    ]
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "codex": ["src/engine.py"]
    }
    notice = inbox(bridge, peer)[0]
    assert notice["sender"] == "claude"
    assert notice["subject"] == "Reservation granted: src/engine.py"
    assert "nothing on disk is locked" in notice["body_md"]
    assert notice["id"] == released["granted"][0]["message_id"]
    assert request(bridge, peer, "src/engine.py")["granted"]


def test_the_grant_and_the_notice_share_the_release_transaction(
    bridge, repo, paired, monkeypatch
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py")
    request(bridge, peer, "src/engine.py")

    def unavailable(*args, **kwargs):
        raise BridgeError("mail is unavailable")

    monkeypatch.setattr(store, "_send", unavailable)
    with pytest.raises(BridgeError, match="mail is unavailable"):
        release(bridge, holder)
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "claude": ["src/engine.py"]
    }
    monkeypatch.undo()
    assert release(bridge, holder)["granted"][0]["agent"] == "codex"


def test_only_the_first_queued_lane_takes_the_released_key(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    first = actor(bridge, paired["root"], "codex")
    second = actor(bridge, paired["root"], "gemini")
    reserve(bridge, holder, "src/engine.py")
    assert request(bridge, first, "src/engine.py")["queued"][0]["position"] == 1
    queued = request(bridge, second, "src/engine.py")["queued"][0]
    assert queued["position"] == 2
    granted = release(bridge, holder)["granted"]
    assert [entry["agent"] for entry in granted] == ["codex"]
    waiting = request(bridge, second, "src/engine.py")
    assert waiting["granted"] == []
    assert waiting["queued"] == [
        {
            "id": queued["id"],
            "path": "src/engine.py",
            "owner": "codex",
            "position": 1,
        }
    ]


def test_asking_again_keeps_the_first_request_and_its_place(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py")
    first = request(bridge, peer, "src/engine.py")["queued"][0]
    again = request(bridge, peer, "src/engine.py")["queued"][0]
    assert again == first
    granted = release(bridge, holder)["granted"]
    assert [entry["paths"] for entry in granted] == [["src/engine.py"]]
    assert [notice["id"] for notice in inbox(bridge, peer)] == [
        granted[0]["message_id"]
    ]


def test_a_cancelled_request_takes_nothing_when_the_holder_releases(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py")
    queued = request(bridge, peer, "src/engine.py")["queued"][0]
    cancelled = store.call(
        bridge.home,
        peer,
        "cancel_reservation_request",
        {"request_id": queued["id"]},
    )
    assert cancelled == {"cancelled": 1}
    assert release(bridge, holder) == {"released": 1, "granted": []}
    assert store.active_reservations(bridge.home, paired["root"]) == {}
    assert inbox(bridge, peer) == []
    with pytest.raises(BridgeError, match="no queued request"):
        store.call(
            bridge.home,
            peer,
            "cancel_reservation_request",
            {"request_id": queued["id"]},
        )


def test_cancelling_without_an_identifier_withdraws_every_request(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", "docs/guide.md")
    request(bridge, peer, "src/engine.py", "docs/guide.md")
    cancelled = store.call(bridge.home, peer, "cancel_reservation_request", {})
    assert cancelled == {"cancelled": 2}
    assert release(bridge, holder)["granted"] == []


def test_revoking_a_registration_expires_its_queued_requests(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py")
    request(bridge, peer, "src/engine.py")
    assert store.revoke(bridge.home, paired["root"], "codex") == 1
    assert release(bridge, holder) == {"released": 1, "granted": []}
    assert store.active_reservations(bridge.home, paired["root"]) == {}


def test_a_queued_request_is_bounded_per_lane(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    keys = [f"src/module-{number}.py" for number in range(40)]
    reserve(bridge, holder, *keys[:16])
    reserve(bridge, holder, *keys[16:32])
    reserve(bridge, holder, *keys[32:])
    request(bridge, peer, *keys[:16])
    request(bridge, peer, *keys[16:32])
    with pytest.raises(BridgeError, match="Cancel queued reservation"):
        request(bridge, peer, *keys[32:])


def test_status_and_top_report_the_queue_under_the_holder(
    bridge, repo, paired, capsys
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py")
    request(bridge, peer, "src/engine.py")
    assert store.usage(bridge.home, paired["root"])["claude"]["queued"] == 1
    bridge.status(cli.Selection(participant="claude"))
    output = capsys.readouterr().out
    assert "Reservation requests queued on its keys: 1 (codex)" in output
    view = dashboard.collect(bridge.home, False, {})
    rows = view["projects"][0]["rows"]
    holding = next(row for row in rows if row["participant"] == "claude")
    assert holding["queued"] == 1
    assert holding["queued_by"] == ["codex"]
    assert any("1+1" in line for line in dashboard.render(view))


def test_an_expired_lease_without_a_live_session_goes_to_the_queue(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", ttl_seconds=30)
    assert request(bridge, peer, "src/engine.py")["granted"] == []
    expire(bridge, holder, 60)
    unreachable(bridge, holder)
    reclaimed = store.reclaim_expired(bridge.home, paired["root"])
    assert [entry["agent"] for entry in reclaimed] == ["claude"]
    assert reclaimed[0]["paths"] == ["src/engine.py"]
    assert [entry["agent"] for entry in reclaimed[0]["granted"]] == ["codex"]
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "codex": ["src/engine.py"]
    }
    granted = inbox(bridge, peer)[0]
    assert granted["subject"] == "Reservation granted: src/engine.py"
    assert "expired unrenewed and was released" in granted["body_md"]
    told = inbox(bridge, holder)[0]
    assert told["sender"] == "operator"
    assert told["id"] == reclaimed[0]["message_id"]
    assert "codex took it from the queue" in told["body_md"]
    assert "nothing on disk was locked" in told["body_md"]


def test_a_second_sweep_reclaims_nothing_and_writes_no_second_notice(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    reserve(bridge, holder, "src/engine.py", ttl_seconds=30)
    expire(bridge, holder, 60)
    unreachable(bridge, holder)
    assert len(store.reclaim_expired(bridge.home, paired["root"])) == 1
    assert store.reclaim_expired(bridge.home, paired["root"]) == []
    assert len(inbox(bridge, holder)) == 1


def test_a_live_holder_renews_its_expired_lease_at_the_next_checkpoint(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", ttl_seconds=3600)
    request(bridge, peer, "src/engine.py")
    expire(bridge, holder, 60)
    assert held(bridge, holder) == {"leases": 1, "expired": 1}
    hook(bridge, paired, "claude")
    assert held(bridge, holder) == {"leases": 1, "expired": 0}
    assert store.reclaim_expired(bridge.home, paired["root"]) == []
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "claude": ["src/engine.py"]
    }
    assert inbox(bridge, peer) == []


def test_a_checkpoint_releases_a_lease_whose_claim_is_closed(
    bridge, repo, paired
):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", ttl_seconds=30)
    request(bridge, peer, "src/engine.py")
    expire(bridge, holder, 60)
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE file_reservations SET claim_id='issue-7' WHERE agent_id=?",
            (holder["id"],),
        )
    hook(bridge, paired, "claude")
    assert held(bridge, holder) == {"leases": 0, "expired": 0}
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "codex": ["src/engine.py"]
    }
    told = inbox(bridge, holder)[0]
    assert told["sender"] == "operator"
    assert told["subject"] == (
        "Reservation released with its claim: src/engine.py"
    )
    assert "a claim you no longer hold" in told["body_md"]


def test_status_lists_an_expired_lease_apart_from_the_live_ones(
    bridge, repo, paired, capsys
):
    holder = actor(bridge, paired["root"], "claude")
    reserve(bridge, holder, "src/engine.py", ttl_seconds=30)
    reserve(bridge, holder, "docs/guide.md")
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts=datetime('now','-90 "
            "seconds') WHERE path_pattern='src/engine.py'"
        )
    bridge.status(cli.Selection(participant="claude"))
    output = capsys.readouterr().out
    assert "active reservations: 1; expired: 1, oldest " in output
    assert "s past its deadline" in output
    stats = store.usage(bridge.home, paired["root"])["claude"]
    assert stats["stale_leases"] == 1
    assert stats["stale_lease_age"] >= 90
