"""Renders a live read-only operator view of coordination and enforcement."""

import contextlib
import curses
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_parley import (
    approvals,
    budgets,
    metrics,
    plan,
    process,
    records,
    roster,
    store,
    supervision,
    tables,
    views,
)
from agent_parley.checkpoints import (
    activity,
    branch_head,
    event_summary,
    lane_branch,
    mailbox,
    participant_liveness,
)
from agent_parley.issues import deadline_state, snapshot
from agent_parley.state import BridgeError

BRANCH_TTL = 5.0
MAX_PROMPT = 120
COLUMNS = (
    ("PARTICIPANT", 14),
    ("PROVIDER", 16),
    ("STATE", 22),
    ("EVENT", 6),
    ("BRANCH", 18),
    ("REVIEW", 8),
    ("ISSUES", 10),
    ("MAIL", 9),
    ("LEASES", 9),
    ("CONTEXT", 9),
    ("DENIALS", 9),
    ("CALLS", 9),
    ("TOKENS", 9),
    ("IDLE", 8),
    ("FIT", 8),
)
DROP_ORDER = (
    "PROVIDER",
    "EVENT",
    "BRANCH",
    "CONTEXT",
    "CALLS",
    "TOKENS",
    "LEASES",
    "FIT",
    "IDLE",
    "REVIEW",
    "ISSUES",
    "DENIALS",
)
SORT_KEYS: dict[str, Callable[[dict], Any]] = {
    "PARTICIPANT": lambda row: row["participant"],
    "PROVIDER": lambda row: row["provider"],
    "STATE": lambda row: row["state"],
    "EVENT": lambda row: -row["last_event_ts"],
    "BRANCH": lambda row: row["branch"],
    "REVIEW": lambda row: row["review"],
    "ISSUES": lambda row: -len(row["owned"]),
    "MAIL": lambda row: -_number(row["unread"]),
    "LEASES": lambda row: -row["leases"],
    "CONTEXT": lambda row: -row["injected_bytes"],
    "DENIALS": lambda row: -row["denials"],
    "CALLS": lambda row: -row["calls"],
    "TOKENS": lambda row: -(row["tokens"] or 0),
    "IDLE": lambda row: -row["idle_seconds"],
    "FIT": lambda row: 0 if row["fit"] is False else 1 if row["fit"] else 2,
}
KEYS = (
    ("j, down", "select the next lane, paging when it is past the fold"),
    ("k, up", "select the previous lane"),
    ("enter", "every recorded field of the selected lane, in full"),
    ("s", "order by the next column"),
    ("r", "reverse the order"),
    ("f", "narrow to participants, comma separated; empty clears"),
    ("o", "narrow to projects, comma separated; empty clears"),
    ("c", "show these columns, comma separated; empty shows all"),
    ("P", "every lane, claim and store problem, oldest first, in place"),
    ("?", "this key map and the column legend"),
    ("q", "leave; this view never writes state"),
)
LEGEND = (
    "A lane marked idle is alive, has served no coordination call within "
    "the configured interval, and holds unread or unacknowledged mail at "
    "least that old; the line under it names the oldest waiting item. The "
    "marker only reports: nothing is revoked and no ownership moves.",
    "An issue marked ! is past its recorded deadline or its attempt "
    "budget. It stays owned while its holder works; a holder that has run "
    "no tool past the inactivity window is woken, then the issue is offered "
    "to a peer, then released.",
    "An issue marked * is held by a lane whose session process is gone and "
    "which has been silent past the stall threshold; the line under it "
    "names those claims and the reservations that lane still holds. It is "
    "still owned until a peer runs issue claim --take-orphaned.",
    "A lane with a token, call or hour budget carries a line showing the "
    "share consumed; over budget marks a crossed limit with !. The budget "
    "informs and does not gate: nothing is stopped or refused, and a token "
    "budget counts what the client recorded, not spend.",
    "Columns: MAIL unread/pending acknowledgement; LEASES held leases, "
    "!past a declared time to live, +queued requests waiting on those "
    "keys, with the age of the oldest; DENIALS "
    "denied or blocked of retained hook events; CALLS served MCP calls, "
    "!rejected; TOKENS what that lane's own native client recorded for "
    "its session, not billed spend and not comparable between vendors, "
    "blank when its records were not readable; IDLE observed coordination "
    "inactivity inside the window, measured from this lane's own recorded "
    "turn ends, with + when the window reaches past what retention kept. "
    "IDLE says how long a lane went without coordination activity; it "
    "does not claim to know what the native client was doing inside a "
    "turn. A branch marked ! left "
    "its assigned bridge branch. A lease past a declared time to live is "
    "counted apart from the live ones and still held: it keeps blocking "
    "until its holder renews it at a checkpoint or releases it, and the "
    "runtime reclaims it for the first queued lane once no live session is "
    "observed for that holder or the expiry grace has run out.",
    "FIT is the last capacity check the runtime read for that lane, with "
    "+ when an advisory work offer is waiting for it; the line under an "
    "unfit lane names the check that failed, and no offer names that "
    "lane. A blank cell means nothing was published for it yet. An offer "
    "claims nothing and transfers nothing.",
    "REVIEW is the latest verdict a peer recorded against that lane's "
    "report; the reviewer and the report it judges are in the lane detail. "
    "A verdict is the reviewing lane's own claim about work it did not do: "
    "it is neither an operator approval nor independent verification, and it "
    "gates no integration.",
    "CONTEXT counts only the bytes coordination injected into a lane's "
    "context. A message, report or offer above its cap is kept whole as "
    "an attachment and its record carries a reference; the attachment's "
    "size is never counted here, only the reference that named it.",
    "A row carrying drift, a stale lease, a rejected call, an overdue or "
    "orphaned issue or a stopped session is drawn in colour where the "
    "terminal "
    "offers it and in bold where it does not. Every one of those also "
    "carries its own ! or word in the table, so a monochrome pipe reads "
    "exactly the same.",
)


