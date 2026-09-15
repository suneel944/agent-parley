"""Pins how often one status frame reads the store and the issue ledger."""

import collections
import sqlite3

import pytest

from agent_parley import cli, issues, store, supervision


@pytest.fixture
def registered(bridge, repo, paired):
    """Registers both lanes so the store answers project-wide questions."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    return paired


def counted(monkeypatch):
    """Counts the project-wide readings a frame takes, by kind."""
    counts: collections.Counter = collections.Counter()

    def wrap(kind, original):
        def call(*args, **kwargs):
            counts[kind] += 1
            return original(*args, **kwargs)

        return call

    ledger = wrap("snapshot", issues.snapshot)
    monkeypatch.setattr(store, "connect", wrap("connect", store.connect))
    monkeypatch.setattr(issues, "snapshot", ledger)
    monkeypatch.setattr(cli, "snapshot", ledger)
    monkeypatch.setattr(
        supervision,
        "configuration",
        wrap("configuration", supervision.configuration),
    )
    return counts


def test_a_frame_reads_each_project_wide_source_once(
    bridge, registered, monkeypatch
):
    counts = counted(monkeypatch)
    frame = bridge.status_snapshot()
    assert len(frame["projects"][0]["participants"]) == 2
    assert counts["connect"] == 1
    assert counts["snapshot"] == 1
    assert counts["configuration"] == 1


def test_an_unreadable_schedule_still_reports_against_each_lane(
    bridge, registered, monkeypatch
):
    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("scheduled_deliveries is locked")

    monkeypatch.setattr(store, "schedules", refuse)
    frame = bridge.status_snapshot()
    for participant in frame["projects"][0]["participants"]:
        assert participant["mail"]["error"] == (
            "scheduled_deliveries is locked"
        )


def test_a_lane_read_on_its_own_still_answers_without_a_shared_frame(
    bridge, registered
):
    path = next((bridge.home / "projects").glob("*/project.json"))
    data = cli.roster.normalize(cli.json.loads(path.read_text()))
    alone = bridge._lane_status(path.parent, data, "claude")
    shared = next(
        participant
        for participant in bridge.status_snapshot()["projects"][0][
            "participants"
        ]
        if participant["participant"] == "claude"
    )
    volatile = {"report_age_seconds", "idle_seconds"}
    assert {k: v for k, v in alone.items() if k not in volatile} == {
        k: v for k, v in shared.items() if k not in volatile
    }
