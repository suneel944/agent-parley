"""Checks the structured handoff payload and where its reservations end up."""

from pathlib import Path

from agent_parley import attachments, store, views
from agent_parley.cli import git

KEYS = ["port:5432", "src/engine.py"]


def actor(bridge, root, name):
    """Registers one lane's mail identity and authenticates as it."""
    store.initialize(bridge.home)
    token = store.register(bridge.home, root, name)["registration_token"]
    resolved = store.authenticate(bridge.home, token)
    assert resolved is not None
    return resolved


def reserve(bridge, holder, *keys):
    """Reserves keys as one lane through the served tool path."""
    return store.call(
        bridge.home,
        holder,
        "file_reservation_paths",
        {"paths": list(keys)},
    )


def live(bridge, root):
    """Lists every unreleased lease in one project as holder and key."""
    with store.connect(bridge.home) as db:
        return [
            (row["name"], row["pattern"])
            for row in db.execute(
                "SELECT a.name AS name,f.path_pattern AS pattern "
                "FROM file_reservations f JOIN agents a ON a.id=f.agent_id "
                "JOIN projects p ON p.id=a.project_id "
                "WHERE p.human_key=? AND f.released_ts IS NULL "
                "ORDER BY a.name,f.path_pattern",
                (root,),
            )
        ]


def work(lane):
    """Commits one change on a lane so its diff against the base is real."""
    (lane / "engine.py").write_text("value = 1\n")
    git(lane, "add", "engine.py")
    git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "Lane change",
    )


def handed(bridge, repo, paired, number):
    """Claims, reserves and offers one issue from the claude lane."""
    lanes = {name: Path(path) for name, path in paired["lanes"].items()}
    holder = actor(bridge, paired["root"], "claude")
    actor(bridge, paired["root"], "codex")
    reserve(bridge, holder, *KEYS)
    work(lanes["claude"])
    bridge.issue(lanes["claude"], "claim", number)
    offered = bridge.issue(
        lanes["claude"],
        "offer",
        number,
        to="codex",
        summary="Engine rewritten; checks pass.",
        remaining=["Wire the caller", "Add a regression test"],
    )
    return lanes, offered


def test_an_offer_records_commit_reservations_remaining_and_diff(
    bridge, repo, paired
):
    lanes, offered = handed(bridge, repo, paired, "432")
    offer = offered["offer"]
    assert offer["commit"] == git(lanes["claude"], "rev-parse", "HEAD")
    assert offer["reservations"] == KEYS
    assert offer["remaining"] == ["Wire the caller", "Add a regression test"]
    _, directory = bridge.project(repo)
    diff = attachments.body(directory, offer["diff"], "codex")
    assert "engine.py" in diff
    assert offer["diff_bytes"] == len(diff.encode())
    reported = views.ledger(bridge.issue(repo, "list"))["issues"][0]
    assert reported["offer"]["reservations"] == KEYS
    assert reported["offer"]["remaining"] == offer["remaining"]
    assert reported["offer"]["commit"] == offer["commit"]
    assert reported["offer"]["diff"] == offer["diff"]


def test_accepting_a_handoff_moves_its_reservations_in_one_step(
    bridge, repo, paired
):
    lanes, offered = handed(bridge, repo, paired, "432")
    assert live(bridge, paired["root"]) == [("claude", key) for key in KEYS]
    accepted = bridge.issue(
        lanes["codex"], "accept", "432", offer_id=offered["offer"]["id"]
    )
    assert accepted["owner"] == "codex"
    assert accepted["reservations_moved"] == KEYS
    assert accepted["handoff"]["from"] == "claude"
    assert accepted["handoff"]["remaining"] == offered["offer"]["remaining"]
    assert live(bridge, paired["root"]) == [("codex", key) for key in KEYS]
    assert store.active_reservations(bridge.home, paired["root"]) == {
        "codex": KEYS
    }
    claims = [
        lane["claims"]
        for project in bridge.status_snapshot()["projects"]
        for lane in project["participants"]
        if lane["participant"] == "codex"
    ][0]
    assert claims[0]["handoff"]["reservations"] == KEYS
    assert claims[0]["handoff"]["commit"] == offered["offer"]["commit"]


def test_declining_a_handoff_leaves_every_reservation_with_the_offerer(
    bridge, repo, paired
):
    lanes, offered = handed(bridge, repo, paired, "432")
    declined = bridge.issue(
        lanes["codex"], "decline", "432", offer_id=offered["offer"]["id"]
    )
    assert declined["owner"] == "claude"
    assert declined["offer"] is None
    assert "handoff" not in declined
    assert live(bridge, paired["root"]) == [("claude", key) for key in KEYS]


def test_cancelling_a_handoff_leaves_every_reservation_with_the_offerer(
    bridge, repo, paired
):
    lanes, offered = handed(bridge, repo, paired, "432")
    cancelled = bridge.issue(lanes["claude"], "cancel", "432")
    assert cancelled["owner"] == "claude"
    assert cancelled["offer"] is None
    assert live(bridge, paired["root"]) == [("claude", key) for key in KEYS]
    assert offered["offer"]["reservations"] == KEYS
