"""Checks the live demo recorder's timeline, redaction and SVG output."""

import xml.etree.ElementTree as ElementTree

from scripts import record_live

SVG = "{http://www.w3.org/2000/svg}"


def frame(at, **screens):
    return record_live.Frame(
        at, {name: tuple(lines) for name, lines in screens.items()}
    )


def test_silence_is_shortened_to_the_hold_and_activity_keeps_its_timing():
    frames = [frame(0.0), frame(0.5), frame(60.0), frame(61.0)]
    starts = record_live.timeline(frames)
    hold = record_live.HOLD
    assert starts[:4] == [0.0, 0.5, 0.5 + hold, 1.5 + hold]
    assert starts[-1] == starts[3] + hold * 2


def test_an_element_shown_throughout_carries_no_animation():
    assert record_live.visibility(0.0, 10.0, 10.0) == ""


def test_an_element_is_visible_only_inside_its_stretch():
    middle = record_live.visibility(2.0, 5.0, 10.0)
    assert 'values="0;1;0"' in middle and 'keyTimes="0;0.2;0.5"' in middle
    first = record_live.visibility(0.0, 5.0, 10.0)
    assert 'values="1;0"' in first and 'keyTimes="0;0.5"' in first
    last = record_live.visibility(5.0, 10.0, 10.0)
    assert 'values="0;1"' in last and 'keyTimes="0;0.5"' in last


def test_an_unchanged_row_is_one_stretch_and_blank_rows_are_dropped():
    frames = [
        frame(0.0, claude=["$ run", ""]),
        frame(1.0, claude=["$ run", "claim #41"]),
        frame(2.0, claude=["$ run", "claim #41"]),
    ]
    starts = record_live.timeline(frames)
    assert record_live.spans(frames, starts, "claude", 0) == [
        ("$ run", 0.0, starts[-1])
    ]
    assert record_live.spans(frames, starts, "claude", 1) == [
        ("claim #41", 1.0, starts[-1])
    ]


def test_machine_values_are_rewritten_and_survivors_are_reported():
    places = {"/tmp/parley-demo-x/payments-api": "/home/dev/payments-api"}
    frames = [frame(0.0, claude=["cd /tmp/parley-demo-x/payments-api"])]
    fixed = record_live.redact(frames, places)
    assert fixed[0].screens["claude"] == ("cd /home/dev/payments-api",)
    assert record_live.leaks(fixed, places) == []
    leaked = [frame(1.0, codex=["mail from someone@corp.io"])]
    assert record_live.leaks(leaked, places) == [
        "codex at 1.0s: mail from someone@corp.io"
    ]
    assert record_live.leaks([frame(2.0, codex=["dev@example.com"])], {}) == []


def test_the_svg_draws_every_pane_label_and_line(tmp_path):
    panes = [
        record_live.Pane("claude", "%0", 0, 0, 20, 2),
        record_live.Pane("codex", "%2", 21, 0, 20, 2),
        record_live.Pane("operator", "%1", 0, 3, 41, 1),
    ]
    frames = [
        frame(0.0, claude=["$ run"], codex=["$"], operator=["top"]),
        frame(1.0, claude=["$ run", "refused"], codex=["$"], operator=["top"]),
    ]
    destination = tmp_path / "demo.svg"
    record_live.render(frames, panes, destination)
    texts = [
        "".join(node.itertext())
        for node in ElementTree.parse(destination).iter(f"{SVG}text")
    ]
    for expected in ("claude", "codex", "operator", "$ run", "refused", "top"):
        assert expected in texts
