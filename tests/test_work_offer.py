"""Checks the capacity gate and the advisory work offers built on it."""

import datetime
import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent_parley import (
    dashboard,
    issues,
    records,
    roster,
    store,
    supervision,
    terminal,
    views,
)
from agent_parley.checkpoints import checkpoint
from agent_parley.process import start_ticks
from agent_parley.state import write_json


@pytest.fixture(autouse=True)
def quiet_forge(monkeypatch):
    """Keeps the poll off the host forge while these tests run."""
    monkeypatch.setattr(
        supervision.forge, "branch_completion", lambda *args: None
    )


def registered(bridge, paired):
    """Registers both lanes so mail and presence are readable."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)


def alive(directory, name, **extra):
    """Publishes a live, quiet session for one lane."""
    write_json(
        directory / f"{name}-activity.json",
        {
            "session_pid": os.getpid(),
            "session_ticks": start_ticks(os.getpid()),
            "activity": "idle",
            "updated": time.time(),
            **extra,
        },
    )


def turn_ended(directory, name, ago):
    """Records one retained turn end so an idle stretch reads as open."""
    (directory / f"{name}-events.jsonl").write_text(
        json.dumps({"ts": time.time() - ago, "event": "Stop"}) + "\n"
    )


def tool_used(directory, name):
    """Appends one tool-use event of the kind a working lane records."""
    with (directory / f"{name}-events.jsonl").open("a") as stream:
        stream.write(
            json.dumps({"ts": time.time(), "event": "PostToolUse"}) + "\n"
        )


def unthrottle(directory, name):
    """Ages the wake spacing so the next attempt is admitted immediately."""
    path = directory / f"{name}-wake.json"
    if path.exists():
        record = json.loads(path.read_text())
        record["at"] = 0
        write_json(path, record)
    published = supervision.published_work(directory, name)
    if published.get("dispatch"):
        published["dispatch"]["updated_at"] = 0
        write_json(directory / f"{name}-work.json", published)


def refused(paired, name, ago, text="API Error: usage limit reached"):
    """Writes a usage refusal into the lane's own client session record."""
    lane = Path(paired["lanes"][name])
    directory = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
    )
    directory.mkdir(parents=True, exist_ok=True)
    moment = time.time() - ago
    (directory / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)
                ),
                "message": {"content": [{"type": "text", "text": text}]},
            }
        )
        + "\n"
    )


def succeeded(paired, name, ago):
    """Appends a successful provider response to one lane's transcript."""
    lane = Path(paired["lanes"][name])
    path = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
        / "session.jsonl"
    )
    moment = time.time() - ago
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)
                    ),
                    "message": {
                        "id": f"success-{moment}",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": {"output_tokens": 1},
                    },
                }
            )
            + "\n"
        )


def codex_capacity_record(
    paired,
    rate_limits,
    progress=1,
    request_tokens=None,
    timestamp="2026-09-19T12:00:00Z",
    session="capacity",
):
    """Writes one structured Codex rate-limit observation."""
    lane = Path(paired["lanes"]["codex"])
    today = datetime.date.today()
    directory = (
        Path(os.environ["CODEX_HOME"])
        / "sessions"
        / f"{today:%Y}"
        / f"{today:%m}"
        / f"{today:%d}"
    )
    directory.mkdir(parents=True)
    path = directory / f"rollout-{session}.jsonl"
    info = {"total_token_usage": {"total_tokens": progress}}
    if request_tokens is not None:
        info["last_token_usage"] = {"total_tokens": request_tokens}
    path.write_text(
        json.dumps({"payload": {"cwd": str(lane)}})
        + "\n"
        + json.dumps(
            {
                "timestamp": timestamp,
                "payload": {
                    "type": "token_count",
                    "info": info,
                    "rate_limits": rate_limits,
                },
            }
        )
        + "\n"
    )
    return path


def offer_for(directory, name):
    """Returns the advisory offer published for one lane, if any."""
    return supervision.published_work(directory, name)["offer"]


