"""Checks the dependency graph, the critical-path ranking and its readers."""

import json
import sys
import time
from pathlib import Path

import pytest

from agent_parley import cli, critical, forge, recommend
from agent_parley.state import write_json
from agent_parley.status import FORGE_ISSUES


@pytest.fixture(autouse=True)
def quiet_forge(monkeypatch):
    """Keeps the shortlist off the host forge."""
    monkeypatch.setattr(
        "agent_parley.recommend.forge.issue_pull_request_paths",
        lambda repo, number: [],
    )
    monkeypatch.setattr(
        "agent_parley.recommend.forge.issue_providers",
        lambda repo, limit=forge.MAX_OPEN_ISSUES: {},
    )


def found(blockers, open_issues=None):
    """Builds the graph of the issues the blocker mapping names."""
    names = {
        *blockers,
        *(other for waiting in blockers.values() for other in waiting),
    }
    return critical.graph(blockers, set(open_issues or names))


def ledger(*numbers, owners=None):
    """Builds a ledger of unowned, authorized issues."""
    owners = owners or {}
    return {
        "revision": 1,
        "issues": {
            number: {
                "owner": owners.get(number),
                "offer": None,
                "blocked_by": [],
                "title": f"Issue {number}",
                "execution": {"authorized": True},
            }
            for number in numbers
        },
    }


@pytest.mark.parametrize(
    "line",
    [
        "Blocked by #12",
        "blocked by #12",
        "BLOCKED BY: #12",
        "Depends on #12",
        "depends on: #12",
        "- Blocked by #12",
        "* Depends on #12",
        "  - depends on: #12",
    ],
)
def test_both_line_forms_are_read_in_any_case_and_list_form(line):
    assert critical.references(f"Intro\n\n{line}\n\nMore") == ["12"]


def test_several_numbers_on_one_line_are_deduplicated_and_ordered():
    body = "Blocked by #30, #4 and #12\nDepends on #4, and #7\nBlocked by #30"
    assert critical.references(body) == ["4", "7", "12", "30"]


def test_references_inside_code_and_plain_mentions_are_ignored():
    body = "\n".join(
        [
            "See #5 for context and fixes #6.",
            "The text `Blocked by #7` is an example.",
            "```",
            "Blocked by #8",
            "```",
            "~~~text",
            "Depends on #9",
            "~~~",
            "",
            "    Blocked by #10",
            "",
            "```` md",
            "```",
            "Blocked by #11",
            "```",
            "````",
            "Blocked by #12",
        ]
    )
    assert critical.references(body) == ["12"]


def test_an_empty_or_missing_body_has_no_references():
    assert critical.references("") == []
    assert critical.references(None) == []


def test_a_chain_has_reach_and_length_from_each_link():
    graph = found({"2": ["1"], "3": ["2"], "4": ["3"]})
    assert critical.reach(graph, "1") == (3, 3)
    assert critical.reach(graph, "2") == (2, 2)
    assert critical.reach(graph, "4") == (0, 0)
    assert critical.path(graph) == ["1", "2", "3", "4"]


def test_fan_in_counts_each_unblocked_issue_once():
    graph = found({"3": ["1", "2"], "4": ["3"], "5": ["1"]})
    assert graph["unblocks"]["1"] == ["3", "5"]
    assert critical.reach(graph, "1") == (3, 2)
    assert critical.reach(graph, "2") == (2, 2)
    assert critical.reach(graph, "5") == (0, 0)
    assert critical.path(graph) == ["1", "3", "4"]


def test_the_path_breaks_ties_toward_the_lowest_numbers():
    graph = found({"3": ["1"], "4": ["2"], "5": ["1"]})
    assert critical.path(graph) == ["1", "3"]


def test_edges_to_closed_issues_are_left_out():
    graph = critical.graph({"2": ["1"], "3": ["2"]}, {"2", "3"})
    assert graph["unblocks"] == {"2": ["3"]}


