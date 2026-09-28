"""Checks the exported coordination metrics in both output formats."""

import json
import re
import sys

import pytest

from agent_parley import cli, views

NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its standard output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    assert cli.main() == 0
    return capsys.readouterr().out


def samples(text, name):
    """Returns the sample lines of one metric family."""
    return [line for line in text.splitlines() if line.startswith(f"{name}{{")]


def snapshot(row, root="/tmp/project"):
    """Builds one collector snapshot around a single participant row."""
    return {
        "running": False,
        "home": "/tmp/home",
        "projects": [{"root": root, "rows": [row]}],
        "totals": {},
        "providers": [],
        "window": 0.0,
    }


def test_exposition_reports_every_lane_and_project_series(
    bridge, repo, paired, monkeypatch, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    text = run(monkeypatch, capsys, "--home", str(bridge.home), "metrics")
    assert text.endswith("\n")
    assert "# HELP agent_parley_lane_issues_held" in text
    assert "# TYPE agent_parley_lane_issues_held gauge" in text
    assert "# TYPE agent_parley_lane_hook_events_total counter" in text
    held = samples(text, "agent_parley_lane_issues_held")
    labels = (
        f'{{project="{paired["root"]}",participant="claude",provider="claude"}}'
    )
    assert f"agent_parley_lane_issues_held{labels} 1" in held
    assert len(held) == 2
    assert (
        f'agent_parley_project_participants{{project="{paired["root"]}"}} 2'
        in text
    )
    assert "agent_parley_lane_session_alive" in text


def test_exposition_names_and_labels_are_well_formed(
    bridge, repo, paired, monkeypatch, capsys
):
    text = run(monkeypatch, capsys, "--home", str(bridge.home), "metrics")
    for line in text.splitlines():
        if line.startswith("#"):
            assert line.split()[1:2] != []
            assert NAME.match(line.split()[2])
            continue
        name, _, rest = line.partition("{")
        assert NAME.match(name)
        for pair in rest.split("}")[0].split(","):
            assert NAME.match(pair.split("=")[0])
            assert pair.split("=", 1)[1].startswith('"')
    for family in views.LANE_METRICS + views.PROJECT_METRICS:
        assert NAME.match(family[0])
        assert family[1] in ("counter", "gauge")
        assert family[0].endswith("_total") == (family[1] == "counter")


def test_a_label_value_escapes_quotes_backslashes_and_newlines():
    root = 'a\\b"c\nd'
    view = snapshot({"participant": "claude", "provider_name": "x"}, root)
    text = views.exposition(view)
    assert 'project="a\\\\b\\"c\\nd"' in text
    assert len(text.splitlines()) == (len(views.LANE_METRICS) + 1) * 2 + (
        len(views.PROJECT_METRICS) * 3
    )


def test_a_description_escapes_backslashes_and_newlines():
    assert views._description("a\\b\nc") == "a\\\\b\\nc"


def test_an_unread_measurement_reports_no_sample():
    row = {
        "participant": "claude",
        "provider_name": "claude",
        "tokens": None,
        "unread": "?",
        "calls": 3,
    }
    text = views.exposition(snapshot(row))
    assert samples(text, "agent_parley_lane_tokens_total") == []
    assert samples(text, "agent_parley_lane_mail_unread") == []
    assert "# TYPE agent_parley_lane_tokens_total counter" in text
    assert samples(text, "agent_parley_lane_served_calls_total") == [
        'agent_parley_lane_served_calls_total{project="/tmp/project",'
        'participant="claude",provider="claude"} 3'
    ]


def test_json_reports_the_same_values_as_one_object(
    bridge, repo, paired, monkeypatch, capsys
):
    bridge.issue(paired["lanes"]["claude"], "claim", "42")
    document = json.loads(
        run(
            monkeypatch, capsys, "--home", str(bridge.home), "metrics", "--json"
        )
    )
    assert document["schema"] == views.SCHEMA
    assert document["kind"] == "metrics"
    assert document["generated_at"].endswith("Z")
    assert document["state_directory"] == str(bridge.home)
    assert document["window_seconds"] is None
    assert document["providers"] == []
    reported = {family["name"]: family for family in document["metrics"]}
    held = reported["agent_parley_lane_issues_held"]
    assert held["type"] == "gauge"
    assert held["help"]
    owner = next(
        sample
        for sample in held["samples"]
        if sample["labels"]["participant"] == "claude"
    )
    assert owner["value"] == 1
    assert owner["labels"] == {
        "project": paired["root"],
        "participant": "claude",
        "provider": "claude",
    }
    totals = reported["agent_parley_project_participants"]
    assert totals["samples"][0]["value"] == 2


def test_a_provider_filter_narrows_the_exported_lanes(
    bridge, repo, paired, monkeypatch, capsys
):
    text = run(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "metrics",
        "--provider",
        "codex",
    )
    lanes = samples(text, "agent_parley_lane_session_alive")
    assert len(lanes) == 1
    assert 'participant="codex"' in lanes[0]


def test_an_output_file_receives_the_whole_frame(
    bridge, repo, paired, monkeypatch, capsys, tmp_path
):
    destination = tmp_path / "coordination.prom"
    run(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "metrics",
        "--output",
        str(destination),
    )
    assert capsys.readouterr().out == ""
    written = destination.read_text()
    assert written.startswith("# HELP agent_parley_lane_session_alive")
    assert written.endswith("\n")
    assert len(samples(written, "agent_parley_lane_session_alive")) == 2
    assert [
        entry.name
        for entry in tmp_path.iterdir()
        if entry.name.startswith("tmp")
    ] == []


def test_an_interval_needs_a_destination(
    bridge, repo, paired, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-parley", "--home", str(bridge.home), "metrics", "--every", "5"],
    )
    with pytest.raises(SystemExit) as refused:
        cli.main()
    assert refused.value.code == 2
    assert "--every needs --output" in capsys.readouterr().err
