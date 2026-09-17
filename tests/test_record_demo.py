"""Checks the demo recorder's framing, path rewriting and SVG output."""

import xml.etree.ElementTree as ElementTree
from pathlib import Path

from scripts import record_demo

SVG = "{http://www.w3.org/2000/svg}"


def step(prompt="$", command="agent-parley top", output=("ready",)):
    return record_demo.Step(prompt, command, tuple(output))


def test_captured_output_keeps_its_lines_and_drops_trailing_blanks():
    assert record_demo.lines("one\ntwo\n\n\n") == ("one", "two")


def test_a_line_wider_than_the_frame_wraps_the_way_a_terminal_wraps():
    body = "x" * (record_demo.COLUMNS + 3)
    wrapped = record_demo.lines(body)
    assert wrapped == ("x" * record_demo.COLUMNS, "xxx")


def test_a_short_step_is_shown_whole():
    rows = tuple(str(number) for number in range(record_demo.FRAME_LINES))
    assert record_demo.frame(step(output=rows)) == rows


def test_a_long_step_keeps_both_ends_and_states_what_it_hides():
    rows = tuple(str(number) for number in range(120))
    shown = record_demo.frame(step(output=rows))
    assert len(shown) == record_demo.FRAME_LINES
    assert shown[0] == "0"
    assert shown[-1] == "119"
    hidden = [row for row in shown if "not shown" in row]
    assert hidden == [f"[{120 - record_demo.FRAME_LINES + 1} lines not shown]"]


def test_recording_paths_are_replaced_by_the_published_shape():
    captured = [
        step(
            command="agent-parley setup /tmp/demo-x1/payments-api",
            output=("state: /tmp/demo-x1/state",),
        )
    ]
    places = {
        "/tmp/demo-x1/state": record_demo.DEMO_STATE,
        "/tmp/demo-x1/payments-api": record_demo.DEMO_REPOSITORY,
        "/tmp/demo-x1": record_demo.DEMO_HOME,
    }
    rewritten = record_demo.rewritten(captured, places)[0]
    assert rewritten.command.endswith(record_demo.DEMO_REPOSITORY)
    assert rewritten.output == (f"state: {record_demo.DEMO_STATE}",)
    assert "/tmp/" not in rewritten.command + rewritten.output[0]


def test_every_frame_owns_one_slice_of_the_loop():
    starts = []
    for index in range(4):
        element = ElementTree.fromstring(record_demo.schedule(index, 4, 8.0))
        assert element.get("repeatCount") == "indefinite"
        assert element.get("dur") == "8.0s"
        times = [float(value) for value in element.get("keyTimes").split(";")]
        assert times == sorted(times)
        starts.append(times[-2] if index else 0.0)
    assert starts == [0.0, 0.25, 0.5, 0.75]


def test_a_refusal_and_a_heading_are_not_drawn_as_body_text():
    deny = record_demo.colour('"permissionDecision": "deny"')
    heading = record_demo.colour("PARTICIPANT  STATE")
    body = record_demo.colour("  ready")
    assert deny == record_demo.REFUSAL
    assert heading == record_demo.HEADING
    assert body == record_demo.BODY


def test_the_asset_is_one_svg_showing_a_single_frame_at_a_time(tmp_path):
    captured = [
        step(command="agent-parley up", output=("ready",)),
        step(prompt="ada", command="file_reservation_paths", output=("{}",)),
    ]
    destination = tmp_path / "demo.svg"
    record_demo.render(captured, destination)
    root = ElementTree.fromstring(destination.read_text())
    groups = root.findall(f"{SVG}g")
    assert [group.get("opacity") for group in groups] == ["1", "0"]
    assert all(group.find(f"{SVG}animate") is not None for group in groups)
    drawn = [
        node.text or "".join(part.text or "" for part in node)
        for group in groups
        for node in group.findall(f"{SVG}text")
    ]
    assert "agent-parley up" in "".join(drawn)
    assert "file_reservation_paths" in "".join(drawn)


def test_the_committed_asset_is_the_one_the_readme_points_at():
    root = Path(__file__).resolve().parents[1]
    asset = root / "docs" / "assets" / "demo.svg"
    readme = (root / "README.md").read_text()
    assert asset.exists()
    assert "docs/assets/demo.svg" in readme
    assert "cdn.jsdelivr.net/gh/suneel944/agent-parley@main" in readme
    drawn = asset.read_text()
    assert "<animate" in drawn
    assert str(Path.home()) not in drawn
