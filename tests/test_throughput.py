"""Checks every lane throughput counter against a temporary repository."""

import datetime
import json
import sys
from pathlib import Path

import pytest

from agent_parley import (
    cli,
    decisions,
    lanes,
    metrics,
    records,
    roster,
    store,
)
from agent_parley.state import BridgeError, write_json

HOUR = 3600.0


def iso(moment):
    """Formats a Unix time the way native clients stamp their records."""
    return (
        datetime.datetime.fromtimestamp(moment, datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def lines(path, entries):
    """Writes JSON lines to a native session record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def claude_message(identifier, moment, entered, produced):
    """Builds one Claude transcript record carrying usage."""
    return {
        "timestamp": iso(moment),
        "message": {
            "id": identifier,
            "usage": {
                "input_tokens": entered,
                "cache_read_input_tokens": 1,
                "output_tokens": produced,
            },
        },
    }


def resolved(holder, number, merged):
    """Builds one ledger record whose claim a merged pull request ended."""
    return {
        "owner": None,
        "history": [
            {"action": "claim", "at": merged - 600, "owner": holder},
            {
                "action": "resolve",
                "at": merged,
                "outcome": "complete",
                "holder": holder,
                "evidence": {
                    "state": "MERGED",
                    "closed_at": merged,
                    "pull_request": number + 100,
                },
            },
        ],
    }


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return capsys.readouterr().out


@pytest.fixture
def window(bridge, repo, paired):
    """Records one window of lane activity and returns its bounds."""
    directory = bridge.project(repo)[1]
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        store.register(bridge.home, paired["root"], name)
    now = metrics.time.time()
    since = now - HOUR
    steps = (
        (now - 7200, lanes.WORKING, ""),
        (now - 3240, lanes.IDLE, ""),
        (now - 2880, lanes.BLOCKED, lanes.APPROVAL),
        (now - 2520, lanes.WORKING, ""),
        (now - 2160, lanes.BLOCKED, lanes.CAPACITY),
        (now - 1800, lanes.STOPPED, ""),
        (now - 1440, lanes.STARTING, ""),
        (now - 1080, lanes.WORKING, ""),
    )
    with store.connect(bridge.home, write=True) as db:
        for moment, state, cause in steps:
            lanes.transition(
                db, paired["root"], "claude", state, cause=cause, now=moment
            )
        lanes.transition(
            db, paired["root"], "codex", lanes.WORKING, now=now - 1800
        )
    write_json(
        directory / "issues.json",
        {
            "revision": 1,
            "issues": {
                "41": resolved("claude", 41, now - 7200),
                "42": resolved("claude", 42, now - 1200),
                "43": resolved("codex", 43, now - 600),
            },
        },
    )
    lane = paired["lanes"]["claude"]
    lines(
        Path(records.DEFAULT_HOMES["claude"])
        / "projects"
        / records.UNSAFE.sub("-", str(lane))
        / "session.jsonl",
        [
            {"type": "user", "timestamp": iso(now - 1000)},
            claude_message("m0", now - 7200, 1000, 1000),
            claude_message("m1", now - 1000, 100, 10),
            claude_message("m1", now - 990, 100, 10),
            claude_message("m2", now - 500, 200, 20),
        ],
    )
    metrics.record_report(
        directory, "claude", {"kind": "approval", "decision": "approve"}
    )
    record = decisions.open_or_refresh(
        directory,
        project=paired["root"],
        lane="claude",
        kind="prompt",
        key="k",
        question="Allow?",
        options=["yes", "no"],
        now=now - 900,
    )
    decisions.answer(directory, record["id"], "yes", "operator", now=now - 800)
    bridge.say(repo, "claude", "First")
    bridge.say(repo, "claude", "Second")
    bridge.pause(repo, "claude")
    bridge.pause(repo, "claude", resume=True)
    bridge.report(lane, "ready", "Engine built", "", "12 tests passed")
    return {"since": since, "now": now, "root": paired["root"]}


def test_lane_hours_follow_the_recorded_transitions(bridge, window):
    with store.connect(bridge.home) as db:
        reading = metrics.lane_hours(
            db, window["root"], "claude", window["since"], window["now"]
        )
        codex = metrics.lane_hours(
            db, window["root"], "codex", window["since"], window["now"]
        )
        missing = metrics.lane_hours(
            db, window["root"], "nobody", window["since"], window["now"]
        )
    assert reading["seconds"] == pytest.approx(
        {
            "active": 2160.0,
            "idle": 360.0,
            "operator_wait": 360.0,
            "blocked": 360.0,
        }
    )
    assert reading["starts"] == 1
    assert reading["complete"] is True
    assert codex["seconds"]["active"] == pytest.approx(1800.0)
    assert codex["complete"] is True
    assert missing["complete"] is False
    assert (
        metrics.lane_hours(None, window["root"], "claude", 0.0, 1.0)["complete"]
        is False
    )


def test_history_that_starts_inside_the_window_is_incomplete(bridge, window):
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "DELETE FROM lane_events WHERE lane='claude' AND ts<?",
            (window["since"],),
        )
    with store.connect(bridge.home) as db:
        reading = metrics.lane_hours(
            db, window["root"], "claude", window["since"], window["now"]
        )
    assert reading["complete"] is False
    assert reading["seconds"]["active"] == pytest.approx(1800.0)


def test_every_counter_is_reported_per_lane_and_project(bridge, window):
    reading = metrics.throughput(bridge.home, window["since"])
    project = reading["projects"][0]
    claude = project["lanes"]["claude"]
    assert claude["merged_issues"] == 1
    assert claude["active_hours"] == 0.6
    assert claude["idle_hours"] == 0.1
    assert claude["operator_wait_hours"] == 0.1
    assert claude["blocked_hours"] == 0.1
    assert claude["input_tokens"] == 302
    assert claude["output_tokens"] == 30
    assert claude["approvals"] == 1
    assert claude["decisions"] == 1
    assert claude["operator_mail"] == 2
    assert claude["resumes"] == 1
    assert claude["operator_touches"] == 5
    assert claude["unattributed_starts"] == 1
    assert claude["merge_waits"] == 1
    assert claude["merge_waits_pending"] == 1
    codex = project["lanes"]["codex"]
    assert codex["merged_issues"] == 1
    assert codex["input_tokens"] == 0
    totals = project["totals"]
    assert totals["merged_issues"] == 2
    assert totals["operator_mail"] == 2
    assert totals["operator_touches"] == 5
    assert totals["input_tokens"] == 302
    assert totals["history_complete"] is True


def test_the_merge_queue_wait_ends_at_integration(bridge, repo, window):
    directory = bridge.project(repo)[1]
    metrics.record_report(
        directory, "claude", {"kind": "integration", "action": "merge"}
    )
    claude = metrics.throughput(bridge.home, window["since"])["projects"][0][
        "lanes"
    ]["claude"]
    assert claude["merge_waits"] == 1
    assert claude["merge_waits_pending"] == 0


def test_running_totals_count_only_the_window(tmp_path):
    codex = lines(
        tmp_path / "rollout.jsonl",
        [
            {
                "timestamp": iso(50),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                        }
                    }
                },
            },
            {
                "timestamp": iso(150),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 400,
                            "output_tokens": 40,
                        }
                    }
                },
            },
            {
                "timestamp": iso(300),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 900,
                            "output_tokens": 90,
                        }
                    }
                },
            },
        ],
    )
    assert metrics._file_tokens(codex, "codex", 100.0, 200.0) == (300, 30, 0)
    copilot = lines(
        tmp_path / "events.jsonl",
        [
            {
                "type": "session.shutdown",
                "timestamp": iso(150),
                "data": {
                    "modelMetrics": {
                        "a": {"usage": {"inputTokens": 7, "outputTokens": 3}},
                        "b": {"usage": {"inputTokens": 1, "outputTokens": 1}},
                    }
                },
            }
        ],
    )
    assert metrics._file_tokens(copilot, "copilot", 100.0, 200.0) == (8, 4, 0)
    gemini = lines(
        tmp_path / "chat.jsonl",
        [
            {"id": "g1", "type": "gemini", "timestamp": iso(150)},
            {
                "id": "g1",
                "type": "gemini",
                "timestamp": iso(150),
                "tokens": {"input": 5, "output": 2, "total": 7},
            },
            {
                "id": "g1",
                "type": "gemini",
                "timestamp": iso(160),
                "tokens": {"input": 5, "output": 2, "total": 7},
            },
            {
                "id": "g2",
                "type": "gemini",
                "tokens": {"input": 9, "output": 9, "total": 18},
            },
        ],
    )
    assert metrics._file_tokens(gemini, "gemini", 100.0, 200.0) == (5, 2, 0)


def test_a_provider_without_a_split_reader_reports_no_tokens(bridge):
    lane = {"provider": "opencode", "lane": "/lane"}
    assert metrics.native_tokens(bridge.home, lane, 0.0, 1.0) is None
    assert metrics.native_tokens(bridge.home, {"provider": "amp"}, 0, 1) is None


def test_the_command_prints_text_and_json(bridge, window, monkeypatch, capsys):
    home = ["--home", str(bridge.home)]
    start = iso(window["since"])
    printed = run(
        monkeypatch, capsys, *home, "metrics", "throughput", "--since", start
    )
    assert "MERGED" in printed and "claude" in printed
    assert "1 approvals, 1 decisions, 2 mail, 1 resumes" in printed
    assert "1 lane starts unattributed" in printed
    document = json.loads(
        run(
            monkeypatch,
            capsys,
            *home,
            "metrics",
            "throughput",
            "--since",
            "2h",
            "--json",
        )
    )
    claude = document["projects"][0]["lanes"]["claude"]
    assert claude["merged_issues"] == 1
    assert claude["input_tokens"] == 302


def test_a_quiet_window_prints_no_table(bridge, paired):
    reading = metrics.throughput(bridge.home, metrics.time.time())
    printed = metrics.describe_throughput(reading)
    assert "MERGED" not in printed
    assert "1 projects recorded nothing in the window." in printed


def test_a_window_start_is_an_instant_or_a_window():
    assert cli.instant("2026-10-15T09:01:00Z") == 1792054860.0
    assert cli.instant("1h") == pytest.approx(metrics.time.time() - HOUR, 5)
    with pytest.raises(ValueError):
        cli.instant("yesterday")


RATES = {"input": 1000, "output": 5000, "cache_read": 100}


@pytest.fixture
def priced(bridge, repo, window, monkeypatch):
    """Names the claude lane's model, prices it and records one finding."""
    directory = bridge.project(repo)[1]
    manifest = json.loads((directory / "project.json").read_text())
    manifest["participants"]["claude"]["model"] = "opus"
    write_json(directory / "project.json", manifest)
    write_json(
        directory / "reviews.json",
        {
            "next": 2,
            "rounds": {
                "r1": {
                    "id": "r1",
                    "state": "returned",
                    "builder": "codex",
                    "reviewer": "claude",
                    "returned": window["now"] - 100,
                    "findings": 2,
                }
            },
        },
    )
    monkeypatch.chdir(repo)
    bridge.price(repo, "claude", None, "opus", RATES)
    return window


def test_no_price_table_means_no_cost_output(
    bridge, window, monkeypatch, capsys
):
    reading = metrics.cost(bridge.home, window["since"])
    assert reading["price_table_version"] is None
    assert "projects" not in reading
    assert "No price table is recorded" in reading["message"]
    assert "provider price" in reading["message"]
    home = ["--home", str(bridge.home)]
    printed = run(
        monkeypatch, capsys, *home, "metrics", "cost", "--since", "2h"
    )
    assert printed == reading["message"] + "\n"
    document = json.loads(
        run(
            monkeypatch,
            capsys,
            *home,
            "metrics",
            "cost",
            "--since",
            "2h",
            "--json",
        )
    )
    assert document["price_table_version"] is None
    assert "not billed spend" in document["estimate"]
    assert "projects" not in document


def test_cost_joins_priced_tokens_with_shipped_units(bridge, priced):
    reading = metrics.cost(bridge.home, priced["since"])
    lanes = reading["projects"][0]["lanes"]
    claude = lanes["claude"]
    assert claude["estimated_cost"] == 0.4502
    assert (claude["issues"], claude["pull_requests"]) == (1, 1)
    assert claude["findings"] == 2
    assert claude["cost_per_issue"] == 0.4502
    assert claude["cost_per_pull_request"] == 0.4502
    assert claude["cost_per_finding"] == 0.2251
    group = reading["projects"][0]["providers"]["claude"]
    assert group["estimated_cost"] == 0.4502
    assert group["cost_per_issue"] == 0.4502
    assert group["unknown_lanes"] == []


def test_a_lane_without_a_price_row_is_unknown_not_zero(bridge, priced):
    reading = metrics.cost(bridge.home, priced["since"])
    project = reading["projects"][0]
    codex = project["lanes"]["codex"]
    assert codex["estimated_cost"] is None
    assert codex["cost_per_issue"] is None
    assert "no price row for codex" in codex["unknown_reason"]
    assert project["providers"]["codex"]["estimated_cost"] is None
    assert project["providers"]["codex"]["unknown_lanes"] == ["codex"]
    printed = metrics.describe_cost(reading)
    assert "codex: cost unknown, no price row for codex" in printed
    assert "unknown" in printed


def test_a_provider_without_token_counts_is_unknown_not_zero():
    unpriced = {"amp": {"default": RATES}}
    row = metrics._lane_cost_row(
        {"provider": "amp", "model": "default"},
        None,
        unpriced,
        {"issues": 1, "pull_requests": 1, "findings": 1},
    )
    assert row["estimated_cost"] is None
    assert row["input_tokens"] is None
    assert row["cost_per_issue"] is None
    assert "no token count" in row["unknown_reason"]
    assert metrics.lane_cost(None, RATES) is None
    tokens = {"input": 10, "output": 1, "cache_read": 4}
    assert metrics.lane_cost(tokens, None) is None
    assert metrics.lane_cost(tokens, RATES) == pytest.approx(0.0114)


def test_the_command_labels_every_format_and_names_the_version(
    bridge, repo, priced, monkeypatch, capsys
):
    home = ["--home", str(bridge.home)]
    arguments = [*home, "metrics", "cost", "--since", "2h"]
    document = json.loads(run(monkeypatch, capsys, *arguments, "--json"))
    version = document["price_table_version"]
    assert len(version) == 12
    assert "not billed spend" in document["estimate"]
    assert "operator's recorded prices" in document["estimate"]
    printed = run(monkeypatch, capsys, *arguments)
    assert document["estimate"] in printed
    assert f"Price table version {version}." in printed
    assert "PER_ISSUE" in printed
    bridge.price(repo, "claude", None, "opus", {**RATES, "output": 1})
    changed = json.loads(run(monkeypatch, capsys, *arguments, "--json"))
    assert changed["price_table_version"] != version


def test_model_rates_are_set_from_the_command_line(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.chdir(repo)
    home = ["--home", str(bridge.home)]
    printed = run(
        monkeypatch,
        capsys,
        *home,
        "provider",
        "price",
        "claude",
        "--model",
        "opus",
        "--input",
        "3",
        "--output",
        "15",
        "--cache-read",
        "0.3",
    )
    assert "model opus per million tokens: input 3, output 15" in printed
    assert roster.price_table(bridge.home)["claude"]["opus"] == {
        "input": 3,
        "output": 15,
        "cache_read": 0.3,
    }
    shown = run(monkeypatch, capsys, *home, "provider", "price", "claude")
    assert "cache-read 0.3" in shown
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            *home,
            "provider",
            "price",
            "claude",
            "--model",
            "opus",
            "--input",
            "1",
        ],
    )
    with pytest.raises(SystemExit):
        cli.main()


def test_a_model_price_is_validated(bridge):
    for rates in (
        {"input": 1, "output": 2},
        {"input": -1, "output": 2, "cache_read": 0},
        {"input": "1", "output": 2, "cache_read": 0},
    ):
        with pytest.raises(BridgeError, match="model price"):
            roster.price({"models": {"opus": rates}})


def test_a_malformed_review_round_is_skipped_not_fatal(bridge, repo, priced):
    directory = bridge.project(repo)[1]
    good = json.loads((directory / "reviews.json").read_text())
    rounds = good["rounds"]
    rounds["r2"] = {**rounds["r1"], "id": "r2", "returned": "x"}
    rounds["r3"] = {**rounds["r1"], "id": "r3", "findings": "2a"}
    write_json(directory / "reviews.json", {**good, "next": 4})
    reading = metrics.cost(bridge.home, priced["since"])
    assert reading["projects"][0]["lanes"]["claude"]["findings"] == 2


def test_a_lane_with_no_recorded_model_uses_the_default_row(
    bridge, repo, window, monkeypatch
):
    directory = bridge.project(repo)[1]
    monkeypatch.chdir(repo)
    bridge.price(repo, "claude", None, "default", RATES)
    reading = metrics.cost(bridge.home, window["since"])
    claude = reading["projects"][0]["lanes"]["claude"]
    assert claude["model"] == ""
    assert claude["estimated_cost"] == 0.4502
    manifest = json.loads((directory / "project.json").read_text())
    manifest["participants"]["claude"]["model"] = "sonnet"
    write_json(directory / "project.json", manifest)
    reading = metrics.cost(bridge.home, window["since"])
    named = reading["projects"][0]["lanes"]["claude"]
    assert named["estimated_cost"] is None
    assert "no price row for claude model sonnet" in named["unknown_reason"]


def test_merges_after_the_window_end_are_not_counted(bridge, priced):
    ledger = {"issues": {"43": resolved("codex", 43, priced["now"] - 600)}}
    assert len(metrics._merges(ledger, 0.0)) == 1
    assert len(metrics._merges(ledger, 0.0, priced["now"])) == 1
    assert metrics._merges(ledger, 0.0, priced["now"] - 900) == {}
    early = metrics.cost(bridge.home, priced["since"], priced["now"] - 900)
    lanes = early["projects"][0]["lanes"]
    assert lanes["codex"]["pull_requests"] == 0
    assert lanes["claude"]["pull_requests"] == 1
    later = metrics.cost(bridge.home, priced["since"], priced["now"])
    assert later["projects"][0]["lanes"]["codex"]["pull_requests"] == 1


def test_providers_are_totalled_across_projects(bridge, priced):
    reading = metrics.cost(bridge.home, priced["since"])
    root = reading["projects"][0]["root"]
    assert reading["providers"]["claude"]["estimated_cost"] == 0.4502
    assert reading["providers"]["claude"]["cost_per_pull_request"] == 0.4502
    assert reading["providers"]["codex"]["unknown_lanes"] == [f"{root}:codex"]
    printed = metrics.describe_cost(reading)
    assert printed.index("All projects") < printed.index("Project ")
    assert "claude" in printed.split("Project ")[0]


def test_a_cached_codex_total_is_counted_as_a_window_delta(tmp_path):
    rollout = lines(
        tmp_path / "rollout.jsonl",
        [
            {
                "timestamp": iso(50),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 10,
                        }
                    }
                },
            },
            {
                "timestamp": iso(150),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 400,
                            "cached_input_tokens": 120,
                            "output_tokens": 40,
                        }
                    }
                },
            },
        ],
    )
    assert metrics._file_tokens(rollout, "codex", 100.0, 200.0) == (
        300,
        30,
        100,
    )


