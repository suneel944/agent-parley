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

GAP = "  "
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


def fit(value: str, width: int) -> str:
    """Pads a cell, marking any value the column could not show in full.

    Args:
        value: Cell text.
        width: Column width.

    Returns:
        Text padded to the column width, ending in an ellipsis when clipped,
        so a truncated branch or issue list never reads as complete.
    """
    if len(value) > width:
        return value[: width - 1] + "…"
    return value.ljust(width)


def session(
    liveness: str, paused: bool, stalled: bool, stall_age: float
) -> str:
    """Describes a lane's session the same way in every operator view.

    Args:
        liveness: Reading taken from the lane's own checkpoint records.
        paused: Whether the participant is paused.
        stalled: Whether the lane is alive and has served no coordination
            call inside the configured interval.
        stall_age: Seconds the lane has been in that state.

    Returns:
        One cell naming the state the lane is in. A paused lane reports its
        pause first, an idle lane reports how long it has been idle, and a
        lane whose checkpoints are unreadable says so rather than claiming
        enforcement it cannot observe.
    """
    if paused:
        return f"paused; {liveness}"
    if stalled:
        return f"idle {age(stall_age)}; {liveness}"
    if "checkpoints unavailable" in liveness:
        return "running; no hooks"
    return liveness


def widths(columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[int]:
    """Measures each column against its heading and its widest value."""
    return [
        max(len(name), *(len(row[index]) for row in rows), 1)
        if rows
        else len(name)
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
        One cell per column of `STATUS_COLUMNS`. An issue past its deadline
        or its attempt budget is marked, and so is a lane away from its
        assigned branch; neither marker moves ownership or revokes anything.
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
            record["idle"]["age_seconds"],
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
