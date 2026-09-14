"""Drives the live view's fitting, paging and keys without a terminal."""

import curses
import os
import sys
from pathlib import Path

import pytest

from agent_parley import dashboard, tables
from agent_parley.state import BridgeError

HOME = Path("/tmp/parley-top-view")


def lane(name: str, **changes: object) -> dict:
    """Builds one participant row without reading any recorded state."""
    row = {
        "participant": name,
        "provider_name": "codex",
        "provider": "codex/default",
        "credential": "",
        "state": "running; event 1s ago",
        "stalled": False,
        "stall": "",
        "stall_age": 0,
        "operator_edits": [],
        "operator_edit": "",
        "event_age": "1s",
        "last_event_ts": 1.0,
        "branch": f"parley/{name}",
        "drift": False,
        "owned": [],
        "overdue": [],
        "issues": "-",
        "offers": 0,
        "unread": 0,
        "pending_ack": 0,
        "leases": 0,
        "stale_leases": 0,
        "lease_age": 0,
        "injected_bytes": 0,
        "hook_events": 0,
        "denials": 0,
        "calls": 0,
        "errors": 0,
        "tokens": None,
        "idle_seconds": 0,
        "idle_complete": True,
        "fit": None,
        "unfit": "",
        "work_offer": False,
        "offer_kind": "",
        "awaiting_approval": False,
        "prompt": "",
    }
    return {**row, **changes}


def snapshot(*groups: tuple[str, list[dict]]) -> dict:
    """Builds a collected view from literal rows, with counted totals."""
    return dashboard.select(
        {
            "running": True,
            "home": str(HOME),
            "projects": [
                {"root": root, "rows": list(rows)} for root, rows in groups
            ],
            "totals": {},
            "providers": [],
            "window": 0.0,
        }
    )


def lanes(count: int, **changes: object) -> list[dict]:
    """Builds a numbered group of otherwise identical rows."""
    return [lane(f"lane-{index}", **changes) for index in range(count)]


def text(lines: list[str]) -> str:
    """Joins rendered lines for substring assertions."""
    return "\n".join(lines)


