"""Checks the unattended acceptance run's seeding and its verdict."""

import contextlib
import json
import os
import subprocess
import sys
import time

import pytest

from agent_parley import metrics, server, state
from scripts import acceptance

LANES = ["claude:claude", "codex:codex"]
SHIM = """#!{executable}
import json
import sys

arguments = [item for item in sys.argv[1:] if item != "--json"]
command = arguments[2:]
if command[0] == "issue" and command[1] == "show":
    print(
        json.dumps(
            {{
                "history": {{
                    "records": [
                        {{
                            "kind": "claim",
                            "action": "claim",
                            "participant": "claude",
                            "detail": "claim #1",
                        }},
                        {{
                            "kind": "report",
                            "action": "ready",
                            "participant": "claude",
                            "detail": "reported ready",
                        }},
                    ]
                }}
            }}
        )
    )
elif command[0] == "problems":
    print(json.dumps({{"problems": []}}))
    sys.exit(1)
else:
    print(json.dumps({{}}))
"""
BLIND = """#!{executable}
import sys

sys.stderr.write("the service is not ready\\n")
sys.exit(1)
"""
BLOCKED = SHIM.replace(
    '"action": "ready",', '"action": "blocked", "report_id": "r1",'
)
HOLDING = SHIM.replace(
    'elif command[0] == "problems":',
    'elif command[0] == "status":\n'
    "    lane = {{\n"
    '        "participant": "claude",\n'
    '        "claims": [{{"issue": 1, "delivered": DELIVERED}}],\n'
    "    }}\n"
    "    print(\n"
    "        json.dumps(\n"
    '            {{"projects": [{{"root": "ROOT", "participants": [lane]}}]}}\n'
    "        )\n"
    "    )\n"
    'elif command[0] == "problems":',
)
NEIGHBOUR = SHIM.replace(
    '{{"problems": []}}',
    '{{"problems": [{{"project": "/elsewhere", "actor": "lane",'
    ' "command": "", "condition": "stalled"}}]}}',
)


def estate(tmp_path, lanes=LANES):
    """Builds a state home and a project the verdict can be read from."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    directory = home / "projects" / "one"
    directory.mkdir(parents=True)
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    (home / "server.log").write_text("ready\n")
    (directory / "project.json").write_text(json.dumps({"root": str(repo)}))
    for lane in lanes:
        name = lane.split(":")[0]
        (directory / f"{name}-wake.json").write_text(
            json.dumps({"attempts": 1, "result": "accepted"})
        )
        (directory / f"{name}-activity.json").write_text(
            json.dumps({"activity": "working", "session_pid": 4242})
        )
    return home, repo


def shim(tmp_path, template=SHIM):
    """Writes a coordination CLI that answers the verdict's readings."""
    path = tmp_path / "parley-shim"
    path.write_text(template.format(executable=sys.executable))
    path.chmod(0o755)
    return str(path)


def lane_frame(stalled, claims, stale_reservations, process_alive):
    """Builds one frame holding a single lane's status reading."""
    return {
        "at": 0.0,
        "status": {
            "participants": [
                {
                    "participant": "claude",
                    "idle": {"stalled": stalled},
                    "claims": [{"issue": number} for number in claims],
                    "mail": {"stale_reservations": stale_reservations},
                    "availability": {"process_alive": process_alive},
                }
            ]
        },
    }


def record(frames, samples):
    """Writes frames the way the run appends them."""
    frames.parent.mkdir(parents=True, exist_ok=True)
    frames.write_text("".join(json.dumps(sample) + "\n" for sample in samples))


