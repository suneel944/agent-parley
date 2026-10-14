"""Checks the exported coordination metrics in both output formats."""

import json
import re
import sys
from pathlib import Path

import pytest

from agent_parley import cli, views

NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
ROOT = Path(__file__).resolve().parent.parent


def test_the_operations_guide_lists_every_exported_family():
    text = (ROOT / "docs/operations.md").read_text()
    section = text.split("### Metrics a monitoring stack can read")[1]
    section = section.split("\n#")[0]
    documented = set(re.findall(r"`(agent_parley_[a-z_]+)`", section))
    exported = {family["name"] for family in views.families({"projects": []})}
    assert documented == exported


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
    assert "# TYPE agent_parley_lane_hook_events_window gauge" in text
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


def test_no_windowed_or_retained_family_is_typed_counter():
    exported = [
        family
        for family in views.families({"projects": []})
        if not family.get("deprecated")
    ]
    counters = {
        family["name"] for family in exported if family["type"] == "counter"
    }
    assert counters == {"agent_parley_lane_tokens_total"}
    for family in exported:
        if "window" in family["help"] or "Retained" in family["help"]:
            assert family["type"] == "gauge"
        if family["type"] == "gauge":
            assert not family["name"].endswith("_total")


OLD_NAMES = {
    "agent_parley_lane_context_bytes_total",
    "agent_parley_lane_hook_events_total",
    "agent_parley_lane_hook_denials_total",
    "agent_parley_lane_hook_denials_by_cause_total",
    "agent_parley_lane_served_calls_total",
    "agent_parley_lane_served_rejections_total",
    "agent_parley_lane_pull_requests_opened_total",
    "agent_parley_lane_pull_requests_merged_total",
    "agent_parley_lane_pull_requests_closed_total",
    "agent_parley_project_context_bytes_total",
    "agent_parley_project_hook_events_total",
    "agent_parley_project_hook_denials_total",
}


def test_every_0_18_name_is_exported_as_a_deprecated_counter():
    exported = {
        family["name"]: family for family in views.families({"projects": []})
    }
    for name in OLD_NAMES:
        alias = exported[name]
        assert alias["type"] == "counter"
        assert alias["deprecated"] is True
        assert alias["replacement"] in exported
        assert alias["help"].startswith(
            f"Deprecated: use {alias['replacement']};"
        )
        assert "breaking" in alias["help"]
    deprecated = {
        name for name, family in exported.items() if family.get("deprecated")
    }
    assert deprecated == OLD_NAMES


def test_each_alias_reports_the_values_of_its_replacement():
    row = {
        "participant": "claude",
        "provider_name": "claude",
        **views.NO_OUTCOMES,
        "injected_bytes": 2048,
        "hook_events": 9,
        "denials": 2,
        "calls": 5,
        "errors": 1,
        "pull_requests_opened": 3,
        "pull_requests_merged": 2,
        "pull_requests_closed": 1,
        "denied_by": [{"reason": "policy", "tool": "Bash", "count": 2}],
    }
    view = snapshot(row)
    text = views.exposition(view)
    for new, old in views.DEPRECATED_ALIASES.items():
        replaced = samples(text, new)
        assert replaced
        assert [line.removeprefix(old) for line in samples(text, old)] == [
            line.removeprefix(new) for line in replaced
        ]
        assert f"# TYPE {old} counter" in text
    document = views.measurements(view)
    reported = {family["name"]: family for family in document["metrics"]}
    for new, old in views.DEPRECATED_ALIASES.items():
        assert reported[old]["samples"] == reported[new]["samples"]
        assert reported[old]["replacement"] == new
        assert "deprecated" not in reported[new]


def test_a_lane_without_merges_reports_counts_but_no_ratio_or_median():
    row = {
        "participant": "claude",
        "provider_name": "claude",
        **views.NO_OUTCOMES,
    }
    text = views.exposition(snapshot(row))
    for name in (
        "agent_parley_lane_first_pass_ratio",
        "agent_parley_lane_ci_rounds_median",
        "agent_parley_lane_claim_to_merge_seconds_median",
    ):
        assert samples(text, name) == []
        assert f"# TYPE {name} gauge" in text
    assert samples(text, "agent_parley_lane_pull_requests_merged_window") == [
        'agent_parley_lane_pull_requests_merged_window{project="/tmp/project",'
        'participant="claude",provider="claude"} 0'
    ]


def test_a_label_value_escapes_quotes_backslashes_and_newlines():
    root = 'a\\b"c\nd'
    view = snapshot({"participant": "claude", "provider_name": "x"}, root)
    text = views.exposition(view)
    assert 'project="a\\\\b\\"c\\nd"' in text
    project_aliases = [
        name
        for name in views.DEPRECATED_ALIASES
        if name.startswith("agent_parley_project_")
    ]
    lane_aliases = len(views.DEPRECATED_ALIASES) - len(project_aliases)
    lane_families = len(views.LANE_METRICS) + 1 + lane_aliases
    project_families = len(views.PROJECT_METRICS) + len(project_aliases)
    assert len(text.splitlines()) == lane_families * 2 + project_families * 3


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
    assert samples(text, "agent_parley_lane_served_calls_retained") == [
        'agent_parley_lane_served_calls_retained{project="/tmp/project",'
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