def _number(value: object) -> int:
    """Reads a counted cell that may report that it could not be read."""
    try:
        return int(str(value))
    except ValueError:
        return -1


def _tokens(count: int | None) -> str:
    """Formats a reported token count, or nothing when none was readable.

    Args:
        count: Tokens the lane's own native client recorded for its session,
            or None when those records could not be read.

    Returns:
        A compact count, or an empty cell, which states that nothing was read
        rather than that the lane spent nothing.
    """
    if count is None:
        return ""
    if count < 1000:
        return str(count)
    if count < 1000000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1000000:.1f}M"


def _fitness(row: dict) -> str:
    """Formats a lane's fit result and whether a work offer is pending.

    Args:
        row: Assembled participant row.

    Returns:
        The fit result, marked when an advisory work offer is waiting for the
        lane. An empty cell states that nothing was published for this lane
        rather than that it is unfit.
    """
    value = "" if row["fit"] is None else "fit" if row["fit"] else "unfit"
    return value + ("+" if row["work_offer"] else "")


def _branch(lane: Path, cache: dict) -> str:
    """Reads a lane branch at most once per branch refresh interval.

    Args:
        lane: Assigned bridge worktree.
        cache: Caller-owned mapping of lane to its last reading.

    Returns:
        The lane's branch name or an unavailable marker.
    """
    key = str(lane)
    now = time.monotonic()
    cached = cache.get(key)
    if cached and now - cached[0] < BRANCH_TTL:
        return str(cached[1])
    value = lane_branch(lane)
    cache[key] = (now, value)
    return value


def _awaiting_approval(directory: Path, data: dict, agent: str) -> bool:
    """Reports whether a lane's ready report still awaits an operator.

    Args:
        directory: Private state directory for the common repository.
        data: Project manifest holding this participant.
        agent: Participant that owns the lane.

    Returns:
        True when the project requires an approval the lane does not have.
        A decision that cannot be read counts as awaiting, matching the
        refusal the integration commands would raise.
    """
    if not data["approval"]:
        return False
    head = branch_head(
        Path(data["root"]), data["participants"][agent]["branch"]
    )
    try:
        reviewed = approvals.review(directory, data, agent, head)
    except BridgeError:
        return True
    return reviewed["state"] == approvals.AWAITING


