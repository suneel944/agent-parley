"""Lays out coordination rows as one plain table for every operator view.

`status` reads once and `top` redraws, but both report the same lanes, so
both take their column names, width rule, cell formats and markers from here.
A table that disagreed with itself between the two commands would make an
operator compare two shapes of the same state.

The available width is supplied by the caller and never read from the
environment: a live view passes its drawable area, a single read passes the
terminal it owns, and a redirected stream passes nothing and receives the
whole table, so a pipe or a file keeps every column.
"""

from __future__ import annotations

import unicodedata

GAP = "  "
ZERO_WIDTH = "".join(
    map(chr, (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, *range(0xFE00, 0xFE10)))
)
MINIMUM_TASK = 12
STATUS_COLUMNS = (
    "PARTICIPANT",
    "PROVIDER",
    "ACCOUNT",
    "SESSION",
    "BRANCH",
    "OUTCOME",
    "REVIEW",
    "ISSUES",
    "MAIL",
    "LEASES",
    "REPORTED",
    "TASK",
)
STATUS_DROP = (2, 10, 9, 1, 8, 6, 3, 5, 4, 7)
WORK_COLUMNS = ("ISSUE", "TITLE", "OWNER", "STATE", "LAST EVENT", "PR")
WORK_DROP = (4, 5, 1)
TITLE_WIDTH = 40
LANE_COLUMNS = ("LANE", "STATE", "CLAIMS", "TASK")


def age(seconds: float) -> str:
    """Formats an age compactly, without ever implying sub-second precision."""
    if seconds < 0:
        return "-"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"


def size(count: int) -> str:
    """Formats a byte count in units an operator can compare at a glance."""
    if count < 1024:
        return f"{count}B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f}kB"
    return f"{count / 1024 / 1024:.1f}MB"


def _char_columns(char: str) -> int:
    """Counts the terminal columns one code point occupies.

    East Asian Wide and Fullwidth characters take two columns. Combining
    marks, zero-width spaces and joiners, and variation selectors take none.
    Every other character, including East Asian Ambiguous ones such as the
    ellipsis, takes one, which matches a terminal in a non-CJK locale.
    """
    if char in ZERO_WIDTH or unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def measure(value: str) -> int:
    """Measures text in terminal display columns rather than code points.

    Each code point is counted on its own, so an emoji joined by zero-width
    joiners into one glyph, such as a family sequence, counts the width of
    every emoji it joins. A terminal that draws the sequence as one glyph
    shows it narrower than measured, so a cell may carry spare padding but
    never overruns its column budget.

    Args:
        value: Text as it will be printed.

    Returns:
        The number of terminal columns the text occupies.
    """
    return sum(_char_columns(char) for char in value)


def fit(value: str, width: int) -> str:
    """Pads a cell, marking any value the column could not show in full.

    Width is counted in terminal display columns, as `measure` defines them,
    so wide characters and combining marks line up with the columns around
    them.

    Args:
        value: Cell text.
        width: Column width in terminal display columns.

    Returns:
        Text padded to the column width, ending in an ellipsis when clipped,
        so a truncated branch or issue list never reads as complete. A
        clipped cell keeps whole characters with their combining marks and,
        when a wide character would cross the budget, pads with a space
        instead of splitting it.
    """
    used = measure(value)
    if used <= width:
        return value + " " * (width - used)
    budget = width - 1
    kept: list[str] = []
    used = 0
    for char in value:
        cost = _char_columns(char)
        if used + cost > budget:
            break
        kept.append(char)
        used += cost
    text = "".join(kept).rstrip(ZERO_WIDTH)
    return text + "…" + " " * (budget - used)


def session(
    liveness: str,
    paused: bool,
    stalled: bool,
    stall_age: float,
    retired_age: float | None = None,
) -> str:
    """Describes a lane's session the same way in every operator view.

    Args:
        liveness: Reading taken from the lane's own checkpoint records.
        paused: Whether the participant is paused.
        stalled: Whether the lane is alive and has served no coordination
            call inside the configured interval.
        stall_age: Seconds the lane has been in that state.
        retired_age: Seconds since the participant retired, or None when it is
            still serving. The absolute time it retired at is reported by the
            machine-readable views, which are not width-bound.

    Returns:
        One cell naming the state the lane is in. A retired lane reports that
        first, with how long ago it retired, because nothing else the cell
        could say about it is actionable. A paused lane reports its pause
        next, an idle lane reports how long it has been idle, and a lane whose
        checkpoints are unreadable says so rather than claiming enforcement it
        cannot observe.
    """
    if retired_age is not None:
        return f"retired {age(retired_age)} ago"
    if paused:
        return f"paused; {liveness}"
    if stalled:
        return f"idle {age(stall_age)}; {liveness}"
    if "checkpoints unavailable" in liveness:
        return "running; no hooks"
    return liveness


