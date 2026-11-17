"""Checks that a released orphaned claim's checkpoint reaches the next owner."""

from pathlib import Path

from agent_parley import issues, recovery, supervision
from tests.test_orphaned_claims import killed, registered, running

STALLED = supervision.DEFAULTS["stalled_after"]


def released(bridge, paired):
    """Orphans claude's issue 42 with unstaged work, then releases it."""
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    bridge.issue(lane, "claim", "42")
    (lane / "worktree.txt").write_text("worktree\n")
    killed(directory, "claude", STALLED + 100)
    running(directory, "codex")
    supervision.poll(bridge.home, directory)
    claim_id = issues.snapshot(directory)["issues"]["42"]["claim_id"]
    bridge.issue(lane, "release", "42")
    return directory, recovery.checkpoint(directory, "42", claim_id)


def test_prune_keeps_a_released_orphans_checkpoint(bridge, repo, paired):
    directory, saved = released(bridge, paired)
    record = issues.snapshot(directory)["issues"]["42"]
    bundle = directory / "recovery" / saved["artifact"]["reference"]

    removed = recovery.prune(directory, issues.snapshot(directory))

    assert record["owner"] is None
    assert record["released_orphan"]["owner"] == "claude"
    assert saved["id"] not in removed
    assert bundle.exists()


def test_the_next_claimant_restores_a_released_orphans_work(
    bridge, repo, paired
):
    directory, saved = released(bridge, paired)
    peer = Path(paired["lanes"]["codex"])

    claimed = bridge.issue(peer, "claim", "42")

    assert claimed["owner"] == "codex"
    assert claimed["taken"]["from"] == "claude"
    assert claimed["taken"]["checkpoint"]["id"] == saved["id"]
    assert claimed["history"][-1]["action"] == "take"
    assert claimed["recovery"]["phase"] == "complete"
    assert "released_orphan" not in claimed
    assert (peer / "worktree.txt").read_text() == "worktree\n"


def test_a_released_checkpoint_goes_once_its_taker_releases(
    bridge, repo, paired
):
    directory, saved = released(bridge, paired)
    peer = Path(paired["lanes"]["codex"])
    bridge.issue(peer, "claim", "42")
    bridge.issue(peer, "release", "42")

    removed = recovery.prune(directory, issues.snapshot(directory))

    assert "released_orphan" not in issues.snapshot(directory)["issues"]["42"]
    assert saved["id"] in removed