def holding_a_backlog(bridge, paired, monkeypatch, count):
    """Leaves claude holding one claim with a stated remaining-work count."""
    monkeypatch.setattr(terminal, "request", lambda path, name: "accepted")
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    alive(directory, "codex")
    bridge.issue(lane, "claim", "2")
    if count is not None:
        bridge.report(
            lane,
            "partial",
            "Processing families.",
            "families still to convert",
            "",
            backlog=count,
        )
    turn_ended(directory, "claude", 3600)
    turn_ended(directory, "codex", 3600)
    return lane, peer, directory


def test_unclaimed_work_is_ordered_by_the_peers_that_wait_on_it(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    for number in ("4", "7", "9"):
        bridge.issue(lane, "claim", number)
        bridge.issue(lane, "release", number)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "block", "2", on="9")
    bridge.issue(lane, "claim", "5")
    bridge.issue(lane, "block", "5", on="4")
    bridge.issue(lane, "release", "5")
    ledger = issues.snapshot(lane.parent)
    assert issues.unclaimed(ledger) == ["9", "4", "7"]
    assert issues.holders(ledger) == {"codex": ["2"]}


def test_a_fit_idle_lane_is_offered_unclaimed_and_shed_able_work(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(lane, "claim", "9")
    bridge.issue(lane, "release", "9")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["fit"] is True
    assert published["failed"] == []
    assert published["offer"]["kind"] == "pull"
    assert "#9" in published["offer"]["text"]
    assert "codex" in published["offer"]["text"]


def test_a_recent_usage_refusal_makes_a_lane_unfit_and_unoffered(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    refused(paired, "claude", 30)
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["fit"] is False
    assert published["failed"] == ["capacity"]
    assert published["checks"]["capacity"] is False
    assert "provider capacity is exhausted" in published["reason"]
    assert published["offer"] is None


def test_elapsed_stall_time_does_not_restore_exhausted_capacity(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    refused(paired, "claude", 4000)
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["checks"]["capacity"] is False
    assert published["capacity"]["state"] == "exhausted"
    assert published["fit"] is False
    assert published["offer"] is None


def test_later_success_restores_exhausted_capacity(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "claude")
    refused(paired, "claude", 60)
    succeeded(paired, "claude", 30)
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["checks"]["capacity"] is True
    assert published["capacity"]["state"] == "available"


def test_transient_rate_limit_is_distinct_from_exhaustion(bridge, repo, paired):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    alive(directory, "claude")
    refused(paired, "claude", 30, "API Error: rate limit")
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["checks"]["capacity"] is False
    assert published["capacity"]["state"] == "retryable"
    assert "retryable transient failure" in published["reason"]


def test_codex_structured_limit_preserves_reliable_reset(bridge, repo, paired):
    reset_at = time.time() + 3600
    codex_capacity_record(
        paired,
        {
            "rate_limit_reached_type": "primary",
            "primary": {"used_percent": 100, "resets_at": reset_at},
        },
    )
    observation = records.capacity_observation(
        bridge.home, paired["participants"]["codex"]
    )
    assert observation is not None
    assert observation["state"] == "exhausted"
    assert observation["reset_at"] == reset_at
    assert observation["source"] == "codex-session-record"
    assert observation["session_id"] == "rollout-capacity"


def test_first_real_codex_response_in_new_session_restores_capacity(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["codex"]).parent
    supervision.record_capacity(
        directory,
        "codex",
        {
            "state": "exhausted",
            "observed_at": datetime.datetime.fromisoformat(
                "2026-09-19T12:00:00+00:00"
            ).timestamp(),
            "source": "codex-session-record",
            "session_id": "rollout-old",
            "observation_id": "old-refusal",
            "progress": 10,
        },
    )
    codex_capacity_record(
        paired,
        {"primary": {"used_percent": 10}},
        progress=10,
        request_tokens=2,
        timestamp="2026-09-19T12:01:00Z",
        session="new",
    )
    observed = supervision.capacity(bridge.home, directory, paired, "codex")
    assert observed["state"] == "available"
    assert observed["session_id"] == "rollout-new"
    assert observed["request_tokens"] == 2


@pytest.mark.parametrize(
    ("request_tokens", "timestamp"),
    [
        (None, "2026-09-19T12:01:00Z"),
        (0, "2026-09-19T12:01:00Z"),
        (2, "2026-09-19T11:59:00Z"),
    ],
)
def test_new_codex_session_replay_does_not_restore_capacity(
    bridge, repo, paired, request_tokens, timestamp
):
    directory = Path(paired["lanes"]["codex"]).parent
    supervision.record_capacity(
        directory,
        "codex",
        {
            "state": "exhausted",
            "observed_at": datetime.datetime.fromisoformat(
                "2026-09-19T12:00:00+00:00"
            ).timestamp(),
            "source": "codex-session-record",
            "session_id": "rollout-old",
            "observation_id": "old-refusal",
            "progress": 10,
        },
    )
    codex_capacity_record(
        paired,
        {"primary": {"used_percent": 10}},
        progress=10,
        request_tokens=request_tokens,
        timestamp=timestamp,
        session="copied",
    )
    observed = supervision.capacity(bridge.home, directory, paired, "codex")
    assert observed["state"] == "exhausted"
    assert observed["session_id"] == "rollout-old"


def test_replayed_codex_usage_does_not_clear_exhaustion(bridge, repo, paired):
    path = codex_capacity_record(
        paired,
        {"primary": {"used_percent": 10}},
        progress=10,
        timestamp="2026-09-19T12:00:00Z",
    )
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-09-19T12:01:00Z",
                    "payload": {
                        "type": "error",
                        "message": "usage limit reached",
                    },
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-09-19T12:02:00Z",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 10}},
                        "rate_limits": {"primary": {"used_percent": 10}},
                    },
                }
            )
            + "\n"
        )
    participant = paired["participants"]["codex"]
    replayed = records.capacity_observation(bridge.home, participant)
    assert replayed is not None
    assert replayed["state"] == "exhausted"
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-09-19T12:03:00Z",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 11}},
                        "rate_limits": {"primary": {"used_percent": 10}},
                    },
                }
            )
            + "\n"
        )
    recovered = records.capacity_observation(bridge.home, participant)
    assert recovered is not None
    assert recovered["state"] == "available"
    assert recovered["progressed"] is True