def test_no_edge_means_no_reach_and_no_path():
    graph = critical.graph({}, {"1", "2"})
    assert critical.reach(graph, "1") == (0, 0)
    assert critical.path(graph) == []


def test_a_cycle_is_reported_and_its_edges_are_ignored():
    graph = found({"1": ["3"], "2": ["1"], "3": ["2"], "4": ["3"]})
    assert graph["cycles"] == [["1", "2", "3"]]
    assert graph["unblocks"] == {"3": ["4"]}
    assert critical.reach(graph, "1") == (0, 0)
    assert critical.reach(graph, "3") == (1, 1)


def test_a_self_reference_is_a_cycle_of_one():
    graph = found({"1": ["1"], "2": ["1"]})
    assert graph["cycles"] == [["1"]]
    assert critical.reach(graph, "1") == (1, 1)


def test_a_long_chain_does_not_exhaust_the_stack():
    size = 3000
    chain = {str(n + 1): [str(n)] for n in range(1, size)}
    graph = found(chain)
    assert critical.reach(graph, "1") == (size - 1, size - 1)
    assert len(critical.path(graph)) == size


def test_milestone_edges_join_the_forge_and_ledger_and_keep_one_milestone():
    catalog = {
        "1": {"milestone": "M"},
        "2": {"milestone": "M", "blocked_by": ["1"]},
        "3": {"milestone": "M"},
        "4": {"milestone": "N", "blocked_by": ["1"]},
        "5": {"milestone": "M"},
    }
    state = ledger("2", "3", "5")
    state["issues"]["3"]["blocked_by"] = ["2"]
    state["issues"]["5"]["execution"] = {
        "authorized": True,
        "state": "complete",
    }
    blockers, opened = critical.milestone_edges(catalog, state, "M")
    assert opened == {"1", "2", "3"}
    assert blockers == {"2": ["1"], "3": ["2"]}
    blockers, opened = critical.milestone_edges(catalog, state, None)
    assert opened == {"1", "2", "3", "4"}
    assert blockers == {"2": ["1"], "3": ["2"]}


def test_an_issue_with_an_edge_outranks_one_without_at_equal_terms():
    state = ledger("1", "2", "3")
    plain = recommend.rank(state, {}, {}, {}, "claude")
    assert [r["issue"] for r in plain] == ["1", "2", "3"]
    graph = found({"2": ["3"]})
    ranked = recommend.rank(state, {}, {}, {}, "claude", found=graph)
    assert [r["issue"] for r in ranked] == ["3", "1", "2"]
    first = ranked[0]
    assert (first["reach"], first["chain"], first["cycle"]) == (1, 1, [])
    assert first["reasons"][1] == (
        "critical path: unblocks 1 open issue, longest chain 1"
    )


def test_the_reason_counts_issues_and_names_the_cycle():
    state = ledger("1", "2", "3", "4", "5", "6")
    graph = found({"2": ["1"], "3": ["1"], "4": ["3"], "5": ["6"], "6": ["5"]})
    ranked = {
        r["issue"]: r
        for r in recommend.rank(state, {}, {}, {}, "claude", found=graph)
    }
    assert ranked["1"]["reasons"][1] == (
        "critical path: unblocks 3 open issues, longest chain 2"
    )
    assert "dependency cycle with #6 ignored" in ranked["5"]["reasons"]
    assert ranked["5"]["cycle"] == ["5", "6"]
    assert not any("critical path" in r for r in ranked["5"]["reasons"])


def test_a_graph_without_edges_leaves_the_order_unchanged():
    state = ledger("4", "9", "2")
    bare = recommend.rank(state, {}, {}, {}, "claude")
    empty = recommend.rank(
        state, {}, {}, {}, "claude", found=critical.graph({}, {"2", "4", "9"})
    )
    assert [r["issue"] for r in empty] == [r["issue"] for r in bare]
    assert [r["reasons"] for r in empty] == [r["reasons"] for r in bare]


