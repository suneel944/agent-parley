"""Fault-injection acceptance: every claim completes under live-lane faults.

Each scenario drives two stub clients through a claim while one fault a live
lane meets is injected: a lane killed mid-claim, a service restart, a usage
limit dialog, a permission prompt on resume, a full disk under the logs and
a dropped ``Stop`` event. The stub clients speak through the real hook path
(`checkpoints.serve`), the real launcher dialog watch, the real supervision
poll and the real issue ledger; only the native model and the terminal wake
are stubbed. Time runs on a scenario clock: each poll models one minute of
lane time, so the measured minutes are the minutes a live run would spend.

A scenario passes when its claim reaches the complete state, the idle
lane-minutes the event logs measure stay under `IDLE_LIMIT`, and the
claim-minutes nothing accounts for stay under `UNACCOUNTED_LIMIT`. A claim
minute is accounted for when its holder is active, when the claim carries an
orphan or stranded marker, or when the operator's problem list names the
holder lane or the service.

The release workflow refuses a minor or major release unless this module
passes on the release commit; see `scripts/release_publish.py`.
"""

import errno
import shlex
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from agent_parley import (
    checkpoints,
    dialogs,
    issues,
    metrics,
    problems,
    process,
    store,
    supervision,
    terminal,
)
from agent_parley.cli import git
from agent_parley.state import write_json

MINUTE = 60.0
STALL = int(supervision.DEFAULTS["stalled_after"] // MINUTE)
IDLE_LIMIT = 15
UNACCOUNTED_LIMIT = STALL
NUMBER = "17"
USAGE_LIMIT = (
    "  ⎿  You've hit your weekly limit · resets Sep 21, 6am (Asia/Dubai)    "
    "/upgrade or /usage-credits to finish what you're working on."
)
BRIDGE_TOOL = "mcp__agent_parley__fetch_inbox"
READ = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}


class Clock:
    """Scenario clock that runs ahead of the wall clock by whole minutes."""

    def __init__(self, real):
        self.real = real
        self.offset = 0.0

    def __call__(self):
        return self.real() + self.offset

    def advance(self, minutes=1):
        """Moves lane time forward without sleeping."""
        self.offset += minutes * MINUTE


class Stub:
    """A client lane whose turns are hook events instead of a model."""

    def __init__(self, bridge, paired, name, clock):
        self.bridge = bridge
        self.name = name
        self.clock = clock
        self.lane = Path(paired["lanes"][name])
        self.directory = self.lane.parent
        self.child = None
        self.session = 0
        write_json(self.directory / f"{name}-identity.json", {"name": name})
        self.start()

    def start(self):
        """Starts, or resumes, the native session process."""
        self.session += 1
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.activity("working")

    def activity(self, state):
        """Publishes the activity a native session hook would record."""
        write_json(
            self.directory / f"{self.name}-activity.json",
            {
                "activity": state,
                "updated": self.clock(),
                "session_id": f"{self.name}-{self.session}",
                "session_pid": self.child.pid,
                "session_ticks": process.start_ticks(self.child.pid),
            },
        )

    def event(self, payload):
        """Delivers one hook event through the served hook path."""
        served = checkpoints.serve(
            self.bridge.home,
            {
                "directory": str(self.directory),
                "participant": self.name,
                "payload": {
                    **payload,
                    "cwd": str(self.lane),
                    "session_id": f"{self.name}-{self.session}",
                },
            },
        )
        assert served["status"] == 0, served["stderr"]
        return served

    def turn(self):
        """Runs one working turn that ends in a delivered Stop."""
        self.event({"hook_event_name": "UserPromptSubmit", "prompt": "go"})
        self.event(READ)
        self.activity("working")
        self.event({"hook_event_name": "Stop", "stop_hook_active": False})

    def kill(self):
        """Kills the native session without any hook event."""
        self.child.kill()
        self.child.wait(timeout=5)

    def close(self):
        """Stops the session process the stub owns."""
        if self.child and self.child.poll() is None:
            self.kill()

    def finish(self, repo, number=NUMBER):
        """Commits, reports and merges the claimed issue."""
        (self.lane / f"issue-{number}.txt").write_text("verified\n")
        git(self.lane, "add", f"issue-{number}.txt")
        git(self.lane, "commit", "-m", f"Complete fixture issue {number}")
        self.bridge.report(self.lane, "ready", f"#{number} ready", "", "gate")
        self.bridge.approve(repo, self.name)
        self.bridge.merge(repo, self.name)