def test_exhaustion_is_shared_only_by_an_explicit_account(bridge, repo):
    roster.define_credential(
        bridge.home,
        "shared",
        os.environ["CLAUDE_CONFIG_DIR"],
        [],
        [],
    )
    bridge.add_participant(repo, "claude-a", "claude", "shared")
    manifest = bridge.add_participant(repo, "claude-b", "claude", "shared")
    refused(manifest, "claude-a", 30)
    directory = Path(manifest["lanes"]["claude-a"]).parent
    observed = supervision.capacity(
        bridge.home, directory, manifest, "claude-b"
    )
    assert observed["state"] == "exhausted"
    assert observed["participant"] == "claude-a"


def test_reliable_reset_restores_persisted_capacity(bridge, repo, paired):
    directory = Path(paired["lanes"]["claude"]).parent
    supervision.record_capacity(
        directory,
        "claude",
        {
            "state": "exhausted",
            "observed_at": time.time() - 60,
            "reset_at": time.time() - 30,
            "source": "bounded-probe",
            "session_id": "session",
            "observation_id": "probe-1",
        },
    )
    observed = supervision.capacity(bridge.home, directory, paired, "claude")
    assert observed["state"] == "available"
    assert observed["source"] == "provider-reset"


def test_a_provider_that_publishes_nothing_skips_the_capacity_check(
    bridge, repo, paired
):
    registered(bridge, paired)
    directory = Path(paired["lanes"]["claude"]).parent
    alive(directory, "claude")
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["checks"]["capacity"] is None
    assert published["fit"] is True


def test_ordinary_transcript_text_is_not_capacity_evidence(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "claude")
    records_dir = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
    )
    records_dir.mkdir(parents=True)
    (records_dir / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-09-19T12:00:00Z",
                "message": {"content": "Document usage limit handling."},
            }
        )
        + "\n"
    )
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["checks"]["capacity"] is None
    assert published["capacity"]["state"] == "unknown"