def idle_age(stalled: dict) -> float:
    """Reads the idle age a stalled lane's session cell reports.

    Args:
        stalled: Stall report from `supervision.stall`.

    Returns:
        Seconds the lane has shown no sign of work, or the waiting item's age
        when the lane has recorded no evidence of work at all.
    """
    silent = stalled.get("silent_seconds")
    return stalled["age_seconds"] if silent is None else silent


def widths(columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[int]:
    """Measures each column's display width against its heading and values."""
    return [
        max(measure(name), *(measure(row[index]) for row in rows), 1)
        if rows
        else measure(name)
        for index, name in enumerate(columns)
    ]


def span(selected: list[tuple[int, tuple[str, int]]]) -> int:
    """Reports the printed width of a selection, including its separators."""
    if not selected:
        return 0
    return sum(cell[1][1] + len(GAP) for cell in selected) - len(GAP)


def layout(
    columns: tuple[tuple[str, int], ...],
    width: int | None,
    order: tuple[int, ...] = (),
) -> tuple[list[tuple[int, tuple[str, int]]], list[str]]:
    """Chooses the columns that fit the available width.

    Args:
        columns: Every column as its heading and its wanted width.
        width: Available terminal columns, or None for the whole table.
        order: Column indices to drop first, least useful first. A column
            outside this order is never dropped, so a table always keeps the
            cells that identify its rows.

    Returns:
        The surviving columns paired with their original index, and the
        headings that were dropped, so a view can tell an operator which
        columns the terminal could not hold rather than letting them vanish.
    """
    selected = list(enumerate(columns))
    omitted: list[str] = []
    if width is None:
        return selected, omitted
    for index in order:
        if span(selected) <= width:
            break
        omitted.append(columns[index][0])
        selected = [cell for cell in selected if cell[0] != index]
    if selected and span(selected) > width:
        cell = max(1, (width - len(GAP) * (len(selected) - 1)) // len(selected))
        selected = [(index, (name, cell)) for index, (name, _) in selected]
    return selected, omitted


def heading(selected: list[tuple[int, tuple[str, int]]]) -> str:
    """Formats the heading row of a selection."""
    return GAP.join(fit(name, cell) for _, (name, cell) in selected).rstrip()


def line(
    values: tuple[str, ...], selected: list[tuple[int, tuple[str, int]]]
) -> str:
    """Formats one row, keeping only the cells the selection holds."""
    return GAP.join(
        fit(values[index], cell) for index, (_, cell) in selected
    ).rstrip()


def status_row(record: dict, offers: tuple[int, ...]) -> tuple[str, ...]:
    """Builds one participant row from a status reading.

    Args:
        record: One lane record from the status snapshot.
        offers: Issue numbers offered to this participant and still pending.

    Returns:
        One cell per column of `STATUS_COLUMNS`. A retired lane reports the
        retirement and how long ago it was in the session cell, so an operator
        does not read a lane that asked to stop as a lane that stalled.
        An issue past its deadline
        or its attempt budget is marked with an exclamation mark, a claim of
        a lane the supervisor read as orphaned with an asterisk, and so is a
        lane away from its assigned branch; no marker moves ownership or
        revokes anything.
        A mailbox that could not be read reports a question mark rather than
        a zero, which would claim the lane owes nothing. The review cell
        carries the latest verdict a peer recorded against this lane's
        report, which is that peer's claim and not a verification.
    """
    mail = record["mail"] or {}
    unreadable = "error" in mail
    held = ",".join(
        f"#{claim['issue']}"
        + ("!" if claim["overdue"] or claim["budget_exceeded"] else "")
        + ("*" if claim.get("orphaned") else "")
        for claim in record["claims"]
    )
    reported = record["report_age_seconds"]
    review = record.get("review") or {}
    return (
        record["participant"],
        record["provider"],
        record["credential"] or "default",
        session(
            record["session"],
            record["paused"],
            record["idle"]["stalled"],
            idle_age(record["idle"]),
            record.get("retired_age_seconds"),
        ),
        record["branch"] + ("!" if record["drift"] else ""),
        record["outcome"],
        review.get("verdict") or "-",
        (held + (f"+{len(offers)}" if offers else "")) or "-",
        "?" if unreadable else f"{mail['unread']}/{mail['pending_ack']}",
        "?"
        if unreadable
        else f"{mail['reservations']}"
        + (
            f"!{mail['stale_reservations']}"
            if mail["stale_reservations"]
            else ""
        ),
        "-" if reported is None else age(reported),
        mail.get("error", mail.get("task", "")).replace("\n", " "),
    )


def status_table(
    rows: list[tuple[str, ...]], width: int | None = None
) -> list[str]:
    """Formats participant rows as one table under one heading.

    Args:
        rows: Rows built by `status_row`, in the order they are reported.
        width: Available terminal columns, or None for the whole table.

    Returns:
        The heading, one line per row, and a line naming any column the width
        could not hold. The task column takes whatever width is left once
        every other column has its own, so the reported task is truncated
        before any identifying cell is dropped.
    """
    if not rows:
        return []
    measured = widths(STATUS_COLUMNS, rows)
    wanted = list(measured)
    if width is not None:
        wanted[-1] = MINIMUM_TASK
    selected, omitted = layout(
        tuple(zip(STATUS_COLUMNS, wanted, strict=True)), width, STATUS_DROP
    )
    if width is not None and selected and selected[-1][0] == len(wanted) - 1:
        spare = width - span(selected[:-1]) - len(GAP)
        if spare < MINIMUM_TASK:
            omitted.append(STATUS_COLUMNS[-1])
            selected = selected[:-1]
        else:
            name, _ = selected[-1][1]
            selected[-1] = (selected[-1][0], (name, min(spare, measured[-1])))
    lines = [heading(selected)]
    lines.extend(line(values, selected) for values in rows)
    if omitted:
        lines.append("Hidden columns: " + ", ".join(omitted))
    return lines


def pull_cell(claim: dict) -> str:
    """Names the pull request state one claim carries.

    Args:
        claim: One claim from a lane record of the status snapshot.

    Returns:
        ``ended`` with any observed branch state for a claim whose work ended
        on the forge, the cached open pull request's number, check verdict,
        how long a pending head has waited, any stall and any conflict, or
        ``-`` when none is cached.
    """
    if claim.get("ended"):
        state = str(claim.get("branch_state") or "").lower()
        return f"ended, {state}" if state else "ended"
    pull = claim.get("pull_request")
    if not pull:
        return "-"
    cell = f"#{pull['number']} CI {pull['checks'] or 'unknown'}"
    if pull["checks"] == "pending" and pull.get("pending_seconds") is not None:
        cell += f" {age(pull['pending_seconds'])}"
    if pull.get("stalled"):
        cell += ", stalled"
    if pull.get("mergeable") == "CONFLICTING":
        cell += ", conflicting"
    return cell


def work_row(claim: dict, owner: str, state: str) -> tuple[str, ...]:
    """Builds one open-work row from a claim and the lane holding it.

    Args:
        claim: One claim from a lane record of the status snapshot.
        owner: Participant holding the claim.
        state: State of the lane holding the claim.

    Returns:
        One cell per column of `WORK_COLUMNS`, the title clipped to
        `TITLE_WIDTH` with an ellipsis.
    """
    title = str(claim.get("title") or "-")
    seconds = claim.get("last_event_seconds")
    return (
        f"#{claim['issue']}",
        fit(title, TITLE_WIDTH).rstrip(),
        owner,
        state,
        age(seconds) if seconds is not None else "-",
        pull_cell(claim),
    )


def work_table(rows: list[tuple[str, ...]], width: int | None) -> list[str]:
    """Lays out the open-work rows of one project.

    Args:
        rows: Rows built by `work_row`, in the order they are reported.
        width: Available terminal columns, or None for the whole table.

    Returns:
        The heading, one line per row, and a line naming any column the width
        could not hold.
    """
    if not rows:
        return []
    selected, omitted = layout(
        tuple(zip(WORK_COLUMNS, widths(WORK_COLUMNS, rows), strict=True)),
        width,
        WORK_DROP,
    )
    lines = [heading(selected)]
    lines.extend(line(values, selected) for values in rows)
    if omitted:
        lines.append("Hidden columns: " + ", ".join(omitted))
    return lines


def lane_table(rows: list[tuple[str, ...]], width: int | None) -> list[str]:
    """Lays out one line per lane: name, state, live claims and current task.

    Args:
        rows: One row per lane, one cell per column of `LANE_COLUMNS`.
        width: Available terminal columns, or None for whole lines.

    Returns:
        The heading and one line per lane, each clipped to the width with an
        ellipsis, so the task text is what a narrow terminal loses.
    """
    if not rows:
        return []
    measured = widths(LANE_COLUMNS, rows)
    lines = [
        GAP.join(
            fit(value, cell)
            for value, cell in zip(values, measured, strict=True)
        ).rstrip()
        for values in (LANE_COLUMNS, *rows)
    ]
    if width is None:
        return lines
    return [fit(text, width).rstrip() for text in lines]