class Scenario:
    """Measures one claim's life under a fault on the scenario clock."""

    def __init__(self, bridge, repo, paired, clock):
        self.bridge = bridge
        self.repo = repo
        self.clock = clock
        self.directory = Path(paired["lanes"]["claude"]).parent
        self.start = clock()
        self.service = True
        self.unaccounted = 0
        self.lanes = {
            name: Stub(bridge, paired, name, clock)
            for name in ("claude", "codex")
        }

    def record(self, number=NUMBER):
        """Returns the ledger record of the scenario's claim."""
        return issues.snapshot(self.directory)["issues"][number]

    def problems(self):
        """Reads status and the operator's problem rows on the scenario clock.

        The in-process poll plays the service, so readiness is the
        scenario's own record of whether the service is up.
        """
        report = self.bridge.status_snapshot()
        report["server"]["ready"] = self.service
        return report, problems.derive(
            self.bridge.home, report, now=self.clock()
        )

    def accounted(self, number=NUMBER):
        """Reports whether anything explains the claim's current minute."""
        record = self.record(number)
        owner = record.get("owner")
        if not owner:
            return True
        if record.get("orphan"):
            return True
        if supervision.published_stranded_claim(self.directory, number):
            return True
        report, rows = self.problems()
        if any(
            row["participant"] == owner
            or row["condition"] in {problems.SERVICE, problems.STORE}
            for row in rows
        ):
            return True
        lanes = {
            lane["participant"]: lane
            for project in report["projects"]
            for lane in project["participants"]
        }
        availability = lanes[owner]["availability"]
        return availability["state"] == supervision.ACTIVE

    def tick(self, minutes=1):
        """Advances lane time a minute at a time, polling and measuring."""
        for _ in range(minutes):
            self.clock.advance()
            supervision.poll(self.bridge.home, self.directory)
            if not self.accounted():
                self.unaccounted += 1

    def idle_minutes(self):
        """Sums the idle lane-minutes the lanes' event logs measure."""
        return (
            sum(
                metrics.idle_intervals(
                    self.directory, name, since=self.start, now=self.clock()
                )["seconds"]
                for name in self.lanes
            )
            / MINUTE
        )

    def assert_accepted(self, number=NUMBER):
        """Applies the acceptance bar to the finished scenario."""
        record = self.record(number)
        assert record["execution"]["state"] == "complete"
        assert record["owner"] is None
        assert self.idle_minutes() <= IDLE_LIMIT
        assert self.unaccounted <= UNACCOUNTED_LIMIT

    def close(self):
        """Stops every stub session."""
        for stub in self.lanes.values():
            stub.close()


@pytest.fixture
def scenario(bridge, repo, paired, monkeypatch):
    """Two stub lanes, an approved merge gate and one claim on #17."""
    clock = Clock(time.time)
    monkeypatch.setattr(time, "time", clock)
    monkeypatch.setattr(supervision.forge, "branch_completion", lambda *a: None)
    monkeypatch.setattr(terminal, "request", lambda *a: "accepted")
    resumes = []

    def resume(argv, **kwargs):
        resumes.append(argv[argv.index("run") + 1])
        return subprocess.Popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(
        supervision,
        "subprocess",
        types.SimpleNamespace(**{**vars(subprocess), "Popen": resume}),
    )
    for path in (repo, *map(Path, paired["lanes"].values())):
        git(path, "config", "user.name", "Bridge Test")
        git(path, "config", "user.email", "test@example.com")
    bridge.verification(repo, f"{shlex.quote(sys.executable)} -c pass")
    bridge.approval_policy(repo, ["merge"])
    store.initialize(bridge.home)
    for participant in paired["participants"].values():
        store.register(bridge.home, paired["root"], participant["display"])
    run = Scenario(bridge, repo, paired, clock)
    run.resumes = resumes
    bridge.issue(run.lanes["claude"].lane, "claim", NUMBER)
    run.lanes["claude"].turn()
    run.tick()
    yield run
    run.close()