def test_seeding_refuses_a_directory_that_holds_a_repository(tmp_path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    with pytest.raises(SystemExit):
        acceptance.workspace(tmp_path, 2)


def test_a_dirty_lane_worktree_with_no_claim_is_stranded(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [*git, "commit", "--allow-empty", "-m", "seed"], cwd=repo, check=True
    )
    for name in ("claude-5", "codex", "gemini", "issue-5"):
        subprocess.run(
            [*git, "worktree", "add", "-b", name, str(tmp_path / name)],
            cwd=repo,
            check=True,
        )
        (tmp_path / name / "left.txt").write_text("unfinished\n")
    lanes = ["claude-5:claude", "codex:codex", "gemini:gemini", "clean:claude"]
    subprocess.run(
        [*git, "worktree", "add", "-b", "clean", str(tmp_path / "clean")],
        cwd=repo,
        check=True,
    )
    stranded = acceptance._worktrees(repo, lanes, {"codex"})
    assert stranded == [str(tmp_path / "claude-5"), str(tmp_path / "gemini")]


def test_a_dirty_lane_worktree_fails_the_run(tmp_path):
    home, repo = estate(tmp_path)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(
        [*git, "commit", "--allow-empty", "-m", "seed"], cwd=repo, check=True
    )
    lane = tmp_path / "claude"
    subprocess.run(
        [*git, "worktree", "add", "-b", "claude", str(lane)],
        cwd=repo,
        check=True,
    )
    (lane / "left.txt").write_text("unfinished\n")
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    condition = decided["conditions"]["no worktree outlived its claim"]
    assert condition["passed"] is False
    assert condition["detail"] == [str(lane)]


@pytest.mark.parametrize(("delivered", "stranded"), [(True, 1), (False, 0)])
def test_only_an_undelivered_claim_keeps_a_dirty_worktree(
    tmp_path, delivered, stranded
):
    home, repo = estate(tmp_path)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(
        [*git, "commit", "--allow-empty", "-m", "seed"], cwd=repo, check=True
    )
    lane = tmp_path / "claude"
    subprocess.run(
        [*git, "worktree", "add", "-b", "claude", str(lane)],
        cwd=repo,
        check=True,
    )
    (lane / "left.txt").write_text("unfinished\n")
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    template = HOLDING.replace("ROOT", str(repo)).replace(
        "DELIVERED", str(delivered)
    )
    decided = acceptance.verdict(
        shim(tmp_path, template), home, repo, LANES, 1, frames
    )
    condition = decided["conditions"]["no worktree outlived its claim"]
    assert len(condition["detail"]) == stranded


def test_seeding_writes_one_task_per_backlog_issue(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-identity"))
    acceptance.workspace(tmp_path, 3)
    tasks = sorted(path.name for path in (tmp_path / "tasks").glob("*.md"))
    assert tasks == ["1.md", "2.md", "3.md"]
    assert "task_2" in (tmp_path / "tasks" / "2.md").read_text()
    committed = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "seed the acceptance backlog" in committed.stdout


def test_frames_survive_a_line_written_while_the_run_was_killed(tmp_path):
    frames = tmp_path / "frames.jsonl"
    frames.write_text(json.dumps({"at": 1.0}) + '\n{"at": 2.0')
    assert acceptance.samples(frames) == [{"at": 1.0}]


def test_an_idle_lane_holding_a_claim_fails_the_run(tmp_path):
    home, repo = estate(tmp_path)
    frames = repo / "acceptance" / "frames.jsonl"
    record(
        frames,
        [
            lane_frame(False, [1], 0, True),
            lane_frame(True, [1], 0, True),
        ],
    )
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    condition = decided["conditions"]["no lane idled on an open claim"]
    assert condition["passed"] is False
    assert condition["detail"] == ["claude#1"]
    assert decided["passed"] is False


def held(
    age, process_alive=True, delivered=False, idle_seconds=0, stalled=False
):
    """Builds a frame of a lane holding #1 with an empty inbox."""
    sample = lane_frame(stalled, [1], 0, process_alive)
    lane = sample["status"]["participants"][0]
    lane["idle_seconds"] = idle_seconds
    lane["claims"][0].update(last_event_seconds=age, delivered=delivered)
    return sample


def test_a_lane_idle_on_a_claim_with_no_mail_fails_the_run():
    limit = acceptance.IDLE_CLAIM_SECONDS
    taken = [held(0), held(limit / 2), held(limit + 1)]
    assert acceptance._idle_claims(taken) == ["claude#1"]


def test_a_claim_held_by_a_dead_process_fails_the_run():
    limit = acceptance.IDLE_CLAIM_SECONDS
    taken = [held(60, False), held(limit + 1, False)]
    assert acceptance._idle_claims(taken) == ["claude#1"]


def test_a_lane_woken_often_that_never_works_its_claim_fails_the_run():
    limit = acceptance.IDLE_CLAIM_SECONDS
    taken = [
        held(limit / 2, idle_seconds=240),
        held(limit / 2 + 300, idle_seconds=30),
        held(limit + 1, idle_seconds=20),
    ]
    assert acceptance._idle_claims(taken) == ["claude#1"]


def test_an_unknown_process_on_a_fresh_claim_passes():
    taken = [held(60, None), held(360, None), held(660, None)]
    assert acceptance._idle_claims(taken) == []


def test_a_delivered_claim_never_idles_its_lane():
    limit = acceptance.IDLE_CLAIM_SECONDS
    taken = [
        held(60, delivered=True, idle_seconds=0),
        held(10 * limit, delivered=True, idle_seconds=10 * limit),
        held(20 * limit, False, delivered=True, stalled=True),
    ]
    assert acceptance._idle_claims(taken) == []


def test_a_lane_reporting_on_its_claim_passes():
    limit = acceptance.IDLE_CLAIM_SECONDS
    taken = [held(limit - 60), held(30), held(limit - 1), held(0)]
    assert acceptance._idle_claims(taken) == []


def test_an_expired_lease_on_a_dead_lane_fails_the_run(tmp_path):
    home, repo = estate(tmp_path)
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 2, False)])
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    condition = decided["conditions"]["no lease outlived its holder"]
    assert condition["passed"] is False
    assert condition["detail"] == ["claude"]


