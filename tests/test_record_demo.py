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


def test_a_step_limit_clips_below_the_frame_height():
    rows = tuple(str(number) for number in range(40))
    shown = record_demo.frame(
        record_demo.Step("$", "agent-parley issue claim 41", rows, limit=8)
    )
    assert len(shown) == 8
    assert shown[-1] == "39"


def test_every_item_fades_in_on_one_shared_timeline(tmp_path):
    captured = [
        record_demo.Card("CHAPTER 01", "Lanes"),
        step(command="agent-parley up", output=("ready",)),
        step(command="agent-parley status", output=("ada working",)),
    ]
    total = record_demo.render(captured, tmp_path / "demo.svg")
    root = ElementTree.fromstring((tmp_path / "demo.svg").read_text())
    starts = []
    for group in root.findall(f"{SVG}g"):
        fade = group.find(f"{SVG}animate")
        assert fade.get("dur") == f"{total}s"
        assert fade.get("repeatCount") == "indefinite"
        times = [float(value) for value in fade.get("keyTimes").split(";")]
        assert times == sorted(times)
        assert times[0] == 0.0 and times[-1] == 1.0
        starts.append(0.0 if group.get("opacity") == "1" else times[1])
    assert starts == sorted(starts)
    assert len(set(starts)) == len(captured)


def test_a_caption_is_drawn_below_the_terminal_not_inside_it(tmp_path):
    captioned = record_demo.Step(
        "$", "agent-parley up", ("ready",), caption="One server per host."
    )
    record_demo.render([captioned], tmp_path / "demo.svg")
    root = ElementTree.fromstring((tmp_path / "demo.svg").read_text())
    band = (
        record_demo.HEADER
        + record_demo.MARGIN * 2
        + record_demo.LINE * (record_demo.FRAME_LINES + 1)
    )
    for node in root.iter(f"{SVG}text"):
        if node.text == "One server per host.":
            assert float(node.get("y")) > band
            break
    else:
        raise AssertionError("caption not drawn")


def test_a_recording_run_from_a_lane_inherits_none_of_its_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "lane-secret")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "outer")
    values = record_demo.environment(tmp_path, tmp_path, tmp_path)
    assert "AGENT_PARLEY_TOKEN" not in values
    assert "CLAUDE_CODE_SESSION_ID" not in values
    assert values["AGENT_PARLEY_HOME"] == str(tmp_path)


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
        for node in group.iter(f"{SVG}text")
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
