"""Checks Python symbol reservations against the store, MCP and Git."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agent_parley import checkpoints, cli, dashboard, store, symbols

MODULE = (
    "import os\n"
    "\n"
    "\n"
    "class Store:\n"
    "    def claim(self):\n"
    "        return 1\n"
    "\n"
    "    def release(self):\n"
    "        return 2\n"
)


@pytest.fixture
def lanes(bridge, paired, service):
    """Registers both lanes and writes the module the codex lane edits."""
    store.initialize(bridge.home)
    for name in ("claude", "codex"):
        asyncio.run(bridge.identity(name, paired))
    lane = Path(paired["lanes"]["codex"])
    (lane / "src").mkdir(exist_ok=True)
    (lane / "src" / "store.py").write_text(MODULE)
    tokens = {
        name: json.loads((lane.parent / f"{name}-identity.json").read_text())[
            "registration_token"
        ]
        for name in ("claude", "codex")
    }
    return {
        "lane": lane,
        "directory": lane.parent,
        "root": paired["root"],
        "tokens": tokens,
        "service": service,
    }


def reserve(lanes, name, *keys):
    """Reserves keys exclusively as one lane over the local MCP transport."""
    response = httpx.post(
        f"http://127.0.0.1:{lanes['service'].server_port}/mcp/",
        headers={"Authorization": f"Bearer {lanes['tokens'][name]}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "file_reservation_paths",
                "arguments": {
                    "paths": list(keys),
                    "ttl_seconds": 300,
                    "exclusive": True,
                    "reason": "symbol work",
                },
            },
        },
        timeout=30,
        trust_env=False,
    )
    assert response.status_code == 200
    return response.json()["result"]


def granted(result):
    """Returns the decoded payload of a tool call that succeeded."""
    assert "isError" not in result, result
    return json.loads(result["content"][0]["text"])


def edit(bridge, lanes, old, new):
    """Runs the codex PreToolUse decision for one edit of src/store.py."""
    return checkpoints.checkpoint(
        bridge.home,
        lanes["directory"],
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(lanes["lane"]),
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(lanes["lane"] / "src" / "store.py"),
                "old_string": old,
                "new_string": new,
            },
            "tool_use_id": f"call-{old}-{new}",
        },
    ).get("hookSpecificOutput", {})


def test_an_edit_inside_a_reserved_symbol_is_refused(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    decision = edit(bridge, lanes, "return 1", "return 3")

    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "src/store.py::Store.claim" in reason
    assert "claude" in reason
    assert "advisory" in reason


def test_an_edit_of_another_symbol_in_the_file_is_allowed(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    decision = edit(bridge, lanes, "return 2", "return 4")

    assert decision.get("permissionDecision") != "deny"


def test_two_symbols_share_a_file_and_a_path_covers_both(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    own = granted(reserve(lanes, "codex", "src/store.py::Store.release"))
    whole = granted(reserve(lanes, "codex", "src/store.py"))
    method = granted(reserve(lanes, "codex", "src/store.py::Store"))

    assert own["granted"]
    assert whole["granted"] == []
    assert whole["conflicts"][0]["owner"] == "claude"
    assert method["granted"] == []


def test_a_path_reservation_covers_every_symbol(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py"))

    decision = edit(bridge, lanes, "return 2", "return 4")

    assert decision["permissionDecision"] == "deny"


def test_renaming_a_reserved_symbol_is_refused(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    decision = edit(bridge, lanes, "def claim(", "def take(")

    assert decision["permissionDecision"] == "deny"
    assert "Store.claim" in decision["permissionDecisionReason"]


def test_a_module_level_change_touches_the_whole_file(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    decision = edit(bridge, lanes, "import os", "import sys")

    assert decision["permissionDecision"] == "deny"


def test_a_file_that_does_not_parse_falls_back_and_says_so(bridge, lanes):
    path = lanes["lane"] / "src" / "store.py"
    path.write_text(MODULE + "def broken(:\n")
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    decision = edit(bridge, lanes, "return 1", "return 3")

    assert decision.get("permissionDecision") != "deny"
    context = decision["additionalContext"]
    assert "does not parse" in context
    assert "claude reserves src/store.py::Store.claim" in context


def test_adding_a_method_beside_a_reserved_one_is_allowed(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    decision = edit(
        bridge,
        lanes,
        "return 1\n",
        "return 1\n\n    def take(self):\n        return 5\n",
    )

    assert decision.get("permissionDecision") != "deny"
    assert "Store.claim" not in decision.get("additionalContext", "")


def test_an_edit_that_breaks_the_parse_falls_back_and_says_so(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    decision = edit(bridge, lanes, "return 2", "return (")

    assert decision.get("permissionDecision") != "deny"
    context = decision["additionalContext"]
    assert "does not parse" in context
    assert "compared by path" in context


def test_status_prints_the_reserved_symbol(bridge, lanes, capsys):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    bridge.status(cli.Selection(participant="claude"))

    output = capsys.readouterr().out
    assert "Symbols reserved: src/store.py::Store.claim" in output
    assert "Named resources held" not in output


def test_top_prints_the_reserved_symbol(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim"))

    view = dashboard.select(dashboard.collect(bridge.home, False, {}))
    lines = dashboard.layout(view)["lines"]

    claude = [line for line in lines if line.startswith("claude")]
    assert claude
    assert "src/store.py::Store.claim" in claude[0]


def test_a_resource_with_a_double_colon_is_not_a_symbol(bridge, lanes):
    granted(reserve(lanes, "claude", "host:db::5432", "a.py::f"))

    mail = checkpoints.mailbox(bridge.home, lanes["root"], "claude")

    assert mail["named_resources"] == ["host:db::5432"]
    assert mail["reserved_symbols"] == ["a.py::f"]


def test_status_lists_the_reserved_symbol(bridge, lanes):
    granted(reserve(lanes, "claude", "src/store.py::Store.claim", "a.py::f"))

    mail = checkpoints.mailbox(bridge.home, lanes["root"], "claude")

    assert sorted(mail["reserved_symbols"]) == [
        "a.py::f",
        "src/store.py::Store.claim",
    ]
    assert mail["named_resources"] == []


def test_a_malformed_symbol_key_is_refused(bridge, lanes):
    refused = reserve(lanes, "claude", "src/store.py::not a name")

    assert refused["isError"]
    assert "names no Python symbol" in refused["content"][0]["text"]


@pytest.mark.parametrize(
    ("pattern", "other", "expected"),
    [
        ("a.py::A.f", "a.py::A.g", False),
        ("a.py::A", "a.py::A.g", True),
        ("a.py", "a.py::A.g", True),
        ("src/*.py", "src/a.py::f", True),
        ("b.py::f", "a.py::f", False),
    ],
)
def test_symbol_keys_overlap_by_path_and_nesting(pattern, other, expected):
    assert store.overlapping(pattern, other) is expected
    assert store.overlapping(other, pattern) is expected
    assert not store.named_resource(other)


def test_changed_definitions_are_named_innermost_first():
    assert symbols.touched(MODULE, MODULE.replace("return 2", "x")) == [
        "Store.release"
    ]
    assert symbols.touched(MODULE, MODULE + "def broken(:\n") is None


def test_blank_and_comment_lines_are_not_charged_beside_code():
    added = MODULE.replace(
        "        return 1\n",
        "        return 1\n\n    # new\n    def take(self):\n"
        "        return 5\n",
    )
    assert symbols.touched(MODULE, added) == ["Store.take"]
    spaced = MODULE.replace("        return 1\n", "        return 1\n\n")
    assert symbols.touched(MODULE, spaced) == ["Store"]


def test_a_named_resource_with_a_double_colon_stays_a_resource():
    assert store.named_resource("host:db::5432")
    assert not store.named_resource("src/store.py::Store.claim")
