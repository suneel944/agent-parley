"""Exercises the plugin validation gate and the local skill checks."""

import json
import subprocess

import pytest

from scripts import check_policy


def plugin(root, skills=(("coordinate", "name: coordinate"),)):
    directory = root / "plugins/agent-parley"
    (directory / ".claude-plugin").mkdir(parents=True)
    (directory / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "agent-parley", "version": "0.0.1"})
    )
    for name, declared in skills:
        skill = directory / "skills" / name
        skill.mkdir(parents=True)
        if declared is not None:
            skill.joinpath("SKILL.md").write_text(
                f"---\n{declared}\ndescription: A described skill.\n---\n"
            )
    return root


def validator(monkeypatch, report, returncode=0, stderr=""):
    monkeypatch.setattr(
        check_policy.shutil, "which", lambda name: "/usr/bin/claude"
    )

    def run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, returncode, stdout=report, stderr=stderr
        )

    monkeypatch.setattr(check_policy.subprocess, "run", run)


def report(errors=(), warnings=(), contents=()):
    return json.dumps(
        {
            "success": not errors,
            "manifest": {
                "file": "plugin.json",
                "errors": list(errors),
                "warnings": list(warnings),
            },
            "contents": list(contents),
        }
    )


def test_a_clean_plugin_reports_nothing(tmp_path, monkeypatch):
    validator(monkeypatch, report())
    assert check_policy.plugin_validation_errors(plugin(tmp_path)) == []


def test_the_protocol_warning_is_tolerated(tmp_path, monkeypatch):
    validator(
        monkeypatch,
        report(warnings=[{"path": "protocol", "message": "Unknown field"}]),
    )
    assert check_policy.plugin_validation_errors(plugin(tmp_path)) == []


def test_a_second_unknown_field_still_fails(tmp_path, monkeypatch):
    validator(
        monkeypatch,
        report(
            warnings=[
                {"path": "protocol", "message": "Unknown field"},
                {"path": "invented", "message": "Unknown field 'invented'"},
            ]
        ),
    )
    errors = check_policy.plugin_validation_errors(plugin(tmp_path))
    assert errors == ["plugin.json: Unknown field 'invented'"]


def test_an_unresolvable_declared_path_fails(tmp_path, monkeypatch):
    validator(
        monkeypatch,
        report(errors=[{"path": "commands", "message": "Path not found: x"}]),
    )
    errors = check_policy.plugin_validation_errors(plugin(tmp_path))
    assert errors == ["plugin.json: Path not found: x"]


def test_a_failing_component_fails(tmp_path, monkeypatch):
    validator(
        monkeypatch,
        report(
            contents=[
                {
                    "file": "commands/broken.md",
                    "errors": [{"path": None, "message": "Invalid input"}],
                    "warnings": [],
                }
            ]
        ),
    )
    errors = check_policy.plugin_validation_errors(plugin(tmp_path))
    assert errors == ["commands/broken.md: Invalid input"]


def test_a_validation_run_that_fails_is_named_separately(tmp_path, monkeypatch):
    validator(monkeypatch, "", returncode=2, stderr="unreadable target")
    errors = check_policy.plugin_validation_errors(plugin(tmp_path))
    assert errors == ["Plugin validation did not complete: unreadable target"]


def test_an_unreadable_report_fails(tmp_path, monkeypatch):
    validator(monkeypatch, "not json")
    errors = check_policy.plugin_validation_errors(plugin(tmp_path))
    assert errors == ["Plugin validation returned no readable report"]


def test_a_missing_client_skips_without_passing_silently(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(check_policy.shutil, "which", lambda name: None)
    assert check_policy.plugin_validation_errors(plugin(tmp_path)) == []
    assert "plugin validation skipped" in capsys.readouterr().out


def test_a_skill_without_a_document_fails(tmp_path, monkeypatch):
    validator(monkeypatch, report())
    root = plugin(tmp_path, skills=[("coordinate", None)])
    errors = check_policy.plugin_validation_errors(root)
    assert errors == ["coordinate: skill has no readable SKILL.md"]


def test_a_skill_without_a_name_fails(tmp_path, monkeypatch):
    validator(monkeypatch, report())
    root = plugin(tmp_path, skills=[("coordinate", "title: coordinate")])
    errors = check_policy.plugin_validation_errors(root)
    assert errors == ["coordinate: skill declares no name"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("---\nname: a\n---\nBody\n", {"name": "a"}),
        ("Body only\n", {}),
        ("---\nname: a\n  nested: b\n---\n", {"name": "a"}),
    ],
)
def test_frontmatter_reads_top_level_fields(text, expected):
    assert check_policy.frontmatter(text) == expected


def test_the_shipped_plugin_declares_every_skill():
    root = check_policy.Path(check_policy.__file__).resolve().parents[1]
    assert check_policy.skill_errors(root / "plugins/agent-parley") == []
