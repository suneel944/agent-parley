"""Checks the one escalation of repeated reservation contention."""

import sys
from pathlib import Path

import pytest

from agent_parley import cli, forge, status, store

PATH = "src/engine.py"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Keeps every status reading off the network."""
    monkeypatch.setattr(
        forge, "open_issue_catalog", lambda *args, **kwargs: None
    )


@pytest.fixture
def lanes(bridge, paired):
    """Registers both lanes, each holding one claimed issue."""
    store.initialize(bridge.home)
    resolved = {}
    for name in ("claude", "codex"):
        token = store.register(bridge.home, paired["root"], name)[
            "registration_token"
        ]
        resolved[name] = store.authenticate(bridge.home, token)
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "1")
    bridge.issue(Path(paired["lanes"]["codex"]), "claim", "2")
    return resolved


def reserve(bridge, lane, key=PATH):
    """Reserves one key as a lane through the served tool path."""
    return store.call(
        bridge.home, lane, "file_reservation_paths", {"paths": [key]}
    )


def release(bridge, lane):
    """Releases every key one lane holds."""
    return store.call(bridge.home, lane, "release_file_reservations", {})


def notices(bridge, lane):
    """Reads the contention updates in one lane's own mail."""
    messages = store.call(
        bridge.home, lane, "fetch_inbox", {"include_bodies": True}
    )["messages"]
    return [
        message
        for message in messages
        if message["subject"].startswith("Reservation contention")
    ]


def trade(bridge, lanes, refusals):
    """Alternates the key between the lanes, refusing one lane each round."""
    holder, asker = lanes["claude"], lanes["codex"]
    reserve(bridge, holder)
    for _ in range(refusals):
        assert reserve(bridge, asker)["conflicts"]
        release(bridge, holder)
        reserve(bridge, asker)
        holder, asker = asker, holder


def age(bridge, seconds):
    """Moves every contention record's last refusal back in time."""
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE reservation_contention SET last_ts=last_ts-?,"
            "first_ts=first_ts-?",
            (seconds, seconds),
        )


def show(monkeypatch, bridge, *arguments):
    """Runs one command against the fixture's state."""
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(bridge.home), *arguments]
    )
    return cli.main()


def test_four_alternating_refusals_flag_once_and_mail_each_lane(
    bridge, paired, lanes
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    [flagged] = store.contentions(bridge.home, paired["root"])
    assert flagged["lanes"] == ["claude", "codex"]
    assert flagged["path"] == PATH
    assert flagged["count"] == store.CONTENTION_ATTEMPTS
    for lane in lanes.values():
        [notice] = notices(bridge, lane)
        assert PATH in notice["body_md"]
        assert "advisory" in notice["body_md"]


def test_further_refusals_send_nothing_and_flag_nothing_more(
    bridge, paired, lanes
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS + 2)
    assert len(store.contentions(bridge.home, paired["root"])) == 1
    for lane in lanes.values():
        assert len(notices(bridge, lane)) == 1


def test_fewer_refusals_than_the_threshold_flag_nothing(bridge, paired, lanes):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS - 1)
    assert store.contentions(bridge.home, paired["root"]) == []
    for lane in lanes.values():
        assert notices(bridge, lane) == []


def test_refusals_spread_wider_than_the_window_flag_nothing(
    bridge, paired, lanes
):
    holder, asker = lanes["claude"], lanes["codex"]
    reserve(bridge, holder)
    for _ in range(store.CONTENTION_ATTEMPTS + 1):
        assert reserve(bridge, asker)["conflicts"]
        age(bridge, store.CONTENTION_WINDOW + 1)
    assert store.contentions(bridge.home, paired["root"]) == []
    assert notices(bridge, holder) == [] and notices(bridge, asker) == []


