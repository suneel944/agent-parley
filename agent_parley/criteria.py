"""Acceptance criteria of a claimed issue and the evidence given for each.

A claim reads the issue body once and keeps the list items of its
``Boundaries and acceptance criteria`` or ``Expected and actual behavior``
section, the two headings the repository's issue templates write. Nothing
else in the body is parsed. The list is stored on the issue's ledger record,
delivered to the lane as a numbered list inside the quoted issue text, since
it is issue-author text, with the evidence instruction outside the quote,
and matched against the evidence lines of the lane's ready report and of the
operator's ``issue resolve``. Evidence is one line per criterion, written
``N: <evidence>``, where the evidence is a command and its result, a test
name, or the reason the criterion does not apply. The runtime checks that
every criterion has a nonempty line; it cannot judge whether the line proves
it.
"""

from __future__ import annotations

import re

from agent_parley import protocol
from agent_parley.state import BridgeError

HEADINGS = (
    "boundaries and acceptance criteria",
    "expected and actual behavior",
)
MAX_CRITERIA = 30
MAX_TEXT = 300
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
ITEM = re.compile(r"^ ?(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(\S.*?)\s*$")
EVIDENCE = re.compile(r"^\s*(\d+)(?::\s*|[.)]\s+)(\S.*?)\s*$")
FORMAT = "N: <command and its result, test name, or why it does not apply>"


def parse(body: str) -> list[str]:
    """Lists the acceptance criteria an issue body's template section states.

    Each top-level bullet or numbered item under either template heading is
    one criterion, in order; the section ends at the next heading. Prose,
    nested items and continuation lines are not criteria. Lines inside a
    fenced code block (```` ``` ```` or ``~~~``) are neither headings nor
    criteria, so a shell comment such as ``# run tests`` in a fence leaves
    the section open. At most `MAX_CRITERIA` items are kept, each cut to
    `MAX_TEXT` characters.

    Args:
        body: Markdown body of one issue.

    Returns:
        The criteria in the order written, or an empty list when neither
        section exists or neither lists an item.
    """
    found: list[str] = []
    inside = False
    fence = ""
    for line in body.splitlines():
        if marker := FENCE.match(line):
            opened = marker.group(1)
            if not fence:
                fence = opened
            elif opened.startswith(fence) and not line.strip(f" {fence[0]}"):
                fence = ""
            continue
        if fence:
            continue
        if heading := HEADING.match(line):
            inside = heading.group(1).rstrip(":").lower() in HEADINGS
        elif inside and (item := ITEM.match(line)):
            found.append(item.group(1)[:MAX_TEXT])
    return found[:MAX_CRITERIA]


def matched(criteria: list[str], evidence: str) -> list[str]:
    """Maps evidence lines onto the criteria they prove, by number.

    A line ``N: text`` (or ``N. text``) gives the evidence for criterion N;
    the first line for a number wins and other lines are ignored, so the
    lines can sit inside a longer account.

    Args:
        criteria: Criteria recorded on the claim, numbered from one.
        evidence: Evidence text, one line per criterion.

    Returns:
        One evidence line per criterion, in criterion order, each cut to
        `MAX_TEXT` characters.

    Raises:
        BridgeError: If any criterion has no evidence line, naming each
            missing criterion by number and text.
    """
    lines: dict[int, str] = {}
    for line in evidence.splitlines():
        if found := EVIDENCE.match(line):
            lines.setdefault(int(found.group(1)), found.group(2)[:MAX_TEXT])
    missing = [
        f"{number}. {text}"
        for number, text in enumerate(criteria, 1)
        if number not in lines
    ]
    if missing:
        raise BridgeError(
            "No evidence line for acceptance criterion "
            + "; ".join(missing)
            + f". Give one --evidence line per criterion, written `{FORMAT}`."
        )
    return [lines[number] for number in range(1, len(criteria) + 1)]


def forge_outcome(evidence: dict) -> str:
    """Words a forge reading as the evidence line of every criterion.

    Args:
        evidence: Forge reading ``issue resolve`` or the supervisor ended the
            claim on, with its pull request, state and branch.

    Returns:
        One line naming the pull request, its state and where it came from.
    """
    pull = evidence.get("pull_request")
    return (
        "forge outcome: "
        + (f"pull request #{pull} " if pull else "pull request ")
        + str(evidence.get("state") or "").lower()
        + f" on {evidence.get('branch') or 'the forge'}"
        + (f" ({evidence['url']})" if evidence.get("url") else "")
    )


def dispatch_lines(criteria: list[str] | None, number: str) -> list[str]:
    """Builds the launcher's instruction about an issue's acceptance criteria.

    The line is launcher text and sits outside the quoted issue text; the
    criteria themselves are issue-author text and go inside the quote
    (`quoted_lines`).

    Args:
        criteria: Criteria recorded on the claim; empty when the issue
            states none, None when the forge could not read its body.
        number: Bare issue number.

    Returns:
        One line saying how the ready report proves the numbered criteria,
        or asking the lane to state its own and saying why.
    """
    own = (
        "list the criteria you will prove, with the evidence for each, in "
        "your ready report's --evidence."
    )
    if criteria is None:
        return [
            f"The forge issue body of #{number} could not be read, so its "
            "acceptance criteria are unknown; read the issue yourself and "
            + own
        ]
    if not criteria:
        return [f"Issue #{number} states no acceptance criteria; {own}"]
    return [
        "The quoted issue text below ends with the issue's acceptance "
        "criteria, numbered. The ready report "
        f"(`{protocol.cli_command()} report --state ready`) needs one "
        f"--evidence line per criterion, written `{FORMAT}`."
    ]


def quoted_lines(criteria: list[str] | None) -> list[str]:
    """Lists the acceptance criteria for the quoted part of a dispatch.

    Args:
        criteria: Criteria recorded on the claim, or None.

    Returns:
        A heading and the numbered criteria, or no lines without criteria.
    """
    if not criteria:
        return []
    return [
        "Acceptance criteria:",
        *(f"{index}. {text}" for index, text in enumerate(criteria, 1)),
    ]


def lines(record: dict) -> list[str]:
    """Renders an issue's criteria beside the evidence recorded for each.

    Args:
        record: Issue ledger record.

    Returns:
        A heading and one line per criterion with its evidence, or no lines
        when the claim recorded no criteria.
    """
    criteria = record.get("criteria") or []
    if not criteria:
        return []
    proven = (record.get("execution") or {}).get("criteria_evidence") or []
    rendered = ["Acceptance criteria:"]
    for index, text in enumerate(criteria, 1):
        given = proven[index - 1] if index <= len(proven) else ""
        rendered.append(
            f"  {index}. {text}\n     evidence: {given or 'none recorded'}"
        )
    return rendered
