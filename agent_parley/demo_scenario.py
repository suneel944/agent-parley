"""The coordination story the demo tells, one function per chapter.

Two callers drive these chapters through the same stub-lane harness in
`agent_parley.demo`: the ``agent-parley demo`` command runs the short cut
`tour`, and ``scripts/record_demo.py`` (``make demo-stub``) runs the full
nine-chapter `record` and renders it as the published recording. Because both
read the steps and captions from here, the command and the recording cannot
drift apart.

Every step runs a real ``agent-parley`` command, a real MCP tool dispatch or
the real native hook; only the model session is a stand-in.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_parley.demo import Recorder

ISSUE = "41"
PATTERN = "src/payments/**"
OFFER_ID = re.compile(r'"id": "([0-9a-f]{16,})"')
PROPOSAL_ID = re.compile(r"^([0-9a-f]{16}) pending", re.MULTILINE)
WORK_ORDER = """[plan]
name = "Refund rework"

[dependencies]
"42" = ["41"]

[groups]
refunds = ["41", "42"]
"""
REFUND_FIX = '''"""Refund path the lanes negotiate over."""


def refund(cents: int) -> int:
    """Returns the ledger entry for a refund of whole cents."""
    return -abs(cents)
'''


def opening(recorder: Recorder) -> None:
    """Adds the recording's title card.

    Args:
        recorder: Harness collecting the steps.
    """
    recorder.chapter(
        "AGENT PARLEY",
        "Many coding agents. One repository.",
        "Claims, advisory reservations, handoffs, plans, budgets",
        "and unattended integration, for claude and codex side by side.",
        "Every frame below is real command output.",
        seconds=6.0,
    )


def lanes(recorder: Recorder) -> None:
    """Starts the server, registers the repository and launches two lanes.

    Each participant is launched before the next is added: a lane added but
    never launched would otherwise sit beside a launched one while the first
    supervision poll runs, which is not the order an operator uses.

    Args:
        recorder: Harness collecting the steps.
    """
    root = str(recorder.repository)
    recorder.chapter(
        "CHAPTER 1",
        "Parallel lanes",
        "One coordination server. Each agent gets its own worktree,",
        "identity and inbox; the base checkout is never edited.",
    )
    recorder.run("up")
    recorder.caption("One loopback coordination server serves every project.")
    recorder.run("setup", root)
    recorder.caption("Register the repository once.", limit=10)
    recorder.run("forge", "set", "null", "--repo", root)
    recorder.caption("No forge for this demo: everything stays local.")
    recorder.run("participant", "add", "ada", "--provider", "claude")
    recorder.launch("ada")
    recorder.caption("ada runs on claude, in a worktree of its own.")
    recorder.run("participant", "add", "grace", "--provider", "codex")
    recorder.launch("grace")
    recorder.caption("grace runs on codex, right beside it.")
    recorder.session("ada")
    recorder.session("grace")
    recorder.run("status")
    recorder.caption("Two providers, two lanes, one shared view.")


def work_order(recorder: Recorder) -> None:
    """Applies and shows the reviewed work order.

    Args:
        recorder: Harness collecting the steps.
    """
    order = str(recorder.repository.parent / "work-order.toml")
    recorder.chapter(
        "CHAPTER 2",
        "A work order everyone reads",
        "Dependencies live in one reviewed TOML file,",
        "not in anyone's head.",
    )
    recorder.run("plan", "apply", order)
    recorder.caption("#42 waits on #41; both belong to the refunds group.")
    recorder.run("plan", "show")
    recorder.caption("The plan as a tree, with each issue's current owner.")


def claims(recorder: Recorder) -> None:
    """Claims an issue and meets a reservation collision.

    Args:
        recorder: Harness collecting the steps.
    """
    recorder.chapter(
        "CHAPTER 3",
        "Claims and advisory reservations",
        "An agent claims an issue and reserves what it will touch.",
        "A second agent is told, not silently overwritten.",
    )
    recorder.run("issue", "claim", ISSUE, cwd=recorder.lanes["ada"])
    recorder.caption("ada claims #41 from inside its lane.", limit=12)
    recorder.tool("ada", "file_reservation_paths", {"paths": [PATTERN]})
    recorder.caption("ada reserves the refund module.", limit=16)
    recorder.tool("grace", "file_reservation_paths", {"paths": [PATTERN]})
    recorder.caption("grace asks for the same paths: a named collision.")
    recorder.tool("grace", "request_reservation", {"paths": [PATTERN]})
    recorder.caption("So grace queues behind ada instead of waiting blind.")


def guardrails(recorder: Recorder) -> None:
    """Shows the native hook refusing a branch switch inside a lane.

    Args:
        recorder: Harness collecting the steps.

    Raises:
        RuntimeError: If the hook did not deny the branch switch.
    """
    recorder.chapter(
        "CHAPTER 4",
        "Guardrails in the native hooks",
        "Lanes stay on their assigned branch.",
        "The refusal says why, and what to do instead.",
    )
    recorder.hook("ada", "git checkout -b hotfix/refund")
    decision = json.loads("".join(recorder.latest().output) or "{}")
    verdict = decision.get("hookSpecificOutput", {})
    if verdict.get("permissionDecision") != "deny":
        raise RuntimeError(f"the branch switch was not refused: {decision}")
    recorder.caption("A branch switch inside a lane is refused before it runs.")


def handoffs(recorder: Recorder) -> None:
    """Offers the claimed issue to the second lane, which accepts it.

    Args:
        recorder: Harness collecting the steps.

    Raises:
        RuntimeError: If the offer carried no identifier.
    """
    ada = recorder.lanes["ada"]
    grace = recorder.lanes["grace"]
    recorder.chapter(
        "CHAPTER 5",
        "Handoffs",
        "Work moves between agents explicitly,",
        "with a summary, and only when the recipient accepts.",
    )
    offered = recorder.run(
        "issue",
        "offer",
        ISSUE,
        "--to",
        "grace",
        "--summary",
        "Rounding traced to refund(); fix and tests remain.",
        cwd=ada,
    )
    recorder.caption("ada offers #41 to grace with a summary.", limit=12)
    found = OFFER_ID.search(offered)
    if found is None:
        raise RuntimeError(f"no offer identifier in {offered!r}")
    recorder.run(
        "issue", "accept", ISSUE, "--offer-id", found.group(1), cwd=grace
    )
    recorder.caption("grace accepts; only then does ownership move.", limit=12)
    recorder.tool("ada", "release_file_reservations", {})
    recorder.caption(
        "ada releases all of its reservations; grace's queued request is "
        "granted."
    )
    recorder.run("issue", "list", cwd=grace)
    recorder.caption("Who holds what, at a glance.")


def revisions(recorder: Recorder) -> None:
    """Proposes and approves a change to the work order.

    Args:
        recorder: Harness collecting the steps.

    Raises:
        RuntimeError: If the proposal carried no identifier.
    """
    recorder.chapter(
        "CHAPTER 6",
        "Plan revisions",
        "A lane can propose a change to the work order.",
        "The operator approves it; the ledger never drifts silently.",
    )
    proposed = recorder.run(
        "plan",
        "propose",
        "--base",
        "1",
        "--add",
        "42:43",
        "--reason",
        "#42 reuses the ledger #43 introduces",
        cwd=recorder.lanes["ada"],
    )
    recorder.caption("ada proposes a new dependency from its lane.")
    found = PROPOSAL_ID.search(proposed)
    if found is None:
        raise RuntimeError(f"no proposal identifier in {proposed!r}")
    recorder.run("plan", "proposals")
    recorder.caption("Proposals outside the [revisions] envelope wait here.")
    recorder.run("plan", "approve", found.group(1))
    recorder.caption("The operator approves it.")
    recorder.run("plan", "show")
    recorder.caption("The plan now carries the new edge.")


def budget(recorder: Recorder) -> None:
    """Enforces a run budget for the project.

    Args:
        recorder: Harness collecting the steps.
    """
    recorder.chapter(
        "CHAPTER 7",
        "A run budget",
        "Cap the tokens, calls and hours a whole run may spend.",
        "Past the cap the service starts no wake, dispatch or launch.",
    )
    recorder.run("budget", "enforce", "--calls", "500", "--hours", "8")
    recorder.caption("Enforced for the project, observed by Parley itself.")


def integration(recorder: Recorder) -> None:
    """Integrates the second lane's fix without an operator.

    Args:
        recorder: Harness collecting the steps.
    """
    grace = recorder.lanes["grace"]
    recorder.chapter(
        "CHAPTER 8",
        "Unattended integration",
        "Named issues may merge without an operator,",
        "only after the recorded gate passes on a ready report.",
    )
    recorder.run("verify", "set", "git diff --check")
    recorder.caption("The gate every merge runs in the base checkout.")
    recorder.run("unattended", "set", ISSUE, "--target", "main")
    recorder.caption("#41 may be integrated into main unattended.")
    (grace / "src" / "payments" / "refund.py").write_text(REFUND_FIX)
    recorder.shell("git", "commit", "-am", "Refund whole cents only", cwd=grace)
    recorder.caption("grace commits the fix in its lane.")
    recorder.run(
        "report",
        "--issue",
        ISSUE,
        "--state",
        "ready",
        "--summary",
        "Refunds clamp to whole cents",
        "--evidence",
        "git diff --check: clean",
        cwd=grace,
    )
    recorder.caption("She reports ready, with evidence.")
    recorder.stop("grace")
    recorder.run("unattended", "run", "grace")
    recorder.caption("Her session has exited; the merge runs behind the gate.")
    recorder.shell("git", "log", "--oneline", "-3", cwd=recorder.repository)
    recorder.caption("main now carries grace's commit.")
    recorder.run("history", "participant", "grace")
    recorder.caption("Every claim, handoff, report and merge is on record.")


def dashboard(recorder: Recorder) -> None:
    """Shows the operator dashboard and the problem queue.

    Args:
        recorder: Harness collecting the steps.
    """
    recorder.chapter(
        "CHAPTER 9",
        "One pane for the operator",
        "Live state of every lane, and only the problems",
        "that need a human.",
    )
    recorder.run("top", "--once")
    recorder.caption(
        "The dashboard: state, issues, mail, leases, denials.", shown=True
    )
    recorder.run("problems")
    recorder.caption("The operator's queue of things to decide.")


def closing(recorder: Recorder) -> None:
    """Adds the closing card naming the install command.

    Args:
        recorder: Harness collecting the steps.
    """
    recorder.chapter(
        "GET STARTED",
        "uv tool install agent-parley",
        "agent-parley setup .   ·   agent-parley run <lane>",
        "Local-first. Standard library only. Your native CLIs, unchanged.",
        seconds=6.0,
    )


def record(recorder: Recorder) -> None:
    """Drives every chapter the published recording shows.

    Args:
        recorder: Harness collecting the steps.
    """
    opening(recorder)
    lanes(recorder)
    work_order(recorder)
    claims(recorder)
    guardrails(recorder)
    handoffs(recorder)
    revisions(recorder)
    budget(recorder)
    integration(recorder)
    dashboard(recorder)
    closing(recorder)


def tour(recorder: Recorder) -> None:
    """Drives the short cut ``agent-parley demo`` shows.

    Parallel lanes, a claim and a reservation collision, a hook refusal, a
    handoff and the dashboard, in the same steps and captions the recording
    uses.

    Args:
        recorder: Harness collecting the steps.
    """
    lanes(recorder)
    claims(recorder)
    guardrails(recorder)
    handoffs(recorder)
    dashboard(recorder)
