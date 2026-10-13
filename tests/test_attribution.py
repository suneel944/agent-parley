"""Checks neutral lane branches and the refusal of published attribution."""

import json
from pathlib import Path

import pytest

from agent_parley import cli, policy, roster
from agent_parley.checkpoints import attributed_command, checkpoint
from agent_parley.state import BridgeError, write_json

CREDIT = "Generated" + " with " + "Claude"
TRAILER = "Co-Authored-By:" + " Codex <bot@example.com>"
FOOTER = (
    "Generated" + " with [Claude Code](https://claude" + ".com/claude-code)"
)


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
        "git commit -m fix --trailer " + json.dumps(TRAILER),
        "git commit -m fix --trailer=" + json.dumps(TRAILER),
        "git merge --no-ff -m " + json.dumps(CREDIT) + " other",
        "git tag -a v1 -m " + json.dumps(CREDIT),
        "gh pr create --title " + json.dumps(CREDIT) + " --body ok",
        "gh pr edit 1 --body " + json.dumps(CREDIT),
        "gh pr comment 1 --body " + json.dumps(CREDIT),
        "gh pr merge 1 --squash --body " + json.dumps(CREDIT),
        "gh pr review 1 --approve -b " + json.dumps(CREDIT),
        "gh issue create -t t -b " + json.dumps(CREDIT),
        "gh issue edit 1 --body " + json.dumps(CREDIT),
        "gh issue comment 1 -b " + json.dumps(CREDIT),
        "gh release create v1 --notes " + json.dumps(CREDIT),
        "gh release edit v1 -n " + json.dumps(CREDIT),
        "git notes add -m " + json.dumps(CREDIT),
        "git commit -F - <<'M'\nfix: x\n\n" + TRAILER + "\nM",
        "gh pr create -t fix --body-file - <<M\nsee " + CREDIT + "\nM",
        "git commit -m \"$(cat <<'EOF'\nfix: it's done\n\n"
        + CREDIT
        + '\nEOF\n)"',
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
        "gh issue comment 1 -b 'LGTM'",
        "git commit -F - <<'M'\nfix: x\nM",
        "git commit -F - <<'M' && git push\nfix: it's the codex adapter\nM",
        "gh release create v1 --notes 'Integrates with Claude Code hooks.'",
    ],
)
def test_ordinary_work_is_not_refused(bridge, repo, paired, command):
    lane = Path(paired["lanes"]["claude"])
    assert attributed_command(payload(lane, command), lane) is None


@pytest.mark.parametrize(
    "command",
    [
        "git commit -F {path}",
        "git commit --file={path}",
        "git merge --no-ff -F {path} other",
        "git tag -a v1 -F {path}",
        "gh pr create --title fix --body-file {path}",
        "gh pr create --title fix -F {path}",
    ],
)
def test_a_message_file_carrying_attribution_is_refused(
    bridge, repo, paired, tmp_path, command
):
    lane = Path(paired["lanes"]["claude"])
    message = tmp_path / "message.txt"
    message.write_text("fix: repair the parser\n\n" + TRAILER + "\n")
    found = attributed_command(
        payload(lane, command.format(path=message)), lane
    )
    assert found is not None and found[1] == "assistant_trailer"
    message.write_text("fix: repair the parser\n")
    assert (
        attributed_command(payload(lane, command.format(path=message)), lane)
        is None
    )


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
        ("Co-Authored-By:" + " Gemini <g@example.com>", "assistant_trailer"),
        ("Generated" + " with Amp", "assistant_credit"),
        (FOOTER, "assistant_credit"),
        (chr(0x1F916) + " " + FOOTER, "assistant_credit"),
        ("Written" + " by **Codex**", "assistant_credit"),
        ("Generated" + " with `Claude`", "assistant_credit"),
        ("See https://claude" + ".ai/code for details", "assistant_link"),
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
        "Signed-off-by: Champ Example <champ@example.com>",
        "The bridge integrates with Claude Code hooks.",
        "Built with care by the maintainers.",
    ],
)
def test_ordinary_text_breaks_no_rule(text):
    assert policy.matched_rule(text) is None


@pytest.mark.parametrize(
    "command",
    [
        "(git commit -m " + json.dumps(TRAILER) + ")",
        "env GIT_AUTHOR_NAME=x git commit -m " + json.dumps(TRAILER),
        "timeout 5 git commit -m " + json.dumps(TRAILER),
    ],
)
def test_a_wrapped_attributed_commit_is_refused(paired, command):
    lane = Path(paired["lanes"]["claude"])
    found = attributed_command(payload(lane, command), lane)
    assert found is not None
    assert found[0] == TRAILER
