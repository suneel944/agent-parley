"""Checks the hash-chained audit bundle: export, verify, tamper, sign."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_parley import audit, cli, issues, lifecycle, reviews, supervision
from agent_parley.cli import git
from agent_parley.state import BridgeError, write_json

SECRET = "ghp_" + "x" * 36


def commit(lane, name):
    """Creates one identified commit in a lane worktree."""
    (lane / name).write_text("work\n")
    git(lane, "add", name)
    git(
        lane,
        "-c",
        "user.name=Bridge Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        f"Add {name}",
    )
    return git(lane, "rev-parse", "HEAD")


def cli_run(monkeypatch, capsys, *arguments):
    """Runs one CLI invocation and returns its status and output."""
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments])
    status = cli.main()
    captured = capsys.readouterr()
    return status, captured.out + captured.err


@pytest.fixture
def worked(bridge, repo, paired):
    """Records one claim, handoff, ready report, resolution and round."""
    directory = bridge.project(repo)[1]
    claude = Path(paired["lanes"]["claude"])
    codex = Path(paired["lanes"]["codex"])
    sha = commit(claude, "feature.txt")
    bridge.issue(claude, "claim", "42")
    bridge.report(claude, "ready", "Feature done", "", "make check passed")
    bridge.issue(codex, "claim", "43")
    offer = bridge.issue(codex, "offer", "43", to="claude", summary="tests")
    bridge.issue(claude, "accept", "43", offer_id=offer["offer"]["id"])
    ledger = issues.snapshot(directory)
    record = ledger["issues"]["42"]
    lifecycle.append_history(
        record,
        {
            "action": "resolve",
            "actor": "operator",
            "at": record["history"][-1]["at"],
            "outcome": "complete",
            "claim_id": record.get("claim_id"),
            "registration_token": SECRET,
            "handoff_prompt": "the whole prompt transcript",
            "usage": {"tokens": 1200, "api_key": SECRET},
        },
    )
    write_json(directory / "issues.json", ledger)
    reviews.request(
        directory,
        builder="claude",
        issue="42",
        claim=str(record.get("claim_id")),
        branch="feature",
        paths=["feature.txt"],
        candidates=["codex"],
        providers={"claude": "claude", "codex": "codex"},
    )
    write_json(
        directory / supervision.PULL_REQUEST_RECORD,
        {
            "read_at": 0,
            "pull_requests": {
                "7": {
                    "number": 7,
                    "lane": "claude",
                    "url": "https://example.com/pull/7",
                    "branch": "feature",
                    "sha": sha,
                    "checks": "green",
                    "issues": ["42"],
                    "opened_at": record["history"][-1]["at"],
                }
            },
            "history": [],
        },
    )
    return {"directory": directory, "sha": sha}


def export(monkeypatch, capsys, bridge, repo, output, *extra):
    """Exports the whole recorded window to one bundle file."""
    status, printed = cli_run(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "audit",
        "export",
        "--repo",
        str(repo),
        "--since",
        "2000-01-01T00:00:00Z",
        "--output",
        str(output),
        *extra,
    )
    assert status == 0, printed
    return json.loads(output.read_text()), printed


def verify(monkeypatch, capsys, bundle, *extra):
    """Verifies one bundle file through the command line."""
    return cli_run(monkeypatch, capsys, "audit", "verify", str(bundle), *extra)


def test_export_writes_one_chained_bundle_of_the_window(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    bundle, printed = export(monkeypatch, capsys, bridge, repo, output)
    assert "unsigned" in printed and str(output) in printed
    kinds = audit.counts(bundle)
    assert kinds["lane"] == 2
    assert kinds["resolved"] == 1
    assert kinds["claim"] >= 2 and kinds["handoff"] >= 2
    assert kinds["report"] == 1 and kinds["review"] == 1
    assert kinds["pull_request"] == 1 and kinds["commit"] == 1
    records = bundle["records"]
    lanes = [record["data"] for record in records if record["kind"] == "lane"]
    assert {lane["name"]: lane["provider"] for lane in lanes} == {
        "claude": "claude",
        "codex": "codex",
    }
    assert all("model" in lane for lane in lanes)
    report = next(r["data"] for r in records if r["kind"] == "report")
    assert report["evidence"] and report["lane"] == "claude"
    committed = next(r["data"] for r in records if r["kind"] == "commit")
    assert committed["sha"] == worked["sha"]
    assert committed["lane"] == "claude"
    assert records[0]["previous"] == audit.GENESIS
    for before, after in zip(records, records[1:], strict=False):
        assert after["previous"] == before["hash"]
    assert bundle["head"] == records[-1]["hash"]
    status, printed = verify(monkeypatch, capsys, output)
    assert status == 0
    assert f"Chain intact: {len(records)} records" in printed
    assert "No signature" in printed


def test_export_reads_state_only_and_writes_only_the_named_file(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    directory = worked["directory"]
    before = {
        path: path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and path.suffix != ".lock"
    }
    export(monkeypatch, capsys, bridge, repo, tmp_path / "audit.json")
    after = {
        path: path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and path.suffix != ".lock"
    }
    assert after == before


def test_bundle_carries_no_credential_token_or_transcript(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    bundle, _ = export(monkeypatch, capsys, bridge, repo, output)
    text = output.read_text()
    assert SECRET not in text
    assert "transcript" not in text
    for forbidden in (
        "registration_token",
        "token_digest",
        "handoff_prompt",
        "api_key",
        "credential",
    ):
        assert f'"{forbidden}"' not in text
    resolved = next(
        r["data"] for r in bundle["records"] if r["kind"] == "resolved"
    )
    assert resolved["usage"] == {"tokens": 1200}


def test_scrub_drops_secret_fields_and_withholds_attribution():
    credit = "Generated" + " with " + "Claude"
    cleaned = audit.scrub(
        {
            "summary": credit,
            "nested": [{"access_token": SECRET, "tokens": 3}],
            "password": "hunter2",
            "Authorization": "Bearer abc",
            "kept": "fine",
        }
    )
    assert cleaned == {
        "summary": audit.WITHHELD,
        "nested": [{"tokens": 3}],
        "kept": "fine",
    }


def test_scrub_drops_key_only_as_a_whole_name_part():
    cleaned = audit.scrub(
        {
            "key": "k",
            "ssh_key": "k",
            "signing_key": "k",
            "api_key": "k",
            "apikey": "k",
            "private_keys": "k",
            "monkey": "kept",
            "keyboard": "kept",
            "turnkey": "kept",
            "key_count": "kept",
            "keys": 4,
        }
    )
    assert cleaned == {
        "monkey": "kept",
        "keyboard": "kept",
        "turnkey": "kept",
        "key_count": "kept",
        "keys": 4,
    }


def test_a_lone_surrogate_exports_and_verifies(tmp_path):
    records = audit.chain([("report", {"summary": "half \ud800 pair"})])
    path = tmp_path / "audit.json"
    path.write_text(
        json.dumps(
            {
                "kind": audit.KIND,
                "records": records,
                "head": records[-1]["hash"],
            }
        )
    )
    assert audit.verify(path)["head"] == records[-1]["hash"]


def test_bundle_states_each_retention_horizon(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    bundle, _ = export(monkeypatch, capsys, bridge, repo, tmp_path / "a.json")
    horizons = bundle["horizons"]
    assert horizons["issue_history"]["limit"] == lifecycle.MAX_HISTORY
    assert horizons["issue_history"]["exceeded"] is False
    assert horizons["report_records"]["exceeded"] is False
    assert horizons["pull_request_history"]["exceeded"] is True
    directory = worked["directory"]
    ledger = issues.snapshot(directory)
    record = ledger["issues"]["42"]
    for count in range(lifecycle.MAX_HISTORY):
        lifecycle.append_history(
            record, {"action": "note", "at": 10**9 + count}
        )
    write_json(directory / "issues.json", ledger)
    manifest = {"participants": {}}
    stated = audit.horizons(directory, manifest, 10**9 - 1, 10**9)
    assert stated["issue_history"]["exceeded"] is True
    assert stated["pull_request_history"]["exceeded"] is False
    later = audit.horizons(directory, manifest, 2 * 10**9, 2 * 10**9)
    assert later["issue_history"]["exceeded"] is False


def test_verify_names_the_first_tampered_record_and_exits_nonzero(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    bundle, _ = export(monkeypatch, capsys, bridge, repo, output)
    bundle["records"][3]["data"]["tampered"] = True
    output.write_text(json.dumps(bundle))
    status, printed = verify(monkeypatch, capsys, output)
    assert status == 1
    assert "Record 3 breaks the chain" in printed


def test_verify_names_a_record_removed_from_the_middle(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    bundle, _ = export(monkeypatch, capsys, bridge, repo, output)
    del bundle["records"][2]
    output.write_text(json.dumps(bundle))
    status, printed = verify(monkeypatch, capsys, output)
    assert status == 1
    assert "Record 2" in printed


def test_unsigned_bundle_fails_only_when_a_signature_is_required(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    export(monkeypatch, capsys, bridge, repo, output)
    assert verify(monkeypatch, capsys, output)[0] == 0
    status, printed = verify(monkeypatch, capsys, output, "--require-signature")
    assert status == 1
    assert "carries no signature" in printed


def test_a_window_bounded_by_a_commit_excludes_later_records(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    bundle, _ = export(
        monkeypatch, capsys, bridge, repo, output, "--until", "2000-01-02"
    )
    assert set(audit.counts(bundle)) == {"lane"}
    first = git(repo, "rev-list", "--max-parents=0", "HEAD")
    assert audit.commit_time(repo, first) > 0
    with pytest.raises(BridgeError, match="not an instant"):
        audit.commit_time(repo, "no-such-ref")


needs_ssh = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed"
)


def keypair(tmp_path):
    """Generates a throwaway passphrase-less Ed25519 key."""
    key = tmp_path / "audit-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


@needs_ssh
def test_signed_bundle_verifies_and_names_the_signing_key(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    key = keypair(tmp_path)
    output = tmp_path / "audit.json"
    bundle, printed = export(
        monkeypatch, capsys, bridge, repo, output, "--sign", str(key)
    )
    assert "signed" in printed and "unsigned" not in printed
    assert "BEGIN SSH SIGNATURE" in bundle["signature"]
    fingerprint = subprocess.run(
        ["ssh-keygen", "-l", "-f", f"{key}.pub"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[1]
    status, printed = verify(monkeypatch, capsys, output, "--require-signature")
    assert status == 0, printed
    assert fingerprint in printed


@needs_ssh
def test_signature_fails_when_a_signed_bundle_is_edited(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    key = keypair(tmp_path)
    output = tmp_path / "audit.json"
    bundle, _ = export(
        monkeypatch, capsys, bridge, repo, output, "--sign", str(key)
    )
    bundle["until"]["ref"] = "edited"
    output.write_text(json.dumps(bundle))
    status, printed = verify(monkeypatch, capsys, output)
    assert status == 1
    assert "signature does not match" in printed


@needs_ssh
def test_signer_accepts_its_own_signature_and_rejects_another_key(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    key = keypair(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    stranger = keypair(other)
    output = tmp_path / "audit.json"
    export(monkeypatch, capsys, bridge, repo, output, "--sign", str(key))
    status, printed = verify(
        monkeypatch, capsys, output, "--signer", f"{key}.pub"
    )
    assert status == 0, printed
    assert "Chain intact" in printed
    status, printed = verify(
        monkeypatch, capsys, output, "--signer", f"{stranger}.pub"
    )
    assert status == 1
    assert "signature does not match" in printed


def test_signer_fails_an_unsigned_bundle(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    export(monkeypatch, capsys, bridge, repo, output)
    status, printed = verify(
        monkeypatch, capsys, output, "--signer", str(tmp_path / "x.pub")
    )
    assert status == 1
    assert "carries no signature" in printed


@needs_ssh
def test_signing_with_a_missing_key_writes_nothing(
    bridge, repo, paired, worked, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "audit.json"
    status, printed = cli_run(
        monkeypatch,
        capsys,
        "--home",
        str(bridge.home),
        "audit",
        "export",
        "--repo",
        str(repo),
        "--since",
        "7d",
        "--output",
        str(output),
        "--sign",
        str(tmp_path / "missing-key"),
    )
    assert status == 1
    assert "could not sign" in printed
    assert not output.exists()
