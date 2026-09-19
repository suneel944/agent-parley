"""Durable authorized-work lifecycle coverage."""

import json

import pytest

from agent_parley import issues, lifecycle, plan
from agent_parley.state import BridgeError


def claim(directory, number, agent="codex"):
    """Claims one issue through the serialized issue transition."""
    return issues.change(
        directory,
        agent,
        "claim",
        number,
        participants={"codex", "claude"},
    )


def test_plan_authorizes_blockers_and_completion_reconciles_dependencies(
    tmp_path,
):
    planned = tmp_path / "work.toml"
    planned.write_text(
        '[plan]\nname = "release"\n[dependencies]\n42 = [17]\n43 = [42]\n'
    )
    plan.apply(tmp_path, planned)
    ledger = issues.snapshot(tmp_path)

    assert lifecycle.actionable(ledger) == ["17"]
    first = claim(tmp_path, "17")
    lifecycle.record_report(
        tmp_path,
        "codex",
        "ready",
        "a" * 40,
        "",
    )
    lifecycle.complete(
        tmp_path,
        "17",
        first["claim_id"],
        "b" * 40,
        ["make", "check"],
    )

    ledger = issues.snapshot(tmp_path)
    completed = ledger["issues"]["17"]
    assert completed["owner"] is None
    assert completed["execution"]["state"] == lifecycle.COMPLETE
    assert completed["execution"]["gate"] == {
        "command": ["make", "check"],
        "status": "passed",
        "commit": "b" * 40,
        "at": completed["execution"]["gate"]["at"],
    }
    assert ledger["issues"]["42"]["blocked_by"] == []
    assert lifecycle.actionable(ledger) == ["42"]


def test_release_requeues_partial_work_but_never_recycles_completion(tmp_path):
    current = claim(tmp_path, "8")
    issues.change(
        tmp_path,
        "codex",
        "release",
        "8",
        participants={"codex"},
    )
    assert lifecycle.actionable(issues.snapshot(tmp_path)) == ["8"]

    current = claim(tmp_path, "8")
    lifecycle.record_report(
        tmp_path,
        "codex",
        "ready",
        "c" * 40,
        "",
    )
    lifecycle.complete(
        tmp_path,
        "8",
        current["claim_id"],
        "d" * 40,
        [],
    )

    assert lifecycle.actionable(issues.snapshot(tmp_path)) == []
    with pytest.raises(BridgeError, match="Verified complete"):
        claim(tmp_path, "8")


def test_old_generation_and_closed_pull_request_cannot_complete_new_claim(
    tmp_path,
):
    old = claim(tmp_path, "9")
    issues.change(
        tmp_path,
        "codex",
        "release",
        "9",
        participants={"codex"},
    )
    current = claim(tmp_path, "9")
    ledger = issues.snapshot(tmp_path)
    ledger["issues"]["9"]["handoff_prompt"] = {"trigger": "pull request ended"}
    (tmp_path / "issues.json").write_text(json.dumps(ledger))

    assert not issues.released(issues.snapshot(tmp_path)["issues"]["9"])
    with pytest.raises(BridgeError, match="changed ownership generation"):
        lifecycle.complete(
            tmp_path,
            "9",
            old["claim_id"],
            "e" * 40,
            [],
        )
    assert current["claim_id"] != old["claim_id"]


def test_blocked_and_ready_work_are_not_dispatched_as_continuations(tmp_path):
    claim(tmp_path, "10")
    lifecycle.record_report(
        tmp_path,
        "codex",
        "blocked",
        "f" * 40,
        "approval:merge",
    )
    assert lifecycle.actionable(issues.snapshot(tmp_path), "codex") == []

    lifecycle.record_report(
        tmp_path,
        "codex",
        "ready",
        "f" * 40,
        "",
    )
    ledger = issues.snapshot(tmp_path)
    assert lifecycle.actionable(ledger, "codex") == []
    assert ledger["issues"]["10"]["execution"]["next_action"] == (
        "verify and integrate"
    )