def test_replayed_claude_message_does_not_clear_exhaustion(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    records_dir = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
    )
    records_dir.mkdir(parents=True)
    success = {
        "type": "assistant",
        "timestamp": "2026-09-19T12:00:00Z",
        "message": {"id": "message-1", "usage": {"output_tokens": 1}},
    }
    refusal = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "timestamp": "2026-09-19T12:01:00Z",
        "message": {"content": "usage limit reached"},
    }
    replay = {
        **success,
        "timestamp": "2026-09-19T12:02:00Z",
    }
    (records_dir / "session.jsonl").write_text(
        "\n".join(json.dumps(item) for item in (success, refusal, replay))
        + "\n"
    )
    observation = records.capacity_observation(
        bridge.home, paired["participants"]["claude"]
    )
    assert observation is not None
    assert observation["state"] == "exhausted"


def test_exhaustion_survives_native_transcript_rotation(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "claude")
    refused(paired, "claude", 30)
    supervision.poll(bridge.home, directory)
    records_dir = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
    )
    time.sleep(0.01)
    (records_dir / "rotated.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-09-19T12:00:00Z",
                "message": {"content": "continue"},
            }
        )
        + "\n"
    )
    supervision.poll(bridge.home, directory)
    published = supervision.published_work(directory, "claude")
    assert published["capacity"]["state"] == "exhausted"


def test_single_exhausted_claim_is_a_recovery_candidate(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "42")
    ledger = issues.snapshot(lane.parent)
    results = {
        "claude": {
            "fit": False,
            "capacity": {
                "state": "exhausted",
                "reset_at": 200.0,
                "source": "claude-session-record",
                "session_id": "session",
                "observation_id": "event",
            },
        },
        "codex": {"fit": True, "capacity": {"state": "available"}},
    }
    candidates = supervision.stranded_claims(paired, ledger, results)
    assert candidates == [
        {
            "issue": "42",
            "owner": "claude",
            "eligible_peers": ["codex"],
            "reason": "claude provider capacity is exhausted until 200",
            "next_action": "request a recorded recovery transition",
            "reset_at": 200.0,
            "source": "claude-session-record",
            "session_id": "session",
            "observation_id": "event",
        }
    ]


