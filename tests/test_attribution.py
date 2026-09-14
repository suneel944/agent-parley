"""Checks neutral lane branches and the refusal of published attribution."""

import json
from pathlib import Path

import pytest

from agent_parley import cli, policy, roster
from agent_parley.checkpoints import attributed_command, checkpoint
from agent_parley.state import BridgeError, write_json

CREDIT = "Generated" + " with " + "Claude"
TRAILER = "Co-Authored-By:" + " Codex <bot@example.com>"


def commit(repo, message):
    """Commits one change in a checkout using the given message."""
    (repo / "shared.txt").write_text(message + "\n")
    cli.git(repo, "add", "shared.txt")
    cli.git(
        repo,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )


def payload(lane, command):
    """Builds a native tool payload running one shell command in a lane."""
    return {
        "hook_event_name": "PreToolUse",
        "cwd": str(lane),
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def test_a_lane_branch_carries_no_participant_or_provider_name(
    bridge, repo, paired
):
    _, directory = bridge.project(repo)
    for name, branch in paired["branches"].items():
        assert name not in branch
        assert branch.startswith(f"parley/{directory.name}/lane-")
    assert sorted(paired["branches"].values()) == [
        f"parley/{directory.name}/lane-1",
        f"parley/{directory.name}/lane-2",
    ]


def test_an_existing_branch_only_moves_the_next_lane_ordinal(bridge, repo):
    bridge.setup(repo)
    _, directory = bridge.project(repo)
    cli.git(repo, "branch", f"parley/{directory.name}/lane-1")
    manifest = bridge.add_participant(repo, "claude", "claude")
    assert manifest["branches"]["claude"] == f"parley/{directory.name}/lane-2"
    assert cli.has_branch(repo, f"parley/{directory.name}/lane-1")


def test_the_manifest_records_which_scheme_each_lane_uses(bridge, repo):
    bridge.setup(repo)
    _, directory = bridge.project(repo)
    bridge.add_participant(repo, "claude", "claude")
    stored = json.loads((directory / "project.json").read_text())
    assert stored["participants"]["claude"]["scheme"] == "lane"
    legacy = dict(stored)
    legacy["participants"] = {
        "codex": {
            "provider": "codex",
            "display": "codex",
            "lane": str(directory / "codex"),
            "branch": f"parley/{directory.name}/codex",
            "credential": None,
        }
    }
    write_json(directory / "project.json", legacy)
    assert (
        roster.read(directory)["participants"]["codex"]["scheme"]
        == "participant"
    )


def test_the_branch_prefix_is_configurable_per_project(bridge, repo):
    bridge.setup(repo)
    _, directory = bridge.project(repo)
    assert "parley/" in bridge.branch_naming(repo)
    bridge.branch_naming(repo, "work")
    manifest = bridge.add_participant(repo, "claude", "claude")
    assert manifest["branches"]["claude"] == f"work/{directory.name}/lane-1"
    with pytest.raises(BridgeError, match="lane branch prefix"):
        bridge.branch_naming(repo, "Bad Prefix/")


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m " + json.dumps(CREDIT),
        "git commit --amend --message=" + json.dumps(CREDIT),
        "git commit -m 'fix: repair the parser' -m " + json.dumps(TRAILER),
        "git merge --no-ff -m " + json.dumps(CREDIT) + " other",
        "git tag -a v1 -m " + json.dumps(CREDIT),
        "gh pr create --title " + json.dumps(CREDIT) + " --body ok",
    ],
)
def test_a_command_publishing_attribution_is_denied(
    bridge, repo, paired, command
):
    lane = Path(paired["lanes"]["claude"])
    directory = lane.parent
    write_json(directory / "claude-identity.json", {"name": "claude"})
    output = checkpoint(
        bridge.home, directory, "claude", payload(lane, command)
    )
    details = output["hookSpecificOutput"]
    assert details["permissionDecision"] == "deny"
    assert (
        "attributes the work to an assistant"
        in (details["permissionDecisionReason"])
    )
    records = [
        json.loads(line)
        for line in (directory / "claude-events.jsonl").read_text().splitlines()
    ]
    assert records[-1]["reason_class"] == "attribution_refused"
    assert records[-1]["decision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'fix: repair the codex adapter parser'",
        "git status",
        "git commit -m 'docs: explain how claude sessions are launched'",
        "gh pr view 12",
    ],
)
def test_ordinary_work_is_not_refused(bridge, repo, paired, command):
    lane = Path(paired["lanes"]["claude"])
    assert attributed_command(payload(lane, command), lane) is None


def test_a_command_in_another_checkout_is_not_this_lane_s_to_refuse(
    bridge, repo, paired, tmp_path
):
    lane = Path(paired["lanes"]["claude"])
    outside = payload(lane, "git -C /elsewhere commit -m " + json.dumps(CREDIT))
    assert attributed_command(outside, lane) is None


def test_merge_refuses_an_attributed_commit_and_names_it(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    before = cli.git(repo, "rev-parse", "HEAD")
    commit(lane, "feat: add a parser\n\n" + TRAILER)
    with pytest.raises(BridgeError, match="attributes the work"):
        bridge.merge(repo, "claude")
    assert cli.git(repo, "rev-parse", "HEAD") == before
    preview = bridge.preview_merge(repo, "claude")
    assert "attributes the work to an assistant" in preview


def test_merge_accepts_a_clean_history(bridge, repo, paired):
    lane = Path(paired["lanes"]["claude"])
    cli.git(repo, "config", "user.email", "test@example.com")
    cli.git(repo, "config", "user.name", "Bridge Test")
    commit(lane, "feat: add a parser")
    message = bridge.merge(repo, "claude")
    assert "Merged" in message
    subject = cli.git(repo, "log", "--format=%s", "-1")
    assert "claude" not in subject
    assert subject.startswith("Merge lane branch parley/")


def test_the_issue_comment_names_no_participant_or_provider():
    body = cli.report_comment("Engine built", "12 tests passed")
    assert "Reported ready for review." in body
    assert "claude" not in body and "codex" not in body
    assert not policy.has_attribution(body)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (CREDIT, "assistant_credit"),
        (TRAILER, "assistant_trailer"),
        ("Author:" + " Claude <a@example.com>", "assistant_authorship"),
        (
            "This PR was" + " generated with Release Please.",
            "generated_pull_request",
        ),
        (chr(0x1F916) + " shipped", "robot_signature"),
    ],
)
def test_each_refusal_names_its_rule(text, rule):
    assert policy.matched_rule(text) == rule
    assert rule in policy.refusal("This commit message", rule)


@pytest.mark.parametrize(
    "text",
    [
        "fix: repair the codex adapter",
        "Co-Authored-By: Example Maintainer <human@example.com>",
        "Run Claude Code and Codex in separate worktrees.",
    ],
)
def test_ordinary_text_breaks_no_rule(text):
    assert policy.matched_rule(text) is None