def _row(
    home: Path,
    directory: Path,
    data: dict,
    agent: str,
    context: dict,
) -> dict:
    """Assembles one participant row from every read-only source.

    Args:
        home: Private bridge state root.
        directory: Private state directory for the common repository.
        data: Project manifest holding this participant.
        agent: Participant that owns the lane.
        context: Shared per-project readings and the branch cache.

    Returns:
        One row of measured coordination and enforcement state.
    """
    participant = data["participants"][agent]
    state = activity(directory, agent)
    events = event_summary(directory, agent, context["since"])
    stats = context["usage"].get(participant["display"], {})
    issues = context["issues"]["issues"]
    owned = sorted(
        (
            number
            for number, record in issues.items()
            if record["owner"] == agent
        ),
        key=int,
    )
    overdue = [
        number
        for number in owned
        if deadline_state(issues[number])["overdue"]
        or deadline_state(issues[number])["budget_exceeded"]
    ]
    orphaned = [number for number in owned if issues[number].get("orphan")]
    orphan_keys = sorted(
        {
            key
            for number in orphaned
            for key in issues[number]["orphan"].get("reservations", [])
        }
    )
    offers = sum(
        1
        for record in issues.values()
        if record["offer"] and record["offer"]["to"] == agent
    )
    try:
        mail = mailbox(
            home, data["root"], participant["display"], state.get("cursor", 0)
        )
    except (BridgeError, OSError, sqlite3.Error):
        mail = {}
    branch = _branch(Path(participant["lane"]), context["branches"])
    liveness = participant_liveness(directory, agent, context["inactive_after"])
    stalled = supervision.stall(
        home, directory, data, agent, context["stalled_after"]
    )
    idle = metrics.idle_intervals(directory, agent, context["since"])
    published = supervision.published_work(directory, agent)
    edited = context["operator_edits"].get(agent, [])
    advanced = context["base_advances"].get(agent, [])
    budget = budgets.report(
        home, directory, data, agent, context["usage"], context["records"]
    )
    review = metrics.latest_review(directory, agent) or {}
    return {
        "participant": agent,
        "provider_name": participant["provider"],
        "alive": process.alive(
            state.get("session_pid"), state.get("session_ticks")
        ),
        "provider": (
            f"{participant['provider']}/"
            f"{participant['credential'] or 'default'}"
        ),
        "credential": participant["credential"],
        "state": tables.session(
            liveness,
            participant.get("paused", False),
            stalled["stalled"],
            stalled["age_seconds"],
            (
                time.time() - float(participant["retired"])
                if roster.retired(participant)
                else None
            ),
        ),
        "stalled": stalled["stalled"],
        "stall": supervision.stall_marker(stalled),
        "stall_age": stalled["age_seconds"] if stalled["stalled"] else 0,
        "operator_edits": edited,
        "operator_edit": supervision.operator_edit_marker(edited),
        "base_advance_paths": advanced,
        "base_advance": supervision.base_advance_marker(advanced),
        "event_age": (
            tables.age(time.time() - events["last_ts"])
            if events["last_ts"]
            else "-"
        ),
        "last_event_ts": events["last_ts"],
        "branch": branch,
        "drift": branch != participant["branch"],
        "review": str(review.get("verdict", "")),
        "reviewer": str(review.get("reviewer", "")),
        "reviewed_report": str(review.get("report_id", "")),
        "owned": owned,
        "issues_held": len(owned),
        "overdue": overdue,
        "orphaned": orphaned,
        "orphan": supervision.orphan_marker(orphaned, orphan_keys),
        "issues": ",".join(
            f"#{number}"
            + ("!" if number in overdue else "")
            + ("*" if number in orphaned else "")
            for number in owned
        )
        or "-",
        "offers": offers,
        "unread": mail.get("unread", "?"),
        "superseded": mail.get("superseded", "?"),
        "pending_ack": mail.get("pending_ack", "?"),
        "leases": stats.get("leases", 0),
        "stale_leases": stats.get("stale_leases", 0),
        "lease_age": stats.get("lease_age", 0),
        "stale_lease_age": stats.get("stale_lease_age", 0),
        "queued": stats.get("queued", 0),
        "queued_by": list(stats.get("queued_by", [])),
        "injected_bytes": events["injected_bytes"],
        "hook_events": events["events"],
        "denials": events["denials"],
        "denied_by": events.get("denied_by", []),
        "calls": stats.get("calls", 0),
        "errors": stats.get("errors", 0),
        "tokens": records.reported_tokens(
            home, participant, context["records"]
        ),
        "idle_seconds": idle["seconds"],
        "idle_complete": idle["complete"],
        "budget": budget,
        "over_budget": budget["over"],
        "budget_marker": budgets.marker(budget),
        "fit": published["fit"],
        "unfit": published["reason"],
        "work_offer": bool(published["offer"]),
        "offer_kind": (published["offer"] or {}).get("kind", ""),
        "work_dispatch": published.get("dispatch") or {},
        "awaiting_approval": _awaiting_approval(directory, data, agent),
        "prompt": str(
            state.get("last_prompt") or state.get("task", "")
        ).replace("\n", " ")[:MAX_PROMPT],
    }


def _reported_ready(directory: Path, data: dict) -> set[str]:
    """Names the participants whose latest report is the ready state."""
    return {
        name
        for name in data["participants"]
        if activity(directory, name).get("outcome") == "ready"
    }


def _totals(projects: list[dict]) -> dict:
    """Counts the reported rows so a header never counts a hidden one.

    Args:
        projects: Per-project row groups as held in a snapshot.

    Returns:
        Participant, event, denial, context and idle totals, how many lanes
        still await an operator approval, the number of plan groups whose
        every member is reported ready, and the lane holding the longest
        observed idle interval. A ready-group count
        belongs to its project rather than to a row, so narrowing the rows
        never changes it.
    """
    totals = {
        "participants": 0,
        "events": 0,
        "denials": 0,
        "context": 0,
        "idle": 0,
        "ready_groups": 0,
        "awaiting_approval": 0,
    }
    leader = ""
    longest = 0
    for project in projects:
        totals["ready_groups"] += len(project.get("ready_groups", []))
        for row in project["rows"]:
            totals["participants"] += 1
            totals["events"] += row["hook_events"]
            totals["denials"] += row["denials"]
            totals["context"] += row["injected_bytes"]
            totals["idle"] += row["idle_seconds"]
            totals["awaiting_approval"] += int(row["awaiting_approval"])
            if row["idle_seconds"] > longest:
                leader = row["participant"]
                longest = row["idle_seconds"]
    return {
        **totals,
        "idle_leader_seconds": longest,
        "idle_leader": leader,
    }