def test_a_quiet_estate_passes_every_condition(tmp_path):
    home, repo = estate(tmp_path)
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [1], 0, True)])
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    assert decided["passed"] is True, decided["conditions"]
    assert decided["frames"] == 1
    text = acceptance.report(decided, repo, 24.0)
    assert "Result: PASS" in text
    assert "| claude | 1 | accepted | - | working |" in text


def test_a_claim_held_by_an_inactive_lane_is_unaccounted_time(tmp_path):
    home, repo = estate(tmp_path)
    first = lane_frame(False, [1], 0, True)
    second = lane_frame(False, [1], 0, True)
    second["at"] = 600.0
    measured = acceptance.measure(
        home, repo, LANES, {"1": {"state": ""}}, [first, second]
    )
    assert measured["unaccountable_claim_minutes"] == 10.0
    assert measured["claims"] == 1
    assert measured["claims_completed"] == 0


def test_a_broken_pipe_in_the_service_log_fails_the_run(tmp_path):
    home, repo = estate(tmp_path)
    (home / "server.log").write_text("BrokenPipeError: [Errno 32]\n")
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    assert decided["conditions"]["service log is clean"]["passed"] is False


def test_a_problems_view_that_cannot_be_read_fails_the_run(tmp_path):
    home, repo = estate(tmp_path)
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    decided = acceptance.verdict(
        shim(tmp_path, BLIND), home, repo, LANES, 1, frames
    )
    condition = decided["conditions"]["problems holds only operator rows"]
    assert condition["passed"] is False
    assert decided["conditions"]["every backlog issue reported"]["passed"] is (
        False
    )


def test_a_fault_older_than_the_run_is_not_counted(tmp_path):
    home, repo = estate(tmp_path)
    (home / "server.log").write_text("BrokenPipeError: [Errno 32]\nready\n")
    acceptance.mark(repo)
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    assert decided["conditions"]["service log is clean"]["passed"] is True