def test_a_lane_renewing_its_own_keys_does_not_clear_the_flag(
    bridge, paired, lanes
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    release(bridge, lanes["claude"])
    reserve(bridge, lanes["claude"], "src/other.py")
    assert len(store.contentions(bridge.home, paired["root"])) == 1


def test_status_shows_one_line_with_both_claims_in_needs_input(
    bridge, repo, paired, lanes, monkeypatch, capsys
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    monkeypatch.chdir(repo)
    assert show(monkeypatch, bridge, "status") == 0
    output = capsys.readouterr().out
    assert output.count("contention: ") == 1
    assert f"contention: claude (#1) and codex (#2) on {PATH}" in output
    assert "Needs input (2): claude contention" in output
    assert "codex contention" in output
    records = bridge.status_snapshot()["projects"][0]["participants"]
    for record in records:
        assert status.bucket_of(record)["bucket"] == status.NEEDS_INPUT
        assert status.bucket_of(record)["waits"] == ["contention"]


def test_problems_carries_one_operator_row_for_the_pair(
    bridge, paired, lanes, monkeypatch
):
    from agent_parley import problems

    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    found = [
        row
        for row in bridge.problems()
        if row["condition"] == problems.CONTENTION
    ]
    assert len(found) == 1
    assert found[0]["actor"] == problems.BY_OPERATOR
    assert "claude (#1) and codex (#2)" in found[0]["detail"]
    assert PATH in found[0]["detail"]
    assert "issue offer" in found[0]["command"]
    assert "issue release" in found[0]["command"]


def test_issue_show_prints_the_record_for_both_issues(
    bridge, repo, paired, lanes, monkeypatch, capsys
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    monkeypatch.chdir(repo)
    for number, peer, held in (("1", "codex", "#2"), ("2", "claude", "#1")):
        assert show(monkeypatch, bridge, "issue", "show", number) == 0
        output = capsys.readouterr().out
        assert (
            f"Contention with {peer} on {PATH}: "
            f"{store.CONTENTION_ATTEMPTS} refusals; {peer} holds {held}"
        ) in output


def test_a_handoff_clears_the_flag_and_counting_restarts(
    bridge, repo, paired, lanes
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    offered = bridge.issue(
        Path(paired["lanes"]["claude"]),
        "offer",
        "1",
        to="codex",
        summary="Take the engine work.",
    )["offer"]
    assert len(store.contentions(bridge.home, paired["root"])) == 1
    bridge.issue(
        Path(paired["lanes"]["codex"]), "accept", "1", offer_id=offered["id"]
    )
    assert store.contentions(bridge.home, paired["root"]) == []
    holder = next(
        lane
        for lane in lanes.values()
        if store.active_reservations(bridge.home, paired["root"]).get(
            lane["name"]
        )
    )
    peer = next(lane for lane in lanes.values() if lane is not holder)
    assert reserve(bridge, peer)["conflicts"]
    assert store.contentions(bridge.home, paired["root"]) == []
    with store.connect(bridge.home) as db:
        counts = [
            row["count"]
            for row in db.execute("SELECT count FROM reservation_contention")
        ]
    assert counts == [1]


def test_a_release_clears_the_flag(bridge, repo, paired, lanes):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    bridge.issue(Path(paired["lanes"]["claude"]), "release", "1")
    assert store.contentions(bridge.home, paired["root"]) == []


def test_the_record_waits_out_the_window_before_a_new_count(
    bridge, paired, lanes
):
    holder, asker = lanes["claude"], lanes["codex"]
    reserve(bridge, holder)
    for _ in range(store.CONTENTION_ATTEMPTS - 1):
        assert reserve(bridge, asker)["conflicts"]
    age(bridge, store.CONTENTION_WINDOW + 1)
    assert reserve(bridge, asker)["conflicts"]
    assert store.contentions(bridge.home, paired["root"]) == []


def counts(bridge):
    """Reads every stored contention count."""
    with store.connect(bridge.home) as db:
        return [
            row["count"]
            for row in db.execute("SELECT count FROM reservation_contention")
        ]


def plant(bridge, paired, low, high, flagged=1.0):
    """Stores one flagged record between two lane names."""
    with store.connect(bridge.home, write=True) as db:
        project = db.execute(
            "SELECT id FROM projects WHERE human_key=?", (paired["root"],)
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO reservation_contention(project_id,low,high,"
            "path_pattern,count,first_ts,last_ts,flagged_ts) "
            "VALUES (?,?,?,?,4,?,?,?)",
            (project, low, high, "src/other.py", flagged, flagged, flagged),
        )


def test_the_whole_flow_shows_then_ends_one_line_and_one_row(
    bridge, repo, paired, lanes, monkeypatch, capsys
):
    from agent_parley import problems

    def rows():
        """Counts the contention rows of the problems view."""
        return [
            row
            for row in bridge.problems()
            if row["condition"] == problems.CONTENTION
        ]

    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    monkeypatch.chdir(repo)
    assert show(monkeypatch, bridge, "status") == 0
    assert capsys.readouterr().out.count("contention: ") == 1
    assert len(rows()) == 1
    offered = bridge.issue(
        Path(paired["lanes"]["claude"]),
        "offer",
        "1",
        to="codex",
        summary="Take the engine work.",
    )["offer"]
    bridge.issue(
        Path(paired["lanes"]["codex"]), "accept", "1", offer_id=offered["id"]
    )
    assert show(monkeypatch, bridge, "status") == 0
    assert "contention: " not in capsys.readouterr().out
    assert rows() == []


def test_one_lane_retrying_against_an_idle_holder_flags_nothing(
    bridge, paired, lanes
):
    reserve(bridge, lanes["claude"])
    for _ in range(store.CONTENTION_ATTEMPTS + 2):
        assert reserve(bridge, lanes["codex"])["conflicts"]
    assert counts(bridge) == [1]
    assert store.contentions(bridge.home, paired["root"]) == []
    assert notices(bridge, lanes["claude"]) == []


def test_a_hook_refusal_does_not_count_toward_contention(bridge, paired, lanes):
    for lane, holder in (("codex", "claude"), ("claude", "codex")) * 3:
        store.record_refusal(bridge.home, paired["root"], lane, holder, PATH)
    assert counts(bridge) == []


def test_one_refusal_blocked_by_two_leases_counts_once(bridge, paired, lanes):
    reserve(bridge, lanes["claude"], "src/**")
    reserve(bridge, lanes["claude"], PATH)
    result = reserve(bridge, lanes["codex"])
    assert len(result["conflicts"]) == 2
    assert counts(bridge) == [1]


def test_four_refusals_spread_over_more_than_the_window_do_not_flag(
    bridge, paired, lanes
):
    holder, asker = lanes["claude"], lanes["codex"]
    reserve(bridge, holder)
    step = store.CONTENTION_WINDOW * 0.6
    for _ in range(store.CONTENTION_ATTEMPTS):
        assert reserve(bridge, asker)["conflicts"]
        release(bridge, holder)
        reserve(bridge, asker)
        holder, asker = asker, holder
        age(bridge, step)
    assert store.contentions(bridge.home, paired["root"]) == []
    assert counts(bridge) == [2]


def test_a_retired_lane_leaves_no_reported_flag_and_prune_removes_it(
    bridge, paired, lanes
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    store.revoke(bridge.home, paired["root"], "codex")
    assert store.contentions(bridge.home, paired["root"]) == []
    assert store.prune(bridge.home, 1e12)["reservation_contention"] == 1
    assert counts(bridge) == []


def test_an_old_flag_is_pruned_and_a_recent_one_stays(bridge, paired, lanes):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    assert store.prune(bridge.home, 1.0)["reservation_contention"] == 0
    assert len(store.contentions(bridge.home, paired["root"])) == 1


def test_a_handoff_keeps_records_against_other_lanes(bridge, paired, lanes):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    plant(bridge, paired, "claude", "zed")
    offered = bridge.issue(
        Path(paired["lanes"]["claude"]),
        "offer",
        "1",
        to="codex",
        summary="Take the engine work.",
    )["offer"]
    bridge.issue(
        Path(paired["lanes"]["codex"]), "accept", "1", offer_id=offered["id"]
    )
    assert store.contentions(bridge.home, paired["root"]) == []
    with store.connect(bridge.home) as db:
        kept = [
            (row["low"], row["high"])
            for row in db.execute("SELECT low,high FROM reservation_contention")
        ]
    assert kept == [("claude", "zed")]


def test_a_release_keeps_records_while_the_lane_holds_another_issue(
    bridge, paired, lanes
):
    trade(bridge, lanes, store.CONTENTION_ATTEMPTS)
    bridge.issue(Path(paired["lanes"]["claude"]), "claim", "3")
    bridge.issue(Path(paired["lanes"]["claude"]), "release", "1")
    assert len(store.contentions(bridge.home, paired["root"])) == 1
    bridge.issue(Path(paired["lanes"]["claude"]), "release", "3")
    assert store.contentions(bridge.home, paired["root"]) == []