def collect(
    home: Path,
    running: bool,
    branches: dict,
    providers: tuple[str, ...] = (),
    window: float = 0.0,
    readings: dict | None = None,
    operator_edits: bool = True,
) -> dict:
    """Reads one snapshot of every registered project without changing state.

    Args:
        home: Private bridge state root.
        running: Whether the recorded coordination server process is alive.
        branches: Caller-owned branch cache, refreshed on its own interval.
        providers: Provider names to report; every provider when empty. A
            project keeps its heading once it holds a selected participant, so
            an operator can tell an emptied selection from an empty project.
        window: Seconds of enforcement history each event count covers; the
            whole retained log when zero. The window ends at the time of this
            reading, so a live view reports a period that moves with it.
        readings: Caller-owned cache of each lane's last session-record
            reading, so a live view folds only newly appended records instead
            of re-reading a whole transcript on every refresh.
        operator_edits: Whether to read the base checkout once per project
            for dirty paths that overlap a lane's reservation. Off for a
            repository whose base checkout is always dirty.

    Returns:
        Server health, per-project participant rows, the plan groups whose
        every member is reported ready, and totals over the reported rows, so
        a header never counts a participant the table does not show.
    """
    since = time.time() - window if window else 0.0
    cache = {} if readings is None else readings
    projects = []
    for path in sorted((home / "projects").glob("*/project.json")):
        try:
            data = roster.read(path.parent)
        except (BridgeError, OSError, ValueError):
            continue
        if supervision.root_retired(path.parent):
            continue
        try:
            usage = store.usage(home, data["root"])
        except sqlite3.Error:
            usage = {}
        supervised = supervision.configuration(home, data)
        edits, advances = supervision.readings(home, data)
        context = {
            "usage": usage,
            "issues": snapshot(path.parent),
            "branches": branches,
            "records": cache,
            "since": since,
            "stalled_after": supervised["stalled_after"],
            "inactive_after": supervised["inactive_after"],
            "operator_edits": edits if operator_edits else {},
            "base_advances": advances,
        }
        rows = [
            _row(home, path.parent, data, agent, context)
            for agent in sorted(data["participants"])
        ]
        if providers:
            rows = [row for row in rows if row["provider_name"] in providers]
        projects.append(
            {
                "root": data["root"],
                "rows": rows,
                "ready_groups": plan.ready_groups(
                    plan.groups(path.parent),
                    context["issues"],
                    _reported_ready(path.parent, data),
                ),
            }
        )
    return {
        "running": running,
        "home": str(home),
        "projects": projects,
        "totals": _totals(projects),
        "providers": list(providers),
        "window": window,
    }


def export(
    home: Path,
    running: bool,
    providers: tuple[str, ...] = (),
    window: float = 0.0,
    document: bool = False,
) -> str:
    """Reads one metrics frame of every registered project.

    The frame reads the records the live view reads, holds no lock and writes
    no state, so exporting on an interval never competes with coordination.

    Args:
        home: Private bridge state root.
        running: Whether the recorded coordination server process is alive.
        providers: Provider names to report; every provider when empty.
        window: Seconds of enforcement history each count covers; the whole
            retained log when zero.
        document: Whether to report one JSON document instead of the
            Prometheus text exposition format.

    Returns:
        The frame as text ending in a newline.
    """
    view = collect(home, running, {}, providers, window)
    if document:
        return views.render("metrics", views.measurements(view)) + "\n"
    return views.exposition(view)


def _cells(row: dict) -> tuple[str, ...]:
    """Formats one row as the text of every column, in column order.

    Args:
        row: Participant row produced by ``collect``.

    Returns:
        One string per entry of ``COLUMNS``, before any column is dropped
        or any value is clipped, so widths are measured on what the frame
        would show in full.
    """
    return (
        row["participant"],
        row["provider"],
        row["state"],
        row["event_age"],
        row["branch"] + ("!" if row["drift"] else ""),
        row["review"] or "-",
        row["issues"] + (f"+{row['offers']}" if row["offers"] else ""),
        f"{row['unread']}/{row['pending_ack']}",
        f"{row['leases']}"
        + (f"!{row['stale_leases']}" if row["stale_leases"] else "")
        + (f"+{row['queued']}" if row["queued"] else "")
        + (f" {tables.age(row['lease_age'])}" if row["leases"] else ""),
        tables.size(row["injected_bytes"]),
        f"{row['denials']}/{row['hook_events']}",
        f"{row['calls']}" + (f"!{row['errors']}" if row["errors"] else ""),
        _tokens(row["tokens"]),
        tables.age(row["idle_seconds"]) + ("" if row["idle_complete"] else "+"),
        _fitness(row),
    )


def alert(row: dict) -> bool:
    """Reports whether a row carries a warning worth emphasis.

    Args:
        row: Participant row produced by ``collect``.

    Returns:
        True when the lane drifted from its assigned branch, holds a stale
        lease, had a call rejected, owns an overdue or orphaned issue, or its
        recorded session process is gone. Each of those also prints its own
        textual marker, so colour adds emphasis and never carries meaning
        alone.
    """
    return bool(
        row["drift"]
        or row["stale_leases"]
        or row["errors"]
        or row["overdue"]
        or row.get("orphaned")
        or str(row["state"]).startswith("stopped")
    )


def _span(columns: list[tuple[int, str, int]]) -> int:
    """Measures the printed width of a set of columns and their gaps."""
    return sum(size + 2 for _, _, size in columns) - 2 if columns else 0