class Screen:
    """Replays scripted keys and records what each frame drew."""

    def __init__(
        self,
        keys: list[int],
        typed: list[bytes] | None = None,
        height: int = 20,
        width: int = 100,
    ) -> None:
        self.keys = list(keys)
        self.typed = list(typed or [])
        self.size = (height, width)
        self.cells: list[tuple[str, int]] = []
        self.frames: list[list[tuple[str, int]]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self.size

    def timeout(self, value: int) -> None:
        self.wait = value

    def keypad(self, value: bool) -> None:
        self.arrows = value

    def erase(self) -> None:
        self.cells = []

    def clear(self) -> None:
        self.cells = []

    def refresh(self) -> None:
        self.frames.append(list(self.cells))

    def addnstr(
        self,
        row: int,
        column: int,
        value: str,
        width: int,
        attribute: int = 0,
    ) -> None:
        self.cells.append((value[:width], attribute))

    def getch(self) -> int:
        return self.keys.pop(0) if self.keys else ord("q")

    def getstr(self, row: int, column: int, limit: int) -> bytes:
        return self.typed.pop(0) if self.typed else b""


class Sink:
    """Collects printed output and answers for itself whether it is a tty."""

    def __init__(self, terminal: bool) -> None:
        self.terminal = terminal
        self.printed = ""

    def write(self, value: str) -> int:
        self.printed += value
        return len(value)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return self.terminal


def test_a_column_is_as_wide_as_its_widest_value_in_the_frame():
    view = snapshot(("/repo", [lane("codex", branch="release/candidate-77")]))
    frame = dashboard.layout(view, 200)
    assert frame["omitted"] == []
    row = next(line for line in frame["lines"] if line.startswith("codex "))
    assert "release/candidate-77" in row
    assert "…" not in row
    assert "…" in tables.fit("release/candidate-77", 18)


def test_a_narrow_terminal_drops_columns_instead_of_clipping_every_cell():
    view = snapshot(("/repo", lanes(3)))
    lines = dashboard.render(view, 60)
    assert all(len(line) <= 60 for line in lines)
    hidden = next(line for line in lines if line.startswith("Hidden columns:"))
    assert "PARTICIPANT" not in hidden
    assert "MAIL" in text(lines)
    for dropped in ("PROVIDER", "EVENT", "BRANCH"):
        assert dropped in hidden


def test_columns_are_dropped_in_the_documented_order():
    view = snapshot(("/repo", lanes(2)))
    wide = dashboard.layout(view, 150)["omitted"]
    narrow = dashboard.layout(view, 100)["omitted"]
    assert wide == list(dashboard.DROP_ORDER[: len(wide)])
    assert narrow == list(dashboard.DROP_ORDER[: len(narrow)])
    assert len(narrow) > len(wide) > 0


def test_a_clipped_cell_ends_in_a_marker():
    view = snapshot(("/repo", [lane("codex", state="running; " + "x" * 60)]))
    assert "…" in text(dashboard.render(view, 100))


def test_a_page_reports_the_visible_range_and_never_drops_a_row_silently():
    frame = dashboard.layout(snapshot(("/repo", lanes(31))), 120, 12)
    assert frame["total"] == 31
    assert 0 < frame["shown"] < 31
    assert frame["footer"] == f"rows 1-{frame['shown']} of 31"
    assert len(frame["lines"]) <= 12


def test_paging_follows_the_selected_row_to_the_last_one():
    frame = dashboard.layout(snapshot(("/repo", lanes(31))), 120, 12, 30)
    assert frame["footer"] == f"rows {frame['first'] + 1}-31 of 31"
    assert frame["lines"][frame["cursor"]].startswith("lane-30")


def test_an_unbounded_frame_shows_every_row_and_the_legend():
    lines = dashboard.render(snapshot(("/repo", lanes(31))))
    assert sum(line.startswith("lane-") for line in lines) == 31
    assert not any(line.startswith("rows 1-") for line in lines)
    assert dashboard.LEGEND[0] in lines


def test_a_page_keeps_a_lane_with_its_marker_and_last_prompt():
    rows = lanes(6, stall="idle; message 3 waiting", prompt="build the gate")
    frame = dashboard.layout(snapshot(("/repo", rows)), 120, 14)
    shown = [line for line in frame["lines"] if line.startswith("    last:")]
    assert len(shown) == frame["shown"] >= 1
    markers = [line for line in frame["lines"] if "idle; message" in line]
    assert len(markers) == frame["shown"]


def test_ordering_puts_the_largest_counted_value_first():
    rows = [
        lane("quiet", idle_seconds=1),
        lane("loud", idle_seconds=900),
        lane("middle", idle_seconds=60),
    ]
    ordered = dashboard.select(snapshot(("/repo", rows)), "idle")
    assert [row["participant"] for row in ordered["projects"][0]["rows"]] == [
        "loud",
        "middle",
        "quiet",
    ]
    backwards = dashboard.select(snapshot(("/repo", rows)), "IDLE", True)
    assert backwards["projects"][0]["rows"][0]["participant"] == "quiet"
    assert "sort IDLE" in text(dashboard.render(ordered, 120))


def test_an_unknown_sort_column_is_refused():
    with pytest.raises(BridgeError) as failure:
        dashboard.select(snapshot(("/repo", lanes(1))), "tokens-per-hour")
    assert "Unknown sort column" in str(failure.value)


def test_narrowing_recounts_the_totals_it_reports():
    view = snapshot(
        ("/repo/one", [lane("codex", hook_events=4)]),
        ("/repo/two", [lane("claude", hook_events=6)]),
    )
    assert view["totals"]["events"] == 10
    narrowed = dashboard.select(view, "", False, ("two",))
    assert narrowed["totals"]["events"] == 6
    assert narrowed["totals"]["participants"] == 1
    assert "/repo/one" not in text(dashboard.render(narrowed))
    by_name = dashboard.select(view, "", False, (), ("codex",))
    assert by_name["totals"]["events"] == 4
    assert "no participants for the selection" in text(
        dashboard.render(by_name)
    )


def test_chosen_columns_are_the_only_ones_shown():
    view = snapshot(("/repo", lanes(2)))
    lines = dashboard.render(view, 120, columns=("PARTICIPANT", "IDLE"))
    assert "PROVIDER" not in text(lines)
    assert "IDLE" in text(lines)
    assert "columns PARTICIPANT,IDLE" in text(lines)


def test_an_unknown_column_choice_still_shows_the_table():
    view = snapshot(("/repo", lanes(1)))
    assert "PARTICIPANT" in text(dashboard.render(view, 120, columns=("XY",)))


def test_a_warning_row_is_marked_in_text_and_offered_for_colour():
    rows = [lane("codex"), lane("claude", drift=True)]
    frame = dashboard.layout(snapshot(("/repo", rows)), 200, 20)
    assert len(frame["alerts"]) == 1
    assert frame["lines"][frame["alerts"][0]].startswith("claude")
    assert "!" in frame["lines"][frame["alerts"][0]]
    assert dashboard.alert(lane("x", state="stopped"))
    assert dashboard.alert(lane("x", stale_leases=1))
    assert dashboard.alert(lane("x", errors=2))
    assert dashboard.alert(lane("x", overdue=["7"]))
    assert not dashboard.alert(lane("x"))


def test_the_detail_view_reports_every_field_in_full():
    row = lane("codex", branch="a" * 60, prompt="rebuild the release gate")
    lines = dashboard.detail(row)
    assert lines[0] == "participant codex"
    assert f"branch: {'a' * 60}" in lines
    assert "prompt: rebuild the release gate" in lines
    assert len(lines) > len(dashboard.COLUMNS)


def test_the_key_map_names_every_key_and_holds_the_legend():
    lines = dashboard.keymap()
    for key, _ in dashboard.KEYS:
        assert any(key in line for line in lines)
    assert dashboard.LEGEND[-1] in lines


def test_a_snapshot_to_a_pipe_keeps_every_column(monkeypatch):
    view = snapshot(("/repo", [lane("codex", branch="release/candidate-77")]))
    pipe = Sink(False)
    monkeypatch.setattr(dashboard, "collect", lambda *a, **k: view)
    monkeypatch.setattr(sys, "stdout", pipe)
    monkeypatch.setattr(
        dashboard.shutil,
        "get_terminal_size",
        lambda: os.terminal_size((40, 24)),
    )
    dashboard.run(HOME, lambda: True, once=True)
    assert "release/candidate-77" in pipe.printed
    assert "Hidden columns:" not in pipe.printed
    for name, _ in dashboard.COLUMNS:
        assert name in pipe.printed


def test_a_snapshot_to_a_terminal_fits_its_width(monkeypatch):
    view = snapshot(("/repo", [lane("codex")]))
    terminal = Sink(True)
    monkeypatch.setattr(dashboard, "collect", lambda *a, **k: view)
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setattr(
        dashboard.shutil,
        "get_terminal_size",
        lambda: os.terminal_size((70, 24)),
    )
    dashboard.run(HOME, lambda: True, once=True)
    assert all(len(line) <= 70 for line in terminal.printed.splitlines())
    assert "Hidden columns:" in terminal.printed


def drive(
    monkeypatch,
    view: dict,
    keys: list[int],
    typed: list[bytes] | None = None,
    screen: Screen | None = None,
) -> Screen:
    """Runs the live loop against scripted keys and a fixed snapshot."""
    monkeypatch.setattr(dashboard, "collect", lambda *a, **k: view)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "echo", lambda: None)
    monkeypatch.setattr(curses, "noecho", lambda: None)
    drawn = screen if screen is not None else Screen(keys, typed)
    dashboard._loop(
        drawn,
        HOME,
        lambda: True,
        0.1,
        (),
        0.0,
        {
            "sort": "",
            "reverse": False,
            "projects": (),
            "participants": (),
            "columns": (),
        },
    )
    return drawn