def test_render_adds_a_line_per_cycle():
    state = ledger("1", "2", "3")
    graph = found({"1": ["2"], "2": ["1"]})
    result = {
        "provider": "claude",
        "forge_paths": False,
        "candidates": recommend.rank(state, {}, {}, {}, "claude", found=graph),
        "cycles": graph["cycles"],
    }
    lines = recommend.render(result).splitlines()
    assert (
        "Dependency cycle: #1, #2; its edges are ignored in the ranking."
        in lines
    )


def test_the_catalog_reads_edges_from_bodies_and_forge_relationships(
    tmp_path, monkeypatch
):
    calls = []
    issue_list = json.dumps(
        [
            {
                "number": 1,
                "title": "Root",
                "labels": [],
                "assignees": [],
                "body": "Notes\nBlocked by #9\n```\nDepends on #8\n```",
                "milestone": {"title": "M1"},
            },
            {
                "number": 2,
                "title": "Leaf",
                "labels": [],
                "assignees": [],
                "body": None,
                "milestone": None,
            },
        ]
    )
    relations = json.dumps(
        {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "number": 2,
                                "blockedBy": {"nodes": [{"number": 1}]},
                            },
                            {"number": 1, "blockedBy": {"nodes": []}},
                            {
                                "number": 77,
                                "blockedBy": {"nodes": [{"number": 1}]},
                            },
                        ]
                    }
                }
            }
        }
    )

    def reply(args, timeout):
        calls.append(args)
        return relations if args[1] == "api" else issue_list

    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(forge, "_run", reply)
    catalog = forge.open_issue_catalog(tmp_path)
    assert len(calls) == 2
    assert "body" in calls[0][calls[0].index("--json") + 1]
    assert "-F" in calls[1] and "count=1000" not in calls[1]
    assert catalog["1"]["blocked_by"] == ["9"]
    assert catalog["1"]["milestone"] == "M1"
    assert catalog["2"]["blocked_by"] == ["1"]
    assert catalog["2"]["milestone"] == ""
    assert "body" not in catalog["1"]
    assert "77" not in catalog


def test_a_failed_relationship_read_is_not_a_forge_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(forge, "_failure", {})
    monkeypatch.setattr(forge, "_recorded", {})
    issue_list = (
        '[{"number": 1, "title": "Root", "labels": [], "assignees": [],'
        ' "body": "Depends on #4", "milestone": null}]'
    )

    def reply(args, timeout):
        if args[1] == "api":
            forge._record_failure("error", "no such field")
            return None
        return issue_list

    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(forge, "_run", reply)
    catalog = forge.open_issue_catalog(tmp_path)
    assert catalog["1"]["blocked_by"] == ["4"]
    assert forge.last_failure() is None
    assert forge.last_failure(own=True) is None


def test_an_empty_catalog_skips_the_relationship_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(forge, "_implementation", lambda repo: "github")
    monkeypatch.setattr(forge, "_reachable", lambda repo: "owner/name")
    monkeypatch.setattr(
        forge, "_run", lambda args, timeout: calls.append(args) or "[]"
    )
    assert forge.open_issue_catalog(tmp_path) == {}
    assert len(calls) == 1