def _widths(
    view: dict, width: int | None, chosen: tuple[str, ...]
) -> tuple[list[tuple[int, str, int]], list[str]]:
    """Negotiates column widths against the frame and the terminal.

    Args:
        view: Snapshot produced by ``collect``.
        width: Available terminal columns, or None for unbounded output.
        chosen: Column names the operator asked for; all when empty.

    Returns:
        The columns to print as index, name and width, and the names that
        were dropped. Each width is the widest value in this frame, never
        below the column's declared minimum. When the set still exceeds
        the terminal, columns are dropped in ``DROP_ORDER`` rather than
        every cell being clipped; if the columns that order never drops
        still do not fit, they share the width that is left.
    """
    rows = [row for project in view["projects"] for row in project["rows"]]
    measured = [
        (
            index,
            name,
            max(
                [minimum, len(name)] + [len(_cells(row)[index]) for row in rows]
            ),
        )
        for index, (name, minimum) in enumerate(COLUMNS)
        if not chosen or name in chosen
    ]
    if not measured:
        measured = [
            (index, name, max(minimum, len(name)))
            for index, (name, minimum) in enumerate(COLUMNS)
        ]
    omitted: list[str] = []
    if width is None:
        return measured, omitted
    for name in DROP_ORDER:
        if _span(measured) <= width or len(measured) == 1:
            break
        if not any(entry[1] == name for entry in measured):
            continue
        omitted.append(name)
        measured = [entry for entry in measured if entry[1] != name]
    if _span(measured) > width:
        share = max(1, (width - 2 * (len(measured) - 1)) // len(measured))
        measured = [(index, name, share) for index, name, _ in measured]
    return measured, omitted


def select(
    view: dict,
    sort: str = "",
    reverse: bool = False,
    projects: tuple[str, ...] = (),
    participants: tuple[str, ...] = (),
) -> dict:
    """Orders and narrows a snapshot without reading any state again.

    Args:
        view: Snapshot produced by ``collect``.
        sort: Column name to order rows by; the collected order when empty.
            Counted columns order from the largest value down, so the first
            row is the one an operator is looking for.
        reverse: Whether to reverse the resulting order.
        projects: Repository roots to report; every project when empty. A
            root also matches on its trailing path segments, so a directory
            name selects it without its whole path.
        participants: Participant names to report; every one when empty.

    Returns:
        A snapshot holding the selected rows, totals recounted over them,
        and the selection itself, so a header never counts a row the table
        does not show.

    Raises:
        BridgeError: If the sort column is not a reported column.
    """
    if sort and sort.upper() not in SORT_KEYS:
        raise BridgeError(
            f"Unknown sort column {sort}. "
            f"Choose one of: {', '.join(SORT_KEYS)}."
        )
    key = SORT_KEYS.get(sort.upper())
    selected = []
    for project in view["projects"]:
        root = str(project["root"])
        if projects and not any(
            root == name or root.endswith("/" + name.rstrip("/"))
            for name in projects
        ):
            continue
        rows = [
            row
            for row in project["rows"]
            if not participants or row["participant"] in participants
        ]
        if key is not None:
            rows = sorted(rows, key=key)
        if reverse:
            rows = list(reversed(rows))
        selected.append({**project, "rows": rows})
    return {
        **view,
        "projects": selected,
        "totals": _totals(selected),
        "selection": {
            "sort": sort.upper(),
            "reverse": reverse,
            "projects": list(projects),
            "participants": list(participants),
        },
    }


def detail(row: dict) -> list[str]:
    """Reports every recorded field of one lane, none of them clipped.

    Args:
        row: Participant row produced by ``collect``.

    Returns:
        One line per field, so a value a column could not show in full can
        be read without leaving the view.
    """
    lines = [f"participant {row['participant']}", ""]
    for name in sorted(row):
        value = row[name]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "-"
        lines.append(f"{name}: {value}")
    return lines


def keymap() -> list[str]:
    """Reports the key map and the legend the live view keeps behind it."""
    lines = ["agent-parley top keys", ""]
    lines.extend(f"  {key.ljust(9)} {what}" for key, what in KEYS)
    for paragraph in LEGEND:
        lines.extend(["", paragraph])
    return lines


def _blocks(view: dict, columns: list[tuple[int, str, int]]) -> list[dict]:
    """Builds one indivisible block of lines per reported participant.

    Args:
        view: Snapshot produced by ``collect``.
        columns: Negotiated columns as index, name and width.

    Returns:
        Blocks in reported order, each naming the project it belongs to,
        so paging can never split a lane from its stall marker or its last
        prompt.
    """
    empty = (
        "  no participants for the selection"
        if view.get("selection")
        and (view["selection"]["projects"] or view["selection"]["participants"])
        else "  no participants for the selected provider"
        if view.get("providers")
        else "  no participants"
    )
    blocks = []
    for project in view["projects"]:
        if not project["rows"]:
            blocks.append(
                {"root": project["root"], "lines": [empty], "row": None}
            )
        for row in project["rows"]:
            cells = _cells(row)
            lines = [
                tables.GAP.join(
                    tables.fit(cells[index], size) for index, _, size in columns
                ).rstrip()
            ]
            if row["stall"]:
                lines.append(f"    {row['stall']}")
            if row["operator_edit"]:
                lines.append(f"    {row['operator_edit']}")
            if row.get("base_advance"):
                lines.append(f"    {row['base_advance']}")
            if row.get("orphan"):
                lines.append(f"    {row['orphan']}")
            if row.get("budget_marker"):
                lines.append(f"    {row['budget_marker']}")
            if row["unfit"]:
                lines.append(f"    {row['unfit']}")
            if row["work_offer"]:
                lines.append(f"    {row['offer_kind']} offer pending")
            if row["work_dispatch"].get("state") == "escalated":
                lines.append(f"    {row['work_dispatch']['last_result']}")
            if row["prompt"]:
                lines.append(f"    last: {row['prompt']}")
            blocks.append({"root": project["root"], "lines": lines, "row": row})
    return blocks


def _page(
    blocks: list[dict], start: int, available: int | None, header: str
) -> dict:
    """Emits the blocks that fit from a starting block, with their headings.

    Args:
        blocks: Blocks produced by ``_blocks``.
        start: Index of the first block to emit.
        available: Lines the table may occupy, or None for all of them.
        header: Column header line, repeated under each project heading.

    Returns:
        The emitted lines, the line of each emitted row block, the lines
        carrying a warning, and the block index the page stopped at.
    """
    lines: list[str] = []
    positions: dict[int, int] = {}
    alerts: list[int] = []
    root = ""
    stop = start
    for index in range(start, len(blocks)):
        block = blocks[index]
        opening = (
            []
            if block["root"] == root
            else ["", f"project {block['root']}", header.rstrip()]
        )
        needed = len(opening) + len(block["lines"])
        if available is not None and len(lines) + needed > available:
            break
        root = block["root"]
        lines.extend(opening)
        if block["row"] is not None:
            positions[index] = len(lines)
            if alert(block["row"]):
                alerts.append(len(lines))
        lines.extend(block["lines"])
        stop = index + 1
    return {
        "lines": lines,
        "positions": positions,
        "alerts": alerts,
        "stop": stop,
    }


def layout(
    view: dict,
    width: int | None = None,
    height: int | None = None,
    cursor: int = 0,
    columns: tuple[str, ...] = (),
) -> dict:
    """Fits a snapshot to a terminal and reports what the page shows.

    Args:
        view: Snapshot produced by ``collect``, optionally narrowed by
            ``select``.
        width: Available terminal columns, or None for unbounded output.
        height: Available lines, or None to print every row.
        cursor: Index of the selected row among the reported rows. The page
            moves to keep it visible, which is how the view pages.
        columns: Column names to show; all of them when empty.

    Returns:
        The lines to print, which rows they cover, the line holding the
        cursor, the lines carrying a warning, and a footer stating the
        visible range when the frame holds more rows than the page.
    """
    selected, omitted = _widths(view, width, columns)
    header = "  ".join(name.ljust(size) for _, name, size in selected)
    totals = view["totals"]
    rate = (
        f"{100 * totals['denials'] / totals['events']:.0f}%"
        if totals["events"]
        else "0%"
    )
    lines = [
        f"agent-parley top  server: "
        f"{'running' if view['running'] else 'not running'}  "
        f"state: {view['home']}",
        f"projects {len(view['projects'])}  "
        f"participants {totals['participants']}  "
        f"hook events {totals['events']}  "
        f"denials {totals['denials']} ({rate})  "
        f"context {tables.size(totals['context'])}  "
        f"ready groups {totals.get('ready_groups', 0)}  "
        f"idle {tables.age(totals['idle'])}"
        + (
            f" (most {totals['idle_leader']} "
            f"{tables.age(totals['idle_leader_seconds'])})"
            if totals["idle_leader"]
            else ""
        )
        + (
            f"  awaiting approval {totals['awaiting_approval']}"
            if totals.get("awaiting_approval")
            else ""
        )
        + (
            f"  provider {','.join(view['providers'])}"
            if view.get("providers")
            else ""
        )
        + (
            f"  last {tables.age(view['window'])}"
            if view.get("window")
            else "  all retained"
        ),
    ]
    if omitted:
        lines.append("Hidden columns: " + ", ".join(omitted))
    choice = view.get("selection") or {}
    stated = "  ".join(
        part
        for part in (
            f"sort {choice['sort']}" if choice.get("sort") else "",
            "reversed" if choice.get("reverse") else "",
            f"project {','.join(choice['projects'])}"
            if choice.get("projects")
            else "",
            f"participant {','.join(choice['participants'])}"
            if choice.get("participants")
            else "",
            f"columns {','.join(columns)}" if columns else "",
        )
        if part
    )
    if stated:
        lines.append("Selection: " + stated)
    blocks = _blocks(view, selected)
    order = [
        index for index, block in enumerate(blocks) if block["row"] is not None
    ]
    total = len(order)
    cursor = min(max(cursor, 0), max(0, total - 1))
    available = None if height is None else max(1, height - len(lines))
    start, page = _follow(blocks, order, cursor, available, header)
    footer = ""
    if available is not None and len(page["positions"]) < total:
        available = max(1, available - 1)
        start, page = _follow(blocks, order, cursor, available, header)
        before = sum(
            1 for index in range(start) if blocks[index]["row"] is not None
        )
        shown = len(page["positions"])
        footer = (
            f"rows {before + 1}-{before + shown} of {total}"
            if shown
            else f"no room for rows; {total} of {total} hidden"
        )
    offset = len(lines)
    lines.extend(page["lines"])
    if height is None:
        for paragraph in LEGEND:
            lines.extend(["", paragraph])
    if footer:
        lines.append(footer)
    if width is not None:
        lines = [tables.fit(line, max(1, width)).rstrip() for line in lines]
    if height is not None:
        lines = lines[: max(0, height)]
    return {
        "lines": lines,
        "alerts": [offset + line for line in page["alerts"]],
        "cursor": (
            offset + page["positions"][order[cursor]]
            if order and order[cursor] in page["positions"]
            else -1
        ),
        "first": sum(
            1 for index in range(start) if blocks[index]["row"] is not None
        ),
        "shown": len(page["positions"]),
        "total": total,
        "omitted": omitted,
        "footer": footer,
    }


def _follow(
    blocks: list[dict],
    order: list[int],
    cursor: int,
    available: int | None,
    header: str,
) -> tuple[int, dict]:
    """Chooses the page that keeps the selected row on screen.

    Args:
        blocks: Blocks produced by ``_blocks``.
        order: Block index of each reported row, in reported order.
        cursor: Index of the selected row among the reported rows.
        available: Lines the table may occupy, or None for all of them.
        header: Column header line.

    Returns:
        The first block of the page and the page itself. The page starts
        at the top and advances only as far as the selection requires, so
        the operator scrolls rather than jumping.
    """
    start = 0
    page = _page(blocks, start, available, header)
    if order and available is not None:
        target = order[cursor]
        while start <= target and target >= page["stop"]:
            start += 1
            page = _page(blocks, start, available, header)
    return start, page


def render(
    view: dict,
    width: int | None = None,
    height: int | None = None,
    cursor: int = 0,
    columns: tuple[str, ...] = (),
) -> list[str]:
    """Formats a snapshot as plain lines that survive being piped to a file.

    Args:
        view: Snapshot produced by ``collect``.
        width: Available terminal columns, or None for unbounded output, so
            a captured file keeps every column intact.
        height: Available lines, or None to print every row.
        cursor: Index of the selected row among the reported rows.
        columns: Column names to show; all of them when empty.

    Returns:
        Header, one block per participant, and the legend when the output
        is unbounded.
    """
    return list(layout(view, width, height, cursor, columns)["lines"])


def _warning() -> int:
    """Chooses how a warning row is emphasized on this terminal."""
    with contextlib.suppress(curses.error):
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
            return curses.color_pair(1)
    return curses.A_BOLD


def _draw(screen: "curses.window", frame: dict, emphasis: int) -> list[str]:
    """Writes one frame, reporting every line the terminal refused.

    Args:
        screen: Curses window being drawn.
        frame: Layout produced by ``layout``.
        emphasis: Attribute a warning row is drawn with.

    Returns:
        The reason for each write the terminal rejected, so a frame that
        cannot be drawn says why instead of appearing half finished.
    """
    height, width = screen.getmaxyx()
    screen.erase()
    refused = []
    for index, line in enumerate(frame["lines"][: max(0, height - 1)]):
        attribute = emphasis if index in frame["alerts"] else curses.A_NORMAL
        if index == frame["cursor"]:
            attribute |= curses.A_REVERSE
        try:
            screen.addnstr(index, 0, line, max(1, width - 1), attribute)
        except curses.error as failure:
            refused.append(str(failure) or "the terminal refused the write")
    return refused


def _overlay(
    screen: "curses.window", lines: list[str], interval: float
) -> None:
    """Shows text in place of the table until the operator presses a key."""
    height, width = screen.getmaxyx()
    screen.erase()
    for index, line in enumerate(lines[: max(0, height - 1)]):
        with contextlib.suppress(curses.error):
            screen.addnstr(index, 0, line, max(1, width - 1))
    with contextlib.suppress(curses.error):
        screen.addnstr(
            max(0, height - 1), 0, "any key returns", max(1, width - 1)
        )
    screen.refresh()
    screen.timeout(-1)
    screen.getch()
    screen.timeout(max(100, int(interval * 1000)))
    screen.clear()


def _ask(
    screen: "curses.window", label: str, interval: float
) -> tuple[str, ...]:
    """Reads a comma-separated narrowing without leaving the view.

    Args:
        screen: Curses window being drawn.
        label: Prompt shown on the footer line.
        interval: Seconds between redraws, restored before returning.

    Returns:
        The entered names, or nothing when the operator cleared the field.
    """
    height, width = screen.getmaxyx()
    with contextlib.suppress(curses.error):
        screen.addnstr(
            max(0, height - 1),
            0,
            label.ljust(max(1, width - 1)),
            max(1, width - 1),
        )
    screen.timeout(-1)
    curses.echo()
    try:
        entered = screen.getstr(
            max(0, height - 1), min(len(label), max(0, width - 2)), 64
        )
    except curses.error:
        entered = b""
    finally:
        curses.noecho()
        screen.timeout(max(100, int(interval * 1000)))
        screen.clear()
    names = entered.decode(errors="replace").replace(",", " ").split()
    return tuple(names)


def _loop(
    screen: "curses.window",
    home: Path,
    running: Callable[[], bool],
    interval: float,
    providers: tuple[str, ...],
    window: float,
    choices: dict,
    operator_edits: bool = True,
    problems: Callable[[], list[str]] | None = None,
) -> None:
    """Redraws the snapshot until the operator quits; never writes state."""
    branches: dict = {}
    readings: dict = {}
    shaping = dict(choices)
    cursor = 0
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    emphasis = _warning()
    screen.keypad(True)
    screen.timeout(max(100, int(interval * 1000)))
    while True:
        height, width = screen.getmaxyx()
        view = select(
            collect(
                home,
                running(),
                branches,
                providers,
                window,
                readings,
                operator_edits,
            ),
            shaping["sort"],
            shaping["reverse"],
            shaping["projects"],
            shaping["participants"],
        )
        frame = layout(
            view,
            max(1, width - 1),
            max(0, height - 1),
            cursor,
            shaping["columns"],
        )
        refused = _draw(screen, frame, emphasis)
        status = frame["footer"] or f"rows {frame['total']}"
        if refused:
            status = f"{status}  {len(refused)} writes refused: {refused[0]}"
        with contextlib.suppress(curses.error):
            screen.addnstr(
                max(0, height - 1),
                0,
                f"{status}  ? keys  q leaves; this view never writes state",
                max(1, width - 1),
            )
        screen.refresh()
        key = screen.getch()
        rows = [row for project in view["projects"] for row in project["rows"]]
        if key in (ord("q"), ord("Q"), 27):
            return
        if key == curses.KEY_RESIZE:
            screen.clear()
        elif key in (ord("j"), curses.KEY_DOWN):
            cursor = min(cursor + 1, max(0, frame["total"] - 1))
        elif key in (ord("k"), curses.KEY_UP):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_ENTER, 10, 13) and rows:
            _overlay(screen, detail(rows[min(cursor, len(rows) - 1)]), interval)
        elif key == ord("?"):
            _overlay(screen, keymap(), interval)
        elif key == ord("P") and problems is not None:
            _overlay(
                screen, ["agent-parley problems", "", *problems()], interval
            )
        elif key == ord("s"):
            names = list(SORT_KEYS)
            shaping["sort"] = names[
                (names.index(shaping["sort"]) + 1) % len(names)
                if shaping["sort"] in names
                else 0
            ]
        elif key == ord("r"):
            shaping["reverse"] = not shaping["reverse"]
        elif key == ord("f"):
            shaping["participants"] = _ask(screen, "participants: ", interval)
        elif key == ord("o"):
            shaping["projects"] = _ask(screen, "projects: ", interval)
        elif key == ord("c"):
            shaping["columns"] = tuple(
                name.upper() for name in _ask(screen, "columns: ", interval)
            )