def selected(frame: list[tuple[str, int]]) -> str:
    """Reports the row the frame drew in reverse video."""
    return next(
        (line for line, attribute in frame if attribute & curses.A_REVERSE),
        "",
    )


def test_keys_page_through_the_rows_and_leave_on_q(monkeypatch):
    view = snapshot(("/repo", lanes(31)))
    screen = drive(
        monkeypatch, view, [ord("j"), curses.KEY_DOWN, ord("k"), ord("q")]
    )
    assert len(screen.frames) == 4
    assert selected(screen.frames[0]).startswith("lane-0")
    assert selected(screen.frames[1]).startswith("lane-1")
    assert selected(screen.frames[2]).startswith("lane-2")
    assert selected(screen.frames[3]).startswith("lane-1")


def test_the_footer_reports_the_range_and_the_key_hint(monkeypatch):
    screen = drive(monkeypatch, snapshot(("/repo", lanes(31))), [ord("q")])
    footer = screen.frames[0][-1][0]
    assert footer.startswith("rows 1-")
    assert "? keys" in footer
    assert "q leaves" in footer


def test_enter_opens_the_selected_lane_in_full(monkeypatch):
    view = snapshot(("/repo", [lane("codex", branch="b" * 40), lane("claud")]))
    screen = drive(monkeypatch, view, [ord("j"), 10, ord(" "), ord("q")])
    opened = screen.frames[2]
    assert opened[0][0] == "participant claud"
    assert any(line == "any key returns" for line, _ in opened)


