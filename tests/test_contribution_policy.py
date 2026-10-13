"""Exercises the contribution text policy without embedding credits."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_policy
from scripts.check_policy import (
    action_pin_errors,
    contribution_errors,
    duplicate_paragraph_errors,
    has_attribution,
    plugin_validation_errors,
    readme_link_errors,
)

ROOT = Path(__file__).resolve().parents[1]
CREDIT = "Co-Authored-By:" + " Codex <bot@example.com>"


@pytest.mark.parametrize(
    "text",
    [
        "Generated" + " with " + "Claude",
        "Co-Authored-By:" + " Codex <bot@example.com>",
        "This PR was" + " generated with Release Please.",
        chr(0x1F916) + " Created releases:",
    ],
)
def test_rejects_authorship_credits(text):
    assert has_attribution(text)


@pytest.mark.parametrize(
    "text",
    [
        "Run Claude Code and Codex in separate worktrees.",
        "The CLI uses the existing native authentication.",
        "Copyright 2026 Suneel Kaushik S",
        "Co-Authored-By: Example Maintainer <human@example.com>",
    ],
)
def test_preserves_product_documentation_and_required_notices(text):
    assert not has_attribution(text)


PARAGRAPH = (
    "Native Windows is not supported. The wheel installs there, but every "
    "command exits with status 2 and a line pointing to WSL2, because the "
    "runtime relies on POSIX primitives that native Windows does not "
    "provide."
)


def test_duplicate_paragraph_is_reported(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text(f"{PARAGRAPH}\n\n{PARAGRAPH}\n")
    errors = duplicate_paragraph_errors(tmp_path)
    assert len(errors) == 1
    assert "docs/x.md" in errors[0]
    assert "lines 1, 3" in errors[0]
    assert "Native Windows is not supported" in errors[0]


def test_repeated_fenced_code_block_is_not_reported(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    block = "```\n" + PARAGRAPH + "\n```\n"
    (docs / "x.md").write_text(f"{block}\n{block}")
    assert duplicate_paragraph_errors(tmp_path) == []


def test_clean_file_has_no_errors(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text(f"{PARAGRAPH}\n\nSomething else entirely.\n")
    assert duplicate_paragraph_errors(tmp_path) == []


def test_changelog_duplicates_are_ignored(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "CHANGELOG.md").write_text(f"{PARAGRAPH}\n\n{PARAGRAPH}\n")
    assert duplicate_paragraph_errors(tmp_path) == []


OLD_UV = "astral-sh/setup-uv@" + "b" * 40
NEW_UV = "astral-sh/setup-uv@" + "c" * 40


def write_workflows(root, **files):
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for name, uses in files.items():
        steps = "".join(f"      - uses: {pin}\n" for pin in uses)
        (workflows / f"{name}.yml").write_text(f"jobs:\n  x:\n{steps}")


def test_an_action_pinned_to_two_commits_is_reported(tmp_path):
    write_workflows(tmp_path, check=[NEW_UV, NEW_UV], release=[OLD_UV])
    errors = action_pin_errors(tmp_path)
    assert len(errors) == 1
    assert errors[0].startswith("astral-sh/setup-uv is pinned")
    assert ".github/workflows/release.yml" in errors[0]
    assert ".github/workflows/check.yml" in errors[0]


def test_one_commit_per_action_and_local_actions_pass(tmp_path):
    write_workflows(
        tmp_path, check=[NEW_UV, "./.github/actions/secret-scan"], pr=[NEW_UV]
    )
    assert action_pin_errors(tmp_path) == []


def test_the_repository_pins_each_action_once():
    assert action_pin_errors(ROOT) == []


def test_readme_links_must_be_absolute_and_resolve(tmp_path):
    (tmp_path / "README.md").write_text(
        '[a](docs/x.md) [b](#top) <a href="mailto:x@y.z">c</a>\n'
        f"[d]({check_policy.README_BLOB_PREFIX}docs/gone.md)\n"
        f"[e]({check_policy.README_BLOB_PREFIX}README.md#top)\n"
    )
    errors = readme_link_errors(tmp_path)
    assert len(errors) == 2
    assert errors[0].startswith("README.md:1: relative link 'docs/x.md'")
    assert errors[1] == "README.md:2: link target 'docs/gone.md' does not exist"


def test_credits_in_tracked_files_and_history_are_reported(tmp_path):
    assert contribution_errors(tmp_path) == []
    for command in (
        ["init", "-q"],
        ["config", "user.email", "ci@example.invalid"],
        ["config", "user.name", "ci"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(tmp_path), *command], check=True)
    (tmp_path / "notes.txt").write_text(CREDIT + "\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", f"x\n\n{CREDIT}"],
        check=True,
    )
    errors = contribution_errors(tmp_path)
    assert errors[0] == "notes.txt: prohibited attribution"
    assert errors[1].endswith(": prohibited credit")


def validator(monkeypatch, result):
    monkeypatch.setattr(check_policy.shutil, "which", lambda name: name)

    def run(command, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(check_policy.subprocess, "run", run)


def reply(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (OSError("gone"), "Plugin validation could not run: gone"),
        (reply(2, stderr="crash"), "Plugin validation did not complete: crash"),
        (reply(2), "Plugin validation did not complete: no detail reported"),
        (reply(1, "not json"), "Plugin validation returned no readable report"),
    ],
)
def test_a_validator_that_cannot_report_fails(monkeypatch, result, expected):
    validator(monkeypatch, result)
    assert plugin_validation_errors(ROOT) == [expected]


def test_validator_errors_and_untolerated_warnings_are_reported(monkeypatch):
    report = {
        "manifest": {"errors": [{"message": "bad name"}]},
        "contents": [
            {
                "file": "skills/x/SKILL.md",
                "warnings": [
                    {"path": "protocol", "message": "tolerated"},
                    {"path": "name", "message": "too long"},
                ],
            }
        ],
    }
    validator(monkeypatch, reply(1, json.dumps(report)))
    assert plugin_validation_errors(ROOT) == [
        "plugin: bad name",
        "skills/x/SKILL.md: too long",
    ]


def test_this_repository_passes_the_policy(monkeypatch, capsys):
    monkeypatch.setattr(check_policy.shutil, "which", lambda name: None)
    monkeypatch.setattr(check_policy.sys, "argv", ["check_policy.py"])
    check_policy.main()
    assert capsys.readouterr().out.startswith("Policy: plugin validation")


def test_a_commit_message_with_a_credit_fails_the_policy(monkeypatch, tmp_path):
    message = tmp_path / "message"
    message.write_text(CREDIT)
    monkeypatch.setattr(check_policy.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        check_policy.sys, "argv", ["check_policy.py", str(message)]
    )
    with pytest.raises(SystemExit, match="Commit message contains"):
        check_policy.main()