def test_exhausted_claim_without_a_peer_remains_a_wait_obligation(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    bridge.issue(lane, "claim", "42")
    ledger = issues.snapshot(lane.parent)
    results = {
        name: {
            "fit": False,
            "capacity": {
                "state": "exhausted",
                "source": f"{name}-session-record",
                "session_id": name,
                "observation_id": name,
            },
        }
        for name in paired["participants"]
    }
    candidates = supervision.stranded_claims(paired, ledger, results)
    assert candidates[0]["eligible_peers"] == []
    assert candidates[0]["next_action"] == (
        "wait for provider recovery or an eligible peer"
    )


def test_stranded_claim_snapshot_is_authoritative_and_clearable(
    bridge, repo, paired
):
    directory = Path(paired["lanes"]["claude"]).parent
    candidate = {
        "issue": "42",
        "owner": "claude",
        "eligible_peers": ["codex"],
        "reason": "claude provider capacity is exhausted",
        "next_action": "request a recorded recovery transition",
        "reset_at": None,
        "source": "claude-session-record",
        "session_id": "session",
        "observation_id": "event",
    }
    supervision.record_stranded_claims(directory, [candidate])
    assert supervision.published_stranded_claim(directory, "42") == candidate
    supervision.record_stranded_claims(directory, [])
    assert supervision.published_stranded_claim(directory, "42") is None


def test_a_stopped_lane_is_unfit_and_never_named_by_a_rebalance(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "codex", activity="stopped")
    turn_ended(directory, "codex", 3600)
    bridge.issue(lane, "claim", "2")
    bridge.issue(lane, "claim", "3")
    supervision.poll(bridge.home, directory)
    assert supervision.published_work(directory, "codex")["failed"] == [
        "session"
    ]
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "continue"
    assert "codex" not in offer["text"]


def test_a_busy_lane_is_told_which_fit_peer_has_been_idle(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "codex")
    turn_ended(directory, "codex", 3600)
    bridge.issue(lane, "claim", "2")
    bridge.issue(lane, "claim", "3")
    supervision.poll(bridge.home, directory)
    assert supervision.idle_seconds(directory, "codex") >= 3600
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "rebalance"
    assert "codex" in offer["text"]
    assert "#2" in offer["text"] and "#3" in offer["text"]
    assert issues.snapshot(directory)["issues"]["2"]["owner"] == "claude"


def test_an_idle_claim_with_a_countable_backlog_offers_one_split(
    bridge, repo, paired, monkeypatch
):
    _, _, directory = holding_a_backlog(bridge, paired, monkeypatch, 129)

    supervision.poll(bridge.home, directory)
    offer = offer_for(directory, "claude")
    assert offer["kind"] == "split"
    assert offer["issues"] == ["2"]
    assert "129 units" in offer["text"]
    assert "codex" in offer["text"]
    first = supervision.published_work(directory, "claude")["dispatch"]

    supervision.poll(bridge.home, directory)
    repeated = supervision.published_work(directory, "claude")
    assert repeated["offer"]["id"] == offer["id"]
    assert repeated["dispatch"]["generation"] == first["generation"]
    assert issues.snapshot(directory)["issues"]["2"]["owner"] == "claude"
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["offer_kind"] == "split"
    assert "split offer pending" in "\n".join(dashboard.render(view))
    reported = views.frame(view)["projects"][0]["participants"]
    holder = next(row for row in reported if row["participant"] == "claude")
    assert holder["work_offer"] == "split"
    assert holder["work_dispatch"]["state"] == repeated["dispatch"]["state"]


def test_a_claim_with_no_recorded_backlog_offers_no_split(
    bridge, repo, paired, monkeypatch
):
    _, _, directory = holding_a_backlog(bridge, paired, monkeypatch, None)

    supervision.poll(bridge.home, directory)
    assert offer_for(directory, "claude")["kind"] == "continue"


def test_a_split_names_no_recipient_parked_on_a_dialog(
    bridge, repo, paired, monkeypatch
):
    _, _, directory = holding_a_backlog(bridge, paired, monkeypatch, 129)
    write_json(
        directory / "codex-wake.json",
        {"result": sorted(supervision.DIALOG_WAKES)[0]},
    )

    supervision.poll(bridge.home, directory)
    assert offer_for(directory, "claude")["kind"] == "continue"


def test_the_checkpoint_carries_one_offer_and_then_stays_quiet(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    write_json(directory / "claude-identity.json", {"name": "claude"})
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    supervision.poll(bridge.home, directory)
    stop = {
        "hook_event_name": "Stop",
        "session_id": "claude-work",
        "cwd": str(lane),
    }
    first = checkpoint(bridge.home, directory, "claude", stop)
    assert first["decision"] == "block"
    assert "Work offer" in first["reason"]
    assert len(first["reason"].encode()) <= 1536
    assert checkpoint(bridge.home, directory, "claude", stop) == {}


def test_an_actionable_work_offer_wakes_an_old_idle_lane(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    requested = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda path, name: requested.append(name) or "accepted",
    )

    supervision.poll(bridge.home, directory)

    assert requested == ["claude"]
    published = supervision.published_work(directory, "claude")
    wake = json.loads((directory / "claude-wake.json").read_text())
    assert wake["backlog"] == [
        f"work:{published['offer']['id']}:{published['offer']['progress']}"
    ]
    assert published["dispatch"]["state"] == "awaiting_progress"
    assert published["dispatch"]["attempts"] == 1


def test_a_paused_lane_retains_work_without_receiving_a_wake(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    manifest = json.loads((directory / "project.json").read_text())
    manifest["participants"]["claude"]["paused"] = True
    write_json(directory / "project.json", manifest)
    monkeypatch.setattr(
        terminal,
        "request",
        lambda *args: pytest.fail("woke paused work"),
    )

    supervision.poll(bridge.home, directory)

    published = supervision.published_work(directory, "claude")
    assert published["offer"]["kind"] == "pull"
    assert published["dispatch"]["state"] == "pending"
    assert not (directory / "claude-wake.json").exists()


def test_claiming_selected_work_clears_the_dispatch(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(lane, "claim", "9")
    bridge.issue(lane, "release", "9")
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    supervision.work(bridge.home, directory, manifest, config)
    original = supervision.published_work(directory, "claude")["dispatch"]

    bridge.issue(lane, "claim", "9")
    supervision.work(bridge.home, directory, manifest, config)

    published = supervision.published_work(directory, "claude")
    assert published["offer"]["kind"] == "continue"
    assert published["dispatch"]["generation"] != original["generation"]
    assert published["dispatch"]["attempts"] == 0


def test_stale_published_work_is_rechecked_before_wake(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "9")
    bridge.issue(peer, "release", "9")
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    supervision.work(bridge.home, directory, manifest, config)
    assert offer_for(directory, "claude")["issues"] == ["9"]
    bridge.issue(peer, "claim", "9")
    monkeypatch.setattr(
        terminal,
        "request",
        lambda *args: pytest.fail("woke for stale work"),
    )

    supervision.wake(
        bridge.home,
        directory,
        manifest,
        "claude",
        supervision.presence(directory, "claude", config["inactive_after"]),
        config,
    )

    assert not (directory / "claude-wake.json").exists()


def test_direct_wake_honors_lane_opt_out(bridge, repo, paired, monkeypatch):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    supervision.work(bridge.home, directory, manifest, config)
    manifest["participants"]["claude"]["wake"] = False
    monkeypatch.setattr(
        terminal,
        "request",
        lambda *args: pytest.fail("woke opted-out lane"),
    )

    supervision.wake(
        bridge.home,
        directory,
        manifest,
        "claude",
        supervision.presence(directory, "claude", config["inactive_after"]),
        config,
    )

    assert not (directory / "claude-wake.json").exists()


def test_wake_rechecks_persisted_pause_before_request(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    stale_manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, stale_manifest)
    supervision.work(bridge.home, directory, stale_manifest, config)
    persisted = json.loads((directory / "project.json").read_text())
    persisted["participants"]["claude"]["paused"] = True
    write_json(directory / "project.json", persisted)
    monkeypatch.setattr(
        terminal,
        "request",
        lambda *args: pytest.fail("woke a newly paused lane"),
    )

    supervision.wake(
        bridge.home,
        directory,
        stale_manifest,
        "claude",
        supervision.presence(directory, "claude", config["inactive_after"]),
        config,
    )

    wake = json.loads((directory / "claude-wake.json").read_text())
    assert wake["result"] == "busy:stale"
    assert wake["attempts"] == 0


def test_delivery_without_work_progress_retries_then_escalates(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    requested = []
    monkeypatch.setattr(
        terminal,
        "request",
        lambda path, name: requested.append(name) or "accepted",
    )
    supervision.poll(bridge.home, directory)
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    observed = supervision.presence(
        directory, "claude", config["inactive_after"]
    )
    for _ in range(3):
        wake_path = directory / "claude-wake.json"
        wake = json.loads(wake_path.read_text())
        wake["at"] = 0
        write_json(wake_path, wake)
        published = supervision.published_work(directory, "claude")
        published["dispatch"]["updated_at"] = 0
        write_json(directory / "claude-work.json", published)
        supervision.wake(
            bridge.home,
            directory,
            manifest,
            "claude",
            observed,
            config,
        )

    assert requested == ["claude", "claude", "claude"]
    published = supervision.published_work(directory, "claude")
    dispatch = published["dispatch"]
    assert dispatch["state"] == "escalated"
    assert dispatch["attempts"] == 3
    assert "#2" in dispatch["last_result"]
    assert "last result: accepted" in dispatch["last_result"]
    assert "next action:" in dispatch["last_result"]
    view = dashboard.collect(bridge.home, False, {})
    lines = "\n".join(dashboard.render(view))
    assert dispatch["last_result"] in lines
    participants = views.frame(view)["projects"][0]["participants"]
    reported = next(
        item for item in participants if item["participant"] == "claude"
    )
    assert reported["work_dispatch"]["state"] == "escalated"
    supervision.work(bridge.home, directory, manifest, config)
    restarted = supervision.published_work(directory, "claude")["dispatch"]
    assert restarted["state"] == "escalated"
    assert restarted["attempts"] == 3


def test_a_lane_working_between_wakes_is_never_escalated(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    monkeypatch.setattr(terminal, "request", lambda path, name: "accepted")
    supervision.poll(bridge.home, directory)
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    observed = supervision.presence(
        directory, "claude", config["inactive_after"]
    )
    for _ in range(5):
        tool_used(directory, "claude")
        unthrottle(directory, "claude")
        supervision.wake(
            bridge.home, directory, manifest, "claude", observed, config
        )
        record = json.loads((directory / "claude-wake.json").read_text())
        assert record["attempts"] <= 1

    dispatch = supervision.published_work(directory, "claude")["dispatch"]
    assert dispatch["state"] == "awaiting_progress"


def test_an_escalation_holds_until_the_lane_records_activity(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude", updated=time.time() - 500)
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    monkeypatch.setattr(terminal, "request", lambda path, name: "accepted")
    supervision.poll(bridge.home, directory)
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    observed = supervision.presence(
        directory, "claude", config["inactive_after"]
    )
    for _ in range(4):
        unthrottle(directory, "claude")
        supervision.wake(
            bridge.home, directory, manifest, "claude", observed, config
        )
    wake_path = directory / "claude-wake.json"
    escalated = json.loads(wake_path.read_text())
    dispatch = supervision.published_work(directory, "claude")["dispatch"]
    assert dispatch["state"] == "escalated"

    unthrottle(directory, "claude")
    supervision.wake(
        bridge.home, directory, manifest, "claude", observed, config
    )
    held = json.loads(wake_path.read_text())
    assert held["escalated_at"] == escalated["escalated_at"]

    tool_used(directory, "claude")
    unthrottle(directory, "claude")
    supervision.wake(
        bridge.home, directory, manifest, "claude", observed, config
    )

    cleared = supervision.published_work(directory, "claude")["dispatch"]
    assert cleared["state"] == "awaiting_progress"
    assert cleared["attempts"] == 1
    record = json.loads(wake_path.read_text())
    assert "escalated_at" not in record
    assert record["attempts"] == 1


def test_issue_progress_resets_a_rebalance_dispatch_generation(
    bridge, repo, paired
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    alive(directory, "codex")
    turn_ended(directory, "codex", 3600)
    bridge.issue(lane, "claim", "2")
    bridge.issue(lane, "claim", "3")
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    supervision.work(bridge.home, directory, manifest, config)
    before = supervision.published_work(directory, "claude")
    supervision._write_work_dispatch(
        directory,
        "claude",
        before["offer"],
        2,
        "accepted",
        "awaiting_progress",
    )

    bridge.issue(lane, "offer", "2", to="codex", summary="Take issue 2")
    supervision.work(bridge.home, directory, manifest, config)

    after = supervision.published_work(directory, "claude")
    assert after["offer"]["id"] == before["offer"]["id"]
    assert after["offer"]["progress"] != before["offer"]["progress"]
    assert after["dispatch"]["state"] == "pending"
    assert after["dispatch"]["attempts"] == 0


def test_work_poll_cannot_erase_a_concurrent_dispatch_attempt(
    bridge, repo, paired, monkeypatch
):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    manifest = json.loads((directory / "project.json").read_text())
    config = supervision.configuration(bridge.home, manifest)
    supervision.work(bridge.home, directory, manifest, config)
    published = supervision.published_work(directory, "claude")
    offer = published["offer"]
    published["obsolete"] = True
    write_json(directory / "claude-work.json", published)
    paused = threading.Event()
    release = threading.Event()
    original_write = supervision.write_json

    def delayed_write(path, value):
        if (
            path.name == "claude-work.json"
            and threading.current_thread().name == "work-publisher"
        ):
            paused.set()
            assert release.wait(2)
        original_write(path, value)

    monkeypatch.setattr(supervision, "write_json", delayed_write)
    publisher = threading.Thread(
        target=supervision.work,
        args=(bridge.home, directory, manifest, config),
        name="work-publisher",
    )
    publisher.start()
    assert paused.wait(2)
    dispatch_started = threading.Event()

    def dispatch() -> None:
        dispatch_started.set()
        supervision._write_work_dispatch(
            directory,
            "claude",
            offer,
            1,
            "accepted",
            "awaiting_progress",
        )

    dispatcher = threading.Thread(
        target=dispatch,
    )
    dispatcher.start()
    assert dispatch_started.wait(2)
    assert dispatcher.is_alive()
    release.set()
    publisher.join(2)
    dispatcher.join(2)

    assert not publisher.is_alive()
    assert not dispatcher.is_alive()
    final = supervision.published_work(directory, "claude")
    assert final["dispatch"]["attempts"] == 1
    assert final["dispatch"]["state"] == "awaiting_progress"


def test_selected_wake_prompt_names_current_work_and_issues(tmp_path):
    issue = {
        "owner": None,
        "claim_id": None,
        "blocked_by": [],
        "offer": None,
        "execution": {"state": "queued"},
    }
    bindings = [
        {
            "issue": "42",
            "owner": None,
            "claim_id": None,
            "blocked_by": [],
            "offer": None,
            "execution": {"state": "queued"},
        }
    ]
    flags = {
        "present": True,
        "enabled": True,
        "participant_wake": True,
        "paused": False,
    }
    write_json(
        tmp_path / "project.json",
        {"participants": {"codex": {}}, "supervision": {}},
    )
    write_json(tmp_path / "issues.json", {"issues": {"42": issue}})
    write_json(
        tmp_path / "codex-wake-work.json",
        {
            "flags": flags,
            "bindings": bindings,
            "offer": {
                "id": "offer-123",
                "issues": ["42"],
                "text": "Work offer. Claim one yourself.",
            },
        },
    )

    prompt = terminal.selected_prompt(tmp_path, "codex")

    assert prompt is not None
    assert "offer-123" in prompt
    assert "#42" in prompt
    assert "Claim one yourself" in prompt


def test_selected_prompt_refuses_changed_owner_or_pause(tmp_path):
    issue = {
        "owner": "codex",
        "claim_id": "claim-1",
        "blocked_by": [],
        "offer": None,
        "execution": {"state": "running"},
    }
    offer = {
        "id": "offer-123",
        "issues": ["42"],
        "text": "Continue authorized work.",
        "progress": supervision._work_progress(
            {"issues": {"42": issue}}, ["42"]
        ),
    }
    write_json(
        tmp_path / "project.json",
        {"participants": {"codex": {}}, "supervision": {}},
    )
    write_json(tmp_path / "issues.json", {"issues": {"42": issue}})
    write_json(
        tmp_path / "codex-wake-work.json",
        {
            "offer": offer,
            "bindings": supervision._work_bindings(
                {"issues": {"42": issue}}, ["42"]
            ),
            "flags": {
                "present": True,
                "enabled": True,
                "participant_wake": True,
                "paused": False,
            },
        },
    )

    issue["owner"] = "claude"
    write_json(tmp_path / "issues.json", {"issues": {"42": issue}})
    assert terminal.selected_prompt(tmp_path, "codex", tmp_path) is None

    issue["owner"] = "codex"
    write_json(tmp_path / "issues.json", {"issues": {"42": issue}})
    write_json(
        tmp_path / "project.json",
        {
            "participants": {"codex": {"paused": True}},
            "supervision": {},
        },
    )
    assert terminal.selected_prompt(tmp_path, "codex", tmp_path) is None


def test_top_reports_the_fit_result_and_a_pending_offer(bridge, repo, paired):
    registered(bridge, paired)
    lane = Path(paired["lanes"]["claude"])
    peer = Path(paired["lanes"]["codex"])
    directory = lane.parent
    alive(directory, "claude")
    bridge.issue(peer, "claim", "2")
    bridge.issue(peer, "claim", "3")
    supervision.poll(bridge.home, directory)
    view = dashboard.collect(bridge.home, False, {})
    rows = {row["participant"]: row for row in view["projects"][0]["rows"]}
    assert rows["claude"]["fit"] is True
    assert rows["claude"]["work_offer"] is True
    assert rows["claude"]["offer_kind"] == "pull"
    lines = "\n".join(dashboard.render(view))
    assert "FIT" in lines
    assert "pull offer pending" in lines
    reported = views.frame(view)["projects"][0]["participants"]
    assert reported[0]["fit"] is True
    assert reported[0]["work_offer"] == "pull"
