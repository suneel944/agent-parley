"""Checks that keyed coordination writes apply once however often retried."""

import pytest

from agent_parley import issues, lifecycle, store
from agent_parley.state import BridgeError, LockBusy


def actor(bridge, root, name):
    """Registers one lane and resolves its own store identity."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def reserve(bridge, holder, *paths, **arguments):
    """Reserves paths as one lane through the served tool path."""
    return store.call(
        bridge.home,
        holder,
        "file_reservation_paths",
        {"paths": list(paths), **arguments},
    )


def leases(bridge, holder):
    """Counts the leases one lane currently holds."""
    with store.connect(bridge.home) as db:
        return db.execute(
            "SELECT count(*) FROM file_reservations "
            "WHERE agent_id=? AND released_ts IS NULL",
            (holder["id"],),
        ).fetchone()[0]


def test_a_repeated_reservation_reserves_once(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    granted = reserve(bridge, holder, "src/engine.py", idempotency_key="lease")
    again = reserve(bridge, holder, "src/engine.py", idempotency_key="lease")
    assert "replayed" not in granted
    assert again["replayed"] is True
    assert again["granted"] == granted["granted"]
    assert leases(bridge, holder) == 1


def test_a_key_reused_for_other_paths_is_refused_by_name(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    reserve(bridge, holder, "src/engine.py", idempotency_key="lease")
    with pytest.raises(BridgeError, match="already names a different"):
        reserve(bridge, holder, "docs/plan.md", idempotency_key="lease")
    assert leases(bridge, holder) == 1


def test_one_key_belongs_to_one_participant(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", idempotency_key="lease")
    granted = reserve(bridge, peer, "docs/plan.md", idempotency_key="lease")
    assert "replayed" not in granted
    assert leases(bridge, peer) == 1


def test_a_refused_call_replays_as_the_same_refusal(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    peer = actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, "src/engine.py", exclusive=True)
    blocked = reserve(
        bridge, peer, "src/engine.py", exclusive=True, idempotency_key="lease"
    )
    assert blocked["granted"] == []
    store.call(bridge.home, holder, "release_file_reservations", {})
    again = reserve(
        bridge, peer, "src/engine.py", exclusive=True, idempotency_key="lease"
    )
    assert again["granted"] == []
    assert again["replayed"] is True
    assert leases(bridge, peer) == 0


def test_a_repeated_acknowledgement_keeps_the_first_timestamp(
    bridge, repo, paired
):
    sender = actor(bridge, paired["root"], "claude")
    reader = actor(bridge, paired["root"], "codex")
    delivered = store.call(
        bridge.home,
        sender,
        "send_message",
        {
            "to": ["codex"],
            "subject": "Ready for review",
            "body_md": "The branch is pushed.",
            "idempotency_key": "note",
        },
    )
    arguments = {"message_id": delivered["id"], "idempotency_key": "ack"}
    store.call(bridge.home, reader, "acknowledge_message", arguments)
    with store.connect(bridge.home) as db:
        first = db.execute(
            "SELECT ack_ts FROM message_recipients WHERE message_id=?",
            (delivered["id"],),
        ).fetchone()[0]
    replayed = store.call(bridge.home, reader, "acknowledge_message", arguments)
    assert replayed["replayed"] is True
    with store.connect(bridge.home) as db:
        assert (
            db.execute(
                "SELECT ack_ts FROM message_recipients WHERE message_id=?",
                (delivered["id"],),
            ).fetchone()[0]
            == first
        )


def test_a_retained_key_is_bounded_per_participant(bridge, repo, paired):
    holder = actor(bridge, paired["root"], "claude")
    for number in range(store.retries.RETAINED_CALLS + 2):
        store.call(
            bridge.home,
            holder,
            "release_file_reservations",
            {"idempotency_key": str(number)},
        )
    with store.connect(bridge.home) as db:
        retained = db.execute(
            "SELECT count(*) FROM idempotent_calls WHERE agent_id=?",
            (holder["id"],),
        ).fetchone()[0]
    assert retained == store.retries.RETAINED_CALLS


def test_a_repeated_handoff_offers_once(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    offered = bridge.issue(
        lane, "offer", "42", to="codex", summary="Take the parser.", key="pass"
    )
    again = bridge.issue(
        lane, "offer", "42", to="codex", summary="Take the parser.", key="pass"
    )
    assert again["replayed"] is True
    assert again["offer"]["id"] == offered["offer"]["id"]
    record = issues.snapshot(directory)["issues"]["42"]
    assert record["offer"]["id"] == offered["offer"]["id"]
    assert [entry["action"] for entry in record["history"]] == [
        "claim",
        "offer",
    ]


def test_a_transition_key_cannot_be_replayed_onto_another_issue(
    bridge, repo, paired
):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42", key="own")
    with pytest.raises(BridgeError, match="already names a different"):
        bridge.issue(lane, "claim", "43", key="own")
    assert "43" not in issues.snapshot(bridge.project(repo)[1])["issues"]


def test_a_refused_transition_replays_as_refused(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    with pytest.raises(BridgeError, match="has no owner"):
        bridge.issue(lane, "release", "42", key="let-go")
    bridge.issue(lane, "claim", "42")
    with pytest.raises(BridgeError, match="has no owner"):
        bridge.issue(lane, "release", "42", key="let-go")
    assert issues.snapshot(directory)["issues"]["42"]["owner"] == "claude"


def test_a_repeated_report_counts_one_attempt(bridge, repo, paired):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    for _ in range(2):
        bridge.report(
            lane,
            "blocked",
            "Waiting on the schema decision.",
            "Apply the migration.",
            "",
            key="stuck",
        )
    assert issues.snapshot(directory)["issues"]["42"]["attempts"] == 1


def test_a_late_replayed_report_leaves_the_lifecycle_alone(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    stuck = ("blocked", "Waiting on schema.", "Apply the migration.", "")
    bridge.report(lane, *stuck, key="stuck")
    bridge.report(lane, "ready", "Migration applied.", "", "make check", "go")
    bridge.report(lane, *stuck, key="stuck")
    record = issues.snapshot(directory)["issues"]["42"]
    assert lifecycle.state(record)["state"] == lifecycle.READY


def test_a_report_key_reused_for_other_content_is_refused(bridge, repo, paired):
    lane = paired["lanes"]["claude"]
    bridge.issue(lane, "claim", "42")
    bridge.report(lane, "partial", "Parser done.", "Wire the CLI.", "", "step")
    with pytest.raises(BridgeError, match="already names a different"):
        bridge.report(
            lane, "partial", "Parser done.", "Wire the server.", "", "step"
        )


def test_a_claim_refused_by_contention_is_evaluated_again(
    bridge, repo, paired, monkeypatch
):
    lane = paired["lanes"]["claude"]
    applied = issues._change
    contended = []

    def busy_once(*arguments, **options):
        if not contended:
            contended.append(True)
            raise LockBusy("Another operation owns a lock; retry later.")
        return applied(*arguments, **options)

    monkeypatch.setattr(issues, "_change", busy_once)
    with pytest.raises(LockBusy):
        bridge.issue(lane, "claim", "42", key="k1")
    assert bridge.issue(lane, "claim", "42", key="k1")["owner"] == "claude"


def test_a_claim_refused_by_another_owner_stays_refused(bridge, repo, paired):
    lanes = paired["lanes"]
    bridge.issue(lanes["codex"], "claim", "42")
    with pytest.raises(BridgeError, match="is owned by codex"):
        bridge.issue(lanes["claude"], "claim", "42", key="k2")
    bridge.issue(lanes["codex"], "release", "42")
    with pytest.raises(BridgeError, match="is owned by codex"):
        bridge.issue(lanes["claude"], "claim", "42", key="k2")