def recorded(directory, age=0.0):
    """Writes a cached milestone of ten issues with chains and a cycle.

    Milestone M1: 5 blocks 3 and 7, which both block 9, which blocks 10;
    1 and 2 wait on each other; 4 stands alone. Issue 8 sits in milestone M2
    and blocks 4, an edge across milestones that never counts.
    """
    edges = {
        "3": ["5"],
        "7": ["5"],
        "9": ["3", "7"],
        "10": ["9"],
        "1": ["2"],
        "2": ["1"],
        "4": ["8"],
    }
    titles = {
        "1": "Loop a",
        "2": "Loop b",
        "3": "Left",
        "4": "Alone",
        "5": "Root",
        "7": "Right",
        "8": "Elsewhere",
        "9": "Join",
        "10": "Tail",
    }
    write_json(
        directory / FORGE_ISSUES,
        {
            "read_at": time.time() - age,
            "limit": 1000,
            "issues": {
                number: {
                    "title": title,
                    "labels": [],
                    "assignees": [],
                    "milestone": "M2" if number == "8" else "M1",
                    "blocked_by": edges.get(number, []),
                }
                for number, title in titles.items()
            },
        },
    )


def order(ranked):
    """Lists the candidate numbers of one reading."""
    return [record["issue"] for record in ranked["candidates"]]


def test_a_recorded_milestone_ranks_by_what_each_issue_unblocks(
    bridge, repo, paired
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    recorded(directory)
    ranked = bridge.issue_next(lane, 20)
    assert order(ranked) == ["5", "3", "7", "9", "1", "2", "4", "8", "10"]
    assert ranked["cycles"] == [["1", "2"]]
    by_number = {record["issue"]: record for record in ranked["candidates"]}
    assert (by_number["5"]["reach"], by_number["5"]["chain"]) == (4, 3)
    assert (by_number["4"]["reach"], by_number["4"]["chain"]) == (0, 0)
    assert (
        "critical path: unblocks 4 open issues, longest chain 3"
        in (by_number["5"]["reasons"])
    )
    assert "dependency cycle with #2 ignored" in by_number["1"]["reasons"]
    text = recommend.render(ranked)
    assert (
        "Dependency cycle: #1, #2; its edges are ignored in the ranking."
        in text
    )


def test_a_stale_cache_leaves_the_ledger_edges_alone(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    recorded(lane.parent, age=10**6)
    ranked = bridge.issue_next(lane, 20)
    assert ranked["candidates"] == []
    assert ranked["cycles"] == []


def test_milestone_path_prints_the_chain_with_state_and_holder(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    recorded(directory)
    write_json(
        directory / "issues.json",
        {
            "revision": 1,
            "issues": {
                "3": {
                    "owner": "claude",
                    "claim_id": "c",
                    "offer": None,
                    "blocked_by": [],
                    "execution": {
                        "authorized": True,
                        "state": "ready",
                        "claim_id": "c",
                    },
                }
            },
        },
    )
    before = (directory / "issues.json").read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "milestone",
            "path",
            "--milestone",
            "M1",
            "--repo",
            str(lane),
        ],
    )
    assert cli.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "Critical path of M1 (4 issues):",
        "#5 Root: unclaimed, nobody",
        "#3 Left: ready, claude",
        "#9 Join: unclaimed, nobody",
        "#10 Tail: unclaimed, nobody",
        "Dependency cycle: #1, #2; its edges are ignored in the ranking.",
    ]
    assert (directory / "issues.json").read_bytes() == before
    ranked = bridge.issue_next(lane, 20)
    assert "3" not in order(ranked)
    assert (directory / "issues.json").read_bytes() == before


def test_milestone_path_json_and_the_empty_milestone(
    bridge, repo, paired, monkeypatch, capsys
):
    lane = Path(paired["lanes"]["claude"])
    recorded(lane.parent)
    reading = bridge.milestone_path(lane, "M2")
    assert reading["path"] == []
    assert recommend.render_path(reading) == (
        "No dependency edge is recorded for M2."
    )
    monkeypatch.setattr(forge, "active_milestone", lambda repo: None)
    reading = bridge.milestone_path(lane)
    assert reading["milestone"] is None
    assert [step["issue"] for step in reading["path"]] == [
        "5",
        "3",
        "9",
        "10",
    ]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "milestone",
            "path",
            "--json",
            "--repo",
            str(lane),
        ],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "milestone_path"
    assert document["cycles"] == [["1", "2"]]
