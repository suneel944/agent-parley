"""Observes lane availability and reminds holders about waiting peers."""

import contextlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from agent_parley import forge, issues, process, roster, store, terminal
from agent_parley.state import BridgeError, lock, write_json

DEFAULTS = {
    "interval": 30,
    "inactive_after": 300,
    "prompts": True,
    "wake": True,
}


def settings(value: dict) -> dict:
    """Validates supervision settings stored outside the repository."""
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise BridgeError("Invalid supervision settings.")
    result = {**DEFAULTS, **value}
    for field in ("interval", "inactive_after"):
        if (
            type(result[field]) not in (int, float)
            or not 1 <= result[field] <= 86400
        ):
            raise BridgeError(f"{field} must be between 1 and 86400 seconds.")
    for field in ("prompts", "wake"):
        if type(result[field]) is not bool:
            raise BridgeError(f"{field} must be a boolean.")
    return result


def presence(directory: Path, name: str, inactive_after: float = 300) -> dict:
    """Derives availability from the recorded process and native checkpoints.

    A live but quiet session is distinguishable from a dead launcher. No
    heartbeat from the participant is required, and no ownership is inferred.

    Args:
        directory: Private project state directory.
        name: Participant name.
        inactive_after: Checkpoint age after which a live lane is unreachable.

    Returns:
        State, process liveness, last native activity time and observed age.
    """
    path = directory / f"{name}-activity.json"
    value = json.loads(path.read_text()) if path.exists() else {}
    alive = process.alive(value.get("session_pid"), value.get("session_ticks"))
    age = max(0, time.time() - value.get("updated", 0))
    state = "active" if alive and age <= inactive_after else "unreachable"
    return {
        "state": state,
        "process_alive": alive,
        "last_active": value.get("updated"),
        "age_seconds": int(age),
    }


def configuration(home: Path, manifest: dict) -> dict:
    """Resolves project settings while honoring the global wake opt-out."""
    path = home / "supervision.json"
    global_config = settings(
        json.loads(path.read_text()) if path.exists() else {}
    )
    config = settings({**global_config, **manifest.get("supervision", {})})
    config["wake"] = config["wake"] and global_config["wake"]
    config["prompts"] = config["prompts"] and global_config["prompts"]
    return config


def reminders(directory: Path, manifest: dict, closed: set[str]) -> None:
    """Records idempotent reminders without releasing or transferring claims.

    Args:
        directory: Private project state directory.
        manifest: Current participant manifest.
        closed: Issues whose lane pull request is observed merged or closed.
    """
    after_message_id = 0
    home = directory.parent.parent
    if (home / store.DATABASE).exists():
        with store.connect(home) as db:
            after_message_id = db.execute(
                "SELECT coalesce(max(id),0) FROM messages"
            ).fetchone()[0]
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        changed = False
        for number, record in ledger["issues"].items():
            waiting = sorted(
                {
                    other["owner"]
                    for other in ledger["issues"].values()
                    if number in other.get("blocked_by", [])
                    and other.get("owner")
                }
            )
            if not waiting and number not in closed:
                continue
            history = record.get("history", [])
            if not history:
                continue
            released = history and history[-1]["action"] == "release"
            holder = record.get("owner") or (
                history[-1]["actor"] if released else None
            )
            if holder not in manifest["participants"]:
                continue
            if not released and number not in closed:
                continue
            trigger = "claim released" if released else "pull request ended"
            identifier = f"{number}:{history[-1]['at']}:{trigger}"
            if record.get("handoff_prompt", {}).get("id") == identifier:
                continue
            recipients = ", ".join(waiting) or "project peers"
            record["handoff_prompt"] = {
                "id": identifier,
                "holder": holder,
                "waiting": waiting,
                "created": time.time(),
                "after_message_id": after_message_id,
                "trigger": trigger,
                "text": (
                    f"Issue #{number}: {trigger}. {holder}, send an explicit "
                    f"completion message to {recipients} "
                    "with the "
                    "commit, verification and remaining work. Ownership "
                    "does not move until an explicit handoff."
                ),
            }
            changed = True
        if changed:
            ledger["revision"] += 1
            write_json(directory / "issues.json", ledger)


