"""Checks that a ready report describes the commit it records."""

from pathlib import Path

import pytest

from agent_parley import metrics
from agent_parley.state import BridgeError


def reports(directory):
    """Returns the states of a lane's recorded reports."""
    return [
        record.get("state")
        for record in metrics.report_records(directory, "claude")
        if record.get("kind") == "report"
    ]


def test_a_ready_report_refuses_uncommitted_tracked_changes(bridge, paired):
    lane = Path(paired["lanes"]["claude"])
    (lane / "shared.txt").write_text("edited but not committed\n")
    with pytest.raises(BridgeError, match=r"1 tracked file\(s\).*shared\.txt"):
        bridge.report(lane, "ready", "Parser built", "", "12 tests passed")
    assert reports(lane.parent) == []


def test_a_ready_report_ignores_untracked_scratch_files(bridge, paired):
    lane = Path(paired["lanes"]["claude"])
    (lane / "scratch.log").write_text("build output\n")
    bridge.report(lane, "ready", "Parser built", "", "12 tests passed")
    assert reports(lane.parent) == ["ready"]


def test_a_partial_report_keeps_accepting_work_in_progress(bridge, paired):
    lane = Path(paired["lanes"]["claude"])
    (lane / "shared.txt").write_text("half done\n")
    bridge.report(lane, "partial", "Parser half built", "Finish it", "")
    assert reports(lane.parent) == ["partial"]