def test_the_question_mark_shows_the_keys_in_place_of_the_legend(monkeypatch):
    screen = drive(
        monkeypatch, snapshot(("/repo", lanes(2))), [ord("?"), ord(" ")]
    )
    assert screen.frames[1][0][0] == "agent-parley top keys"
    assert not any(dashboard.LEGEND[0] in line for line, _ in screen.frames[0])


def test_single_keys_sort_reverse_and_narrow_the_live_view(monkeypatch):
    rows = [lane("quiet", idle_seconds=1), lane("loud", idle_seconds=900)]
    screen = drive(
        monkeypatch,
        snapshot(("/repo", rows)),
        [ord("s"), ord("r"), ord("f"), ord("q")],
        [b"loud"],
    )
    assert "sort PARTICIPANT" in text([line for line, _ in screen.frames[1]])
    assert "reversed" in text([line for line, _ in screen.frames[2]])
    last = text([line for line, _ in screen.frames[3]])
    assert "participant loud" in last
    assert "quiet" not in last


def test_a_resize_redraws_the_whole_frame(monkeypatch):
    screen = drive(
        monkeypatch,
        snapshot(("/repo", lanes(3))),
        [curses.KEY_RESIZE, ord("q")],
    )
    assert len(screen.frames) == 2
    assert screen.frames[1][0][0].startswith("agent-parley top")


def test_a_refused_write_is_reported_in_the_footer(monkeypatch):
    class Refusing(Screen):
        """Refuses one line the way a resized terminal does."""

        def addnstr(
            self,
            row: int,
            column: int,
            value: str,
            width: int,
            attribute: int = 0,
        ) -> None:
            if row == 1:
                raise curses.error("addnstr() returned ERR")
            super().addnstr(row, column, value, width, attribute)

    screen = drive(
        monkeypatch,
        snapshot(("/repo", lanes(3))),
        [ord("q")],
        screen=Refusing([ord("q")]),
    )
    footer = screen.frames[0][-1][0]
    assert "1 writes refused: addnstr() returned ERR" in footer