def test_a_lane_killed_mid_claim_is_taken_over_and_completed(scenario):
    holder, peer = scenario.lanes["claude"], scenario.lanes["codex"]
    holder.kill()
    scenario.tick(STALL + 1)
    assert scenario.record()["orphan"]["owner"] == "claude"
    scenario.bridge.issue(peer.lane, "claim", NUMBER, take_orphaned=True)
    peer.turn()
    scenario.tick()
    peer.finish(scenario.repo)
    scenario.assert_accepted()


def test_a_service_restart_keeps_the_claim_and_completes_it(scenario):
    holder = scenario.lanes["claude"]
    scenario.bridge.up()
    scenario.bridge.down()
    scenario.service = False
    scenario.tick(2)
    scenario.bridge.up()
    scenario.service = True
    assert scenario.bridge.health()["status"] == "ready"
    assert scenario.record()["owner"] == "claude"
    holder.turn()
    scenario.tick()
    holder.finish(scenario.repo)
    scenario.bridge.down()
    scenario.assert_accepted()


def test_a_usage_limit_dialog_strands_the_claim_for_a_peer(scenario):
    holder, peer = scenario.lanes["claude"], scenario.lanes["codex"]
    watch = dialogs.Watch(holder.directory, "claude")
    watch.advance(USAGE_LIMIT.encode(), 0.0)
    watch.advance(b"", 0.5)
    capacity = supervision.published_capacity(holder.directory, "claude")
    assert capacity["state"] == "exhausted"
    scenario.tick(2)
    assert supervision.published_stranded_claim(holder.directory, NUMBER)
    scenario.bridge.authorize_recovery(scenario.repo, NUMBER, "usage limit")
    scenario.tick()
    scenario.bridge.issue(peer.lane, "claim", NUMBER, take_orphaned=True)
    peer.turn()
    scenario.tick()
    peer.finish(scenario.repo)
    scenario.assert_accepted()


def test_a_permission_prompt_on_resume_is_named_then_answered(scenario):
    holder = scenario.lanes["claude"]
    holder.kill()
    holder.start()
    holder.event({"hook_event_name": "SessionStart"})
    holder.event(
        {"hook_event_name": "PermissionRequest", "tool_name": BRIDGE_TOOL}
    )
    scenario.tick(STALL + 1)
    _, rows = scenario.problems()
    assert any(
        row["participant"] == "claude" and row["condition"] == problems.APPROVAL
        for row in rows
    )
    holder.turn()
    scenario.tick()
    holder.finish(scenario.repo)
    scenario.assert_accepted()


def test_a_full_disk_under_the_logs_never_blocks_the_lane(
    scenario, monkeypatch
):
    holder = scenario.lanes["claude"]
    written = checkpoints.write_json

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(checkpoints, "write_json", full)
    served = holder.event(READ)
    assert "No space left on device" in served["stderr"]
    scenario.tick()
    monkeypatch.setattr(checkpoints, "write_json", written)
    holder.turn()
    scenario.tick()
    holder.finish(scenario.repo)
    scenario.assert_accepted()


def test_a_dropped_stop_event_still_surfaces_and_completes(scenario):
    holder = scenario.lanes["claude"]
    holder.event({"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    scenario.tick(STALL + 1)
    _, rows = scenario.problems()
    assert any(
        row["participant"] == "claude"
        and row["condition"] in {problems.STALLED, problems.INACTIVE}
        for row in rows
    )
    holder.turn()
    scenario.tick()
    holder.finish(scenario.repo)
    scenario.assert_accepted()