def test_a_fault_rotated_out_of_the_log_still_fails_the_run(
    tmp_path, monkeypatch
):
    home, repo = estate(tmp_path)
    (home / "server.log").write_text(
        "".join(
            f"2020-01-01T00:00:0{index}+0000 ready port {index}\n"
            for index in range(4)
        )
    )
    before = (home / "server.log").stat().st_size
    acceptance.mark(repo)
    trim = state.trim_log
    monkeypatch.setattr(
        server,
        "trim_log",
        lambda path: (
            trim(path, ceiling=160, records=2)
            if path.name == server.LOG_NAME
            else trim(path)
        ),
    )
    with (home / "server.log").open("a") as stream:
        with contextlib.redirect_stdout(stream):
            server.log(home, "failed", "BrokenPipeError: [Errno 32]")
            for index in range(3):
                server.log(home, "ready", f"port {index}")
    assert "BrokenPipeError" in (home / "server.log.1").read_text()
    assert "BrokenPipeError" not in (home / "server.log").read_text()
    assert (home / "server.log").stat().st_size < before
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    condition = decided["conditions"]["service log is clean"]
    assert condition["passed"] is False
    assert condition["detail"]["broken_pipe"] == 1


def test_a_log_trimmed_past_the_run_start_is_unreadable(tmp_path):
    home, repo = estate(tmp_path)
    acceptance.mark(repo)
    later = time.strftime(acceptance.STAMP, time.localtime(time.time() + 60))
    (home / "server.log.1").write_text(f"{later} ready port 1\n")
    (home / "server.log").write_text(f"{later} ready port 2\n")
    assert acceptance._log_faults(home, repo)["readable"] is False
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    decided = acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    assert decided["conditions"]["service log is clean"]["passed"] is False


def test_a_blocked_report_with_no_remaining_fails_the_run(tmp_path):
    home, repo = estate(tmp_path)
    directory = home / "projects" / "one"
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    metrics.record_report(
        directory,
        "claude",
        {"id": "r1", "kind": "report", "summary": "stuck", "remaining": ""},
    )
    decided = acceptance.verdict(
        shim(tmp_path, BLOCKED), home, repo, LANES, 1, frames
    )
    condition = decided["conditions"]["every backlog issue reported"]
    assert condition["passed"] is False
    assert decided["endings"]["1"]["state"] == "blocked"


def test_a_blocked_report_with_remaining_work_counts_as_reported(tmp_path):
    home, repo = estate(tmp_path)
    directory = home / "projects" / "one"
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    metrics.record_report(
        directory,
        "claude",
        {
            "id": "r1",
            "kind": "report",
            "summary": "stuck",
            "remaining": "needs a schema decision",
        },
    )
    decided = acceptance.verdict(
        shim(tmp_path, BLOCKED), home, repo, LANES, 1, frames
    )
    condition = decided["conditions"]["every backlog issue reported"]
    assert condition["passed"] is True
    assert decided["endings"]["1"]["reason"] == "needs a schema decision"


def test_another_project_cannot_fail_this_run(tmp_path):
    home, repo = estate(tmp_path)
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [1], 0, True)])
    decided = acceptance.verdict(
        shim(tmp_path, NEIGHBOUR), home, repo, LANES, 1, frames
    )
    condition = decided["conditions"]["problems holds only operator rows"]
    assert condition["passed"] is True
    assert condition["evidence"] == "0 rows, 0 unattended"