def claimed_since(record: dict) -> float:
    """Reports when the current ownership generation of an issue began.

    A lane branch is reused across claims, so a pull request that ended before
    the current claim started describes earlier work and must not report that
    claim as finished. The generation starts at the most recent claim or
    accepted handoff; a record whose history no longer names one is treated as
    having always been owned, which preserves the previous observation.

    Args:
        record: Published ledger record for one issue.

    Returns:
        Unix time the current ownership generation began, or zero.
    """
    return max(
        (
            float(entry.get("at", 0) or 0)
            for entry in record.get("history", [])
            if entry.get("action") in {"claim", "accept"}
        ),
        default=0.0,
    )


def poll(home: Path, directory: Path) -> None:
    """Refreshes one project's observed presence and outstanding reminders."""
    manifest = roster.read(directory)
    config = configuration(home, manifest)
    observations = {
        name: presence(directory, name, config["inactive_after"])
        for name in manifest["participants"]
    }
    with store.connect(home, write=True, timeout=0) as db:
        for name, participant in manifest["participants"].items():
            observed = observations[name]
            db.execute(
                "INSERT INTO participant_presence(agent_id,state,process_alive,"
                "observed_ts,last_active) SELECT a.id,?,?,?,? FROM agents a "
                "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
                "AND a.name=? ON CONFLICT(agent_id) DO UPDATE SET "
                "state=excluded.state,process_alive=excluded.process_alive,"
                "observed_ts=excluded.observed_ts,last_active=excluded.last_active",
                (
                    observed["state"],
                    observed["process_alive"],
                    time.time(),
                    observed["last_active"],
                    manifest["root"],
                    participant["display"],
                ),
            )
    if config["prompts"]:
        closed: set[str] = set()
        ledger = issues.snapshot(directory)
        for name, participant in manifest["participants"].items():
            claimed = {
                number: claimed_since(record)
                for number, record in ledger["issues"].items()
                if record.get("owner") == name
            }
            if not claimed:
                continue
            completion = forge.branch_completion(
                Path(manifest["root"]), participant["branch"]
            )
            if completion is None or completion[0] not in {"MERGED", "CLOSED"}:
                continue
            closed.update(
                number
                for number, since in claimed.items()
                if completion[1] >= since
            )
        reminders(directory, manifest, closed)
        with contextlib.suppress(OSError):
            (directory / issues.SUPERVISION_ERROR).unlink(missing_ok=True)
        observe_responses(home, directory, manifest)
    if config["wake"]:
        for name, participant in manifest["participants"].items():
            if participant.get("wake", True):
                wake(
                    home, directory, manifest, name, observations[name], config
                )


def observe_responses(home: Path, directory: Path, manifest: dict) -> None:
    """Marks reminders responded to after explicit mail reaches every waiter.

    This observes delivery, not the semantic completeness of a handoff. It
    never acknowledges mail, releases an issue or transfers ownership.
    """
    with lock(directory / "issues.lock", timeout=1):
        ledger = issues.snapshot(directory)
        changed = False
        with store.connect(home) as db:
            for record in ledger["issues"].values():
                prompt = record.get("handoff_prompt")
                if not prompt or prompt.get("responded_at"):
                    continue
                holder = manifest["participants"].get(prompt["holder"])
                if not holder:
                    continue
                recipients = {
                    row["name"]
                    for row in db.execute(
                        "SELECT recipient.name FROM messages m "
                        "JOIN agents sender ON sender.id=m.sender_id "
                        "JOIN projects p ON p.id=m.project_id "
                        "JOIN message_recipients r ON r.message_id=m.id "
                        "JOIN agents recipient ON recipient.id=r.agent_id "
                        "WHERE p.human_key=? AND sender.name=? "
                        "AND m.id>?",
                        (
                            manifest["root"],
                            holder["display"],
                            prompt.get("after_message_id", 0),
                        ),
                    )
                }
                if recipients and all(
                    manifest["participants"].get(name, {}).get("display")
                    in recipients
                    for name in prompt["waiting"]
                ):
                    prompt["responded_at"] = time.time()
                    changed = True
        if changed:
            ledger["revision"] += 1
            write_json(directory / "issues.json", ledger)