def run(
    home: Path,
    running: Callable[[], bool],
    once: bool = False,
    interval: float = 1.0,
    providers: tuple[str, ...] = (),
    window: float = 0.0,
    sort: str = "",
    reverse: bool = False,
    projects: tuple[str, ...] = (),
    participants: tuple[str, ...] = (),
    columns: tuple[str, ...] = (),
    operator_edits: bool = True,
    problems: Callable[[], list[str]] | None = None,
) -> None:
    """Shows the dashboard, printing a plain snapshot when it cannot draw.

    Args:
        home: Private bridge state root.
        running: Reports whether the recorded server process is alive.
        once: Print one snapshot instead of drawing a live view.
        interval: Seconds between redraws of the live view.
        providers: Provider names to report; every provider when empty.
        window: Seconds of enforcement history each event count covers; the
            whole retained log when zero.
        sort: Column name to order rows by; the collected order when empty.
        reverse: Whether to reverse that order.
        projects: Repository roots to report; every project when empty.
        participants: Participant names to report; every one when empty.
        columns: Column names to show; all of them when empty.
        operator_edits: Whether each frame reads the base checkout for
            dirty paths that overlap a lane's reservation.
        problems: Produces the problem lines the ``P`` key shows in place
            of the table; the key does nothing when None.

    A snapshot is printed at the width of the terminal when one is
    attached and at the full width of the table when the output is a pipe,
    so a captured file holds every column intact.
    """
    ordering = sort.upper()
    shown = tuple(name.upper() for name in columns)
    shaping: dict = {
        "sort": ordering,
        "reverse": reverse,
        "projects": projects,
        "participants": participants,
        "columns": shown,
    }
    if once or not sys.stdout.isatty():
        view = select(
            collect(
                home,
                running(),
                {},
                providers,
                window,
                operator_edits=operator_edits,
            ),
            ordering,
            reverse,
            projects,
            participants,
        )
        available = (
            shutil.get_terminal_size().columns if sys.stdout.isatty() else None
        )
        for line in render(view, available, columns=shown):
            print(line)
        return
    curses.wrapper(
        _loop,
        home,
        running,
        interval,
        providers,
        window,
        shaping,
        operator_edits,
        problems,
    )