def test_a_recorded_run_is_priced_through_the_command(
    bridge, repo, paired, priced, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    manifest = json.loads((directory / "project.json").read_text())
    manifest["participants"]["codex"]["model"] = "gpt"
    write_json(directory / "project.json", manifest)
    bridge.price(repo, "codex", None, "gpt", RATES)
    day = datetime.date.today()
    lines(
        Path(records.DEFAULT_HOMES["codex"])
        / "sessions"
        / f"{day:%Y}"
        / f"{day:%m}"
        / f"{day:%d}"
        / "rollout-recorded.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"cwd": str(paired["lanes"]["codex"])},
            },
            {
                "timestamp": iso(priced["now"] - 8000),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 200,
                            "output_tokens": 100,
                        }
                    }
                },
            },
            {
                "timestamp": iso(priced["now"] - 500),
                "payload": {
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 3000,
                            "cached_input_tokens": 1200,
                            "output_tokens": 300,
                        }
                    }
                },
            },
        ],
    )
    document = json.loads(
        run(
            monkeypatch,
            capsys,
            "--home",
            str(bridge.home),
            "metrics",
            "cost",
            "--since",
            "2h",
            "--json",
        )
    )
    assert "not billed spend" in document["estimate"]
    assert document["price_table_version"] == roster.price_version(
        roster.price_table(bridge.home)
    )
    found = document["projects"][0]["lanes"]
    assert found["claude"]["estimated_cost"] == 0.4502
    assert found["claude"]["cost_per_finding"] == 0.2251
    assert found["codex"]["estimated_cost"] == 2.1
    assert found["codex"]["cost_per_pull_request"] == 2.1
    assert found["codex"]["cost_per_issue"] == 2.1
    assert found["codex"]["cost_per_finding"] is None
    assert document["providers"]["codex"]["estimated_cost"] == 2.1
    assert document["providers"]["claude"]["estimated_cost"] == 0.4502