def wake(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    observed: dict,
    config: dict,
) -> None:
    """Requests or resumes a native turn with bounded attempts per backlog.

    The backlog counts every reason this lane owes someone a turn: unread or
    unacknowledged mail, an unanswered completion reminder it holds, and a
    handoff offer naming it as recipient. An offer alone is enough, because a
    peer that offers an issue to an idle lane would otherwise wait for an
    unrelated trigger. An offer that was cancelled, declined or accepted is no
    longer recorded on its issue and so leaves the backlog, and a replacement
    offer carries a new identifier, which resets the bounded attempt count
    rather than extending the old one.

    The launcher still owns native authentication, trust and approval prompts.
    A resumed process uses a real terminal, not an unattended permission mode.
    Nothing reads, acknowledges, releases, accepts or transfers work for the
    lane; waking only asks the lane to take its own turn.
    """
    participant = manifest["participants"][name]
    path = directory / f"{name}-activity.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get("activity") not in {"idle", "stopped"}:
        return
    if (
        observed["process_alive"]
        and observed["age_seconds"] < config["inactive_after"]
    ):
        return
    with store.connect(home) as db:
        pending = db.execute(
            "SELECT m.id FROM messages m JOIN message_recipients r "
            "ON r.message_id=m.id JOIN agents a ON a.id=r.agent_id "
            "JOIN projects p ON p.id=a.project_id WHERE p.human_key=? "
            "AND a.name=? AND (r.read_ts IS NULL OR "
            "(m.ack_required=1 AND r.ack_ts IS NULL)) ORDER BY m.id",
            (manifest["root"], participant["display"]),
        ).fetchall()
    backlog = [str(row["id"]) for row in pending]
    ledger = issues.snapshot(directory)["issues"].values()
    backlog.extend(
        record["handoff_prompt"]["id"]
        for record in ledger
        if record.get("handoff_prompt", {}).get("holder") == name
        and not record["handoff_prompt"].get("responded_at")
    )
    backlog.extend(
        record["offer"]["id"]
        for record in ledger
        if (record.get("offer") or {}).get("to") == name
    )
    if not backlog:
        return
    wake_path = directory / f"{name}-wake.json"
    with lock(directory / f"{name}-wake.lock"):
        record = json.loads(wake_path.read_text()) if wake_path.exists() else {}
        attempts = (
            record.get("attempts", 0) if record.get("backlog") == backlog else 0
        )
        if (
            attempts >= 3
            or time.time() - record.get("at", 0) < config["inactive_after"]
        ):
            return
        result = "manual attention required"
        if observed["process_alive"]:
            result = terminal.request(directory, name)
        elif state.get("session_id") and state.get("launcher_managed"):
            entry = roster.provider(home, participant["provider"])
            if entry["adapter"] in roster.ADAPTERS and not entry.get(
                "require_env"
            ):
                with (directory / f"{name}-wake.log").open("ab") as output:
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "agent_parley.cli",
                            "--home",
                            str(home),
                            "run",
                            name,
                            "--repo",
                            manifest["root"],
                            "--resume",
                            "--task",
                            terminal.PROMPT,
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=output,
                        start_new_session=True,
                    )
                result = f"resume requested (launcher {child.pid})"
        write_json(
            wake_path,
            {
                "at": time.time(),
                "backlog": backlog,
                "attempts": attempts + 1,
                "result": result,
            },
        )
        from agent_parley import checkpoints

        checkpoints.record(
            directory,
            name,
            {"hook_event_name": "RuntimeWake"},
            checkpoints.Reason.WAKE_REQUESTED,
            None,
            result,
        )


def run(home: Path, stopped: threading.Event) -> None:
    """Runs bounded project polls until the local service stops.

    Per-project failures are isolated from coordination calls. Observations
    become visibly stale if polling fails; they never grant ownership.
    """
    deadlines: dict[Path, float] = {}
    while not stopped.is_set():
        for path in (home / "projects").glob("*/project.json"):
            if stopped.is_set():
                return
            if time.monotonic() < deadlines.get(path, 0):
                continue
            interval = DEFAULTS["interval"]
            with contextlib.suppress(
                OSError, ValueError, BridgeError, sqlite3.Error
            ):
                manifest = roster.read(path.parent)
                interval = configuration(home, manifest)["interval"]
                poll(home, path.parent)
            deadlines[path] = time.monotonic() + interval
        stopped.wait(1)