def test_trust_is_recorded_for_every_lane_directory(tmp_path, monkeypatch):
    home, repo = estate(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    (tmp_path / "operator" / ".codex").mkdir(parents=True)
    (tmp_path / "operator" / ".codex" / "config.toml").write_text(
        'approval_policy = "never"\n'
    )
    recorded = acceptance.trust(home, repo, LANES)
    written = json.loads((tmp_path / "operator" / ".claude.json").read_text())
    projects = written["projects"]
    lane = str(home / "projects" / "one" / "claude")
    assert projects[lane]["hasTrustDialogAccepted"] is True
    assert projects[str(repo)]["hasTrustDialogAccepted"] is True
    text = (tmp_path / "operator" / ".codex" / "config.toml").read_text()
    assert 'approval_policy = "never"' in text
    assert f'[projects."{repo}"]' in text
    assert 'trust_level = "trusted"' in text
    assert str(home / "projects" / "one" / "codex") in recorded


def test_a_lane_on_a_second_account_is_trusted_in_its_own_record(
    tmp_path, monkeypatch
):
    home, repo = estate(tmp_path, ["claude:claude", "second:claude:profile"])
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    account = tmp_path / "second-account"
    (home / "credentials.json").write_text(
        json.dumps({"entries": {"profile": {"home": str(account)}}})
    )
    acceptance.trust(home, repo, ["claude:claude", "second:claude:profile"])
    directory = home / "projects" / "one"
    written = json.loads((tmp_path / "operator" / ".claude.json").read_text())
    operator = written["projects"]
    second = json.loads((account / ".claude.json").read_text())["projects"]
    assert str(directory / "claude") in operator
    assert str(directory / "second") not in operator
    assert second[str(directory / "second")]["hasTrustDialogAccepted"] is True
    assert second[str(repo)]["hasTrustDialogAccepted"] is True


def test_only_the_opt_ins_and_hook_review_answer_are_recorded(
    tmp_path,
):
    home, repo = estate(tmp_path)
    manifest = home / "projects" / "one" / "project.json"
    acceptance.supervise(home, repo)
    data = json.loads(manifest.read_text())
    assert data["supervision"] == {
        "approve_bridge_tools": True,
        "auto_mode": True,
        "dialogs": {"hook-review": "Trust all and continue"},
    }
    assert data["root"] == str(repo)


def test_the_workspace_allows_edits_only_in_its_own_settings(tmp_path):
    acceptance.workspace(tmp_path / "run", 2)
    settings = json.loads(
        (tmp_path / "run" / ".claude" / "settings.json").read_text()
    )
    assert settings == {
        "permissions": {"allow": list(acceptance.PROJECT_PERMISSIONS)}
    }


def test_the_status_reading_of_another_project_is_ignored(tmp_path):
    document = {
        "projects": [
            {"root": "/somewhere/else", "participants": [1]},
            {"root": str(tmp_path), "participants": [2]},
        ]
    }
    assert acceptance._project(document, tmp_path)["participants"] == [2]


def test_the_run_never_writes_into_the_state_home(tmp_path):
    home, repo = estate(tmp_path)
    before = sorted(os.listdir(home))
    frames = repo / "acceptance" / "frames.jsonl"
    record(frames, [lane_frame(False, [], 0, True)])
    acceptance.verdict(shim(tmp_path), home, repo, LANES, 1, frames)
    assert sorted(os.listdir(home)) == before


def test_a_rehearsal_names_every_reading_the_service_did_not_answer(
    tmp_path,
):
    home, repo = estate(tmp_path)
    faults = acceptance.rehearse(shim(tmp_path, BLIND), home, repo, LANES)
    assert [fault.split(":")[0] for fault in faults] == [
        "up",
        "issues",
        "problems",
        "metrics",
        "status",
    ]


def test_a_rehearsal_plans_every_lane_and_starts_none(tmp_path):
    home, repo = estate(tmp_path)
    reading = json.dumps({"projects": [{"root": str(repo)}]})
    served = tmp_path / "served-shim"
    served.write_text(
        SHIM.format(executable=sys.executable).replace(
            "else:",
            f'elif command[0] == "status":\n    print({reading!r})\nelse:',
        )
    )
    served.chmod(0o755)
    cli = str(served)
    assert acceptance.rehearse(cli, home, repo, LANES) == []
    plan = json.loads((repo / "acceptance" / "plan.json").read_text())
    assert plan["codex"][:4] == [cli, "--home", str(home), "run"]
    assert sorted(plan) == ["claude", "codex"]
    assert not (repo / "acceptance" / "launch").exists()
