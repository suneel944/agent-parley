"""Checks the state archive export, inspection and import round trip."""

import hashlib
import io
import json
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

from agent_parley import archive, roster, store
from agent_parley.cli import Bridge, main
from agent_parley.state import BridgeError, write_json


def registered(bridge: Bridge, repo: Path) -> Path:
    """Registers one lane with mail, a ledger and a token on disk."""
    bridge.add_participant(repo, "claude", "claude")
    root, directory = bridge.project(repo)
    store.initialize(bridge.home)
    identity = store.register(bridge.home, str(root), "claude")
    write_json(directory / "claude-identity.json", identity)
    write_json(
        directory / "claude-activity.json", {"activity": "idle", "ts": 1.0}
    )
    write_json(
        directory / "issues.json",
        {"revision": 1, "issues": {"7": {"holder": "claude"}}},
    )
    (directory / "claude-events.jsonl").write_text(
        json.dumps({"ts": 1.0, "event": "PreToolUse"}) + "\n"
    )
    (directory / "claude-mcp.json").write_text('{"token": "secret"}\n')
    attachments = directory / "attachments" / "42"
    attachments.mkdir(parents=True)
    (attachments / "plan.md").write_text("plan\n")
    with store.connect(bridge.home, write=True) as db:
        project = db.execute(
            "SELECT id FROM projects WHERE human_key=?", (str(root),)
        ).fetchone()[0]
        agent = db.execute(
            "SELECT id FROM agents WHERE project_id=? AND name='claude'",
            (project,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO messages(project_id,sender_id,subject,body_md) "
            "VALUES (?,?,'hello','body')",
            (project, agent),
        )
    return directory


def members(path: Path) -> dict[str, bytes]:
    """Reads every archive member into memory."""
    with tarfile.open(path, "r:*") as tar:
        return {
            member.name: (tar.extractfile(member) or io.BytesIO()).read()
            for member in tar.getmembers()
        }


def rewritten(source: Path, target: Path, **changes: tarfile.TarInfo) -> None:
    """Copies an archive, replacing or adding the named members."""
    with tarfile.open(source, "r:*") as tar, tarfile.open(target, "w") as out:
        for member in tar.getmembers():
            if member.name in changes:
                continue
            stream = tar.extractfile(member)
            out.addfile(member, stream)
        for info in changes.values():
            out.addfile(info, io.BytesIO(b"x" * info.size))


def test_export_show_and_import_round_trip(bridge, repo, tmp_path):
    directory = registered(bridge, repo)
    root = str(bridge.project(repo)[0])
    output = tmp_path / "state.tar.gz"
    manifest = archive.export(bridge.home, output)
    assert manifest["schema"] == store.SCHEMA_VERSION
    assert [entry["root"] for entry in manifest["projects"]] == [root]
    assert manifest["projects"][0]["issues"] == 1
    contents = members(output)
    assert f"projects/{directory.name}/attachments/42/plan.md" in contents
    assert not any(name.endswith("-mcp.json") for name in contents)
    assert not any(name.endswith("credentials.json") for name in contents)
    for name, data in contents.items():
        assert b"registration_token" not in data, name
        if name != archive.MANIFEST:
            assert manifest["files"][name] == hashlib.sha256(data).hexdigest()

    shown = archive.describe(archive.read_manifest(output))
    assert root in shown and "1 issues" in shown and "claude" in shown

    home = tmp_path / "restored"
    home.mkdir(mode=0o700)
    result = archive.import_archive(home, output)
    assert [entry["root"] for entry in result["projects"]] == [root]
    assert result["missing_lanes"] == []
    restored = home / "projects" / directory.name
    assert roster.read(restored)["participants"]["claude"]["branch"]
    assert json.loads((restored / "issues.json").read_text())["revision"] == 1
    assert (restored / "attachments" / "42" / "plan.md").read_text() == "plan\n"
    identity = json.loads((restored / "claude-identity.json").read_text())
    assert "registration_token" not in identity
    with store.connect(home) as db:
        assert db.execute("SELECT subject FROM messages").fetchone()[0] == (
            "hello"
        )
        assert db.execute("SELECT token_digest FROM agents").fetchone()[0] is (
            None
        )


def test_the_command_line_reports_missing_lanes(
    bridge, repo, tmp_path, capsys, monkeypatch
):
    directory = registered(bridge, repo)
    output = tmp_path / "state.tar.gz"
    argv = ["agent-parley", "--home", str(bridge.home), "state", "export"]
    monkeypatch.setattr(
        sys, "argv", argv + ["--output", str(output), "--project", str(repo)]
    )
    assert main() == 0
    assert "Exported 1 projects" in capsys.readouterr().out
    lane = Path(roster.read(directory)["participants"]["claude"]["lane"])
    (directory / "project.json").write_text(
        json.dumps(
            {
                **roster.read(directory),
                "participants": {
                    "claude": {
                        **roster.read(directory)["participants"]["claude"],
                        "lane": str(lane.with_name("gone")),
                    }
                },
            }
        )
    )
    moved = tmp_path / "moved.tar.gz"
    archive.export(bridge.home, moved)
    home = tmp_path / "restored"
    home.mkdir(mode=0o700)
    argv = ["agent-parley", "--home", str(home), "state"]
    monkeypatch.setattr(sys, "argv", argv + ["import", str(moved)])
    assert main() == 0
    out = capsys.readouterr().out
    assert "Lane missing for claude" in out and "gone" in out
    monkeypatch.setattr(sys, "argv", argv + ["show", str(moved)])
    assert main() == 0
    assert "claude" in capsys.readouterr().out


def test_a_hash_mismatch_is_refused(bridge, repo, tmp_path):
    registered(bridge, repo)
    output = tmp_path / "state.tar.gz"
    archive.export(bridge.home, output)
    altered = tmp_path / "altered.tar"
    info = tarfile.TarInfo("providers.json")
    info.size = 3
    rewritten(output, altered, **{"providers.json": info})
    with pytest.raises(BridgeError, match="disagree|digest"):
        archive.read_manifest(altered)
    with tarfile.open(output, "r:*") as tar:
        name = next(
            member.name
            for member in tar.getmembers()
            if member.name.endswith("issues.json")
        )
    info = tarfile.TarInfo(name)
    info.size = 3
    rewritten(output, altered.with_name("hash.tar"), **{name: info})
    with pytest.raises(BridgeError, match="digest"):
        archive.read_manifest(altered.with_name("hash.tar"))


def test_unsafe_members_are_refused(bridge, repo, tmp_path):
    registered(bridge, repo)
    output = tmp_path / "state.tar.gz"
    archive.export(bridge.home, output)
    traversal = tarfile.TarInfo("projects/../escape.json")
    traversal.size = 1
    unsafe = tmp_path / "traversal.tar"
    rewritten(output, unsafe, **{traversal.name: traversal})
    with pytest.raises(BridgeError, match="unsafe path"):
        archive.read_manifest(unsafe)
    absolute = tarfile.TarInfo("/etc/passwd")
    absolute.size = 1
    rewritten(output, unsafe, **{absolute.name: absolute})
    with pytest.raises(BridgeError, match="unsafe path"):
        archive.read_manifest(unsafe)
    link = tarfile.TarInfo("projects/link.json")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    rewritten(output, unsafe, **{link.name: link})
    with pytest.raises(BridgeError, match="regular files"):
        archive.read_manifest(unsafe)
    hard = tarfile.TarInfo("projects/hard.json")
    hard.type = tarfile.LNKTYPE
    hard.linkname = "manifest.json"
    rewritten(output, unsafe, **{hard.name: hard})
    with pytest.raises(BridgeError, match="regular files"):
        archive.read_manifest(unsafe)
    home = tmp_path / "restored"
    home.mkdir(mode=0o700)
    with pytest.raises(BridgeError):
        archive.import_archive(home, unsafe)
    assert not (home / store.DATABASE).exists()


def test_a_newer_schema_and_an_occupied_directory_are_refused(
    bridge, repo, tmp_path
):
    registered(bridge, repo)
    output = tmp_path / "state.tar.gz"
    archive.export(bridge.home, output)
    with pytest.raises(BridgeError, match="--merge"):
        archive.import_archive(bridge.home, output)
    contents = members(output)
    manifest = json.loads(contents[archive.MANIFEST])
    manifest["schema"] = store.SCHEMA_VERSION + 1
    newer = tmp_path / "newer.tar"
    with tarfile.open(newer, "w") as tar:
        for name, stored in contents.items():
            data = (
                json.dumps(manifest).encode()
                if name == archive.MANIFEST
                else stored
            )
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    home = tmp_path / "restored"
    home.mkdir(mode=0o700)
    with pytest.raises(BridgeError, match="newer bridge"):
        archive.import_archive(home, newer)
    assert not (home / store.DATABASE).exists()


def test_merge_adds_a_project_and_refuses_a_collision(bridge, repo, tmp_path):
    directory = registered(bridge, repo)
    root = str(bridge.project(repo)[0])
    output = tmp_path / "state.tar.gz"
    archive.export(bridge.home, output)
    home = tmp_path / "other"
    home.mkdir(mode=0o700)
    other = Bridge(home)
    other.setup(repo)
    with pytest.raises(BridgeError, match="already exists"):
        archive.import_archive(home, output, merge=True)
    assert not (home / store.DATABASE).exists()
    before = sorted(path.name for path in home.iterdir())

    fresh = tmp_path / "fresh"
    fresh.mkdir(mode=0o700)
    Bridge(fresh)
    store.initialize(fresh)
    store.register(fresh, "/elsewhere", "codex")
    (fresh / "projects" / "abcd").mkdir(parents=True)
    write_json(fresh / "projects" / "abcd" / "project.json", {"root": "/x"})
    result = archive.import_archive(fresh, output, merge=True)
    assert [entry["root"] for entry in result["projects"]] == [root]
    assert (fresh / "projects" / directory.name / "issues.json").exists()
    with store.connect(fresh) as db:
        keys = [
            row[0]
            for row in db.execute("SELECT human_key FROM projects ORDER BY id")
        ]
        assert keys == ["/elsewhere", root]
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    with pytest.raises(BridgeError, match="already exists"):
        archive.import_archive(fresh, output, merge=True)
    assert sorted(path.name for path in home.iterdir()) == before


def test_project_scopes_the_export(bridge, repo, tmp_path):
    directory = registered(bridge, repo)
    root = str(bridge.project(repo)[0])
    store.register(bridge.home, "/elsewhere", "codex")
    (bridge.home / "projects" / "ffff").mkdir()
    write_json(
        bridge.home / "projects" / "ffff" / "project.json",
        {"root": "/elsewhere", "base": "main", "participants": {}},
    )
    output = tmp_path / "one.tar.gz"
    manifest = archive.export(bridge.home, output, directory)
    assert [entry["root"] for entry in manifest["projects"]] == [root]
    assert not any(
        name.startswith("projects/ffff") for name in manifest["files"]
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / store.DATABASE).write_bytes(
        members(output)[archive.STORE_MEMBER]
    )
    with sqlite3.connect(snapshot / store.DATABASE) as db:
        assert [
            row[0] for row in db.execute("SELECT human_key FROM projects")
        ] == [root]
        assert db.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
    with pytest.raises(BridgeError, match="already exists"):
        archive.export(bridge.home, output, directory)
    with pytest.raises(BridgeError, match="no project rooted"):
        archive.import_archive(tmp_path / "empty", output, "/elsewhere")
