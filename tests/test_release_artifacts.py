"""Checks release-note extraction and the published release asset set."""

import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts import codex_bundle, release_artifacts
from scripts.codex_bundle import build
from scripts.release_artifacts import checksums, release_notes
from scripts.release_publish import asset_names, verify_assets

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "heading",
    [
        "## [0.3.1] - 2026-09-05",
        "## [0.3.1](https://github.com/example/compare/v0.3.0...v0.3.1) "
        "(2026-09-05)",
    ],
)
def test_extracts_only_requested_version(heading):
    text = f"# Changelog\n\n{heading}\n\n### Fixes\n\n- Fixed.\n\n"
    text += "## [0.3.0] - 2026-09-04\n\nOlder.\n"
    assert release_notes(text, "0.3.1") == (
        "## What's Changed\n\n### Fixes\n\n- Fixed.\n"
    )
    assert release_notes(text, "0.3.0") == ("## What's Changed\n\nOlder.\n")


@pytest.mark.parametrize(
    "text",
    [
        "## [0.3.10] - 2026-09-05\nDifferent version.",
        "## [0.3.1] - 2026-09-05\n\n## [0.3.0] - 2026-09-04\nOlder.",
    ],
)
def test_rejects_missing_or_empty_release_notes(text):
    with pytest.raises(ValueError):
        release_notes(text, "0.3.1")


def test_full_changelog_follows_version_content_without_overview():
    text = (
        "# Changelog\n\nGeneric project overview.\n\n"
        "## [0.3.1] - 2026-09-05\n\n### Fixes\n\n- Fixed.\n"
    )
    assert release_notes(text, "0.3.1", "https://example.com/repo/") == (
        "## What's Changed\n\n### Fixes\n\n- Fixed.\n\n"
        "**Full Changelog**: https://example.com/repo/commits/v0.3.1\n"
    )


@pytest.fixture
def release_bundle(tmp_path):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    archive = build(ROOT, tmp_path)
    for name in asset_names(version) - {archive.name}:
        (tmp_path / name).write_text(f"Artifact: {name}\n")
    assets = [tmp_path / name for name in asset_names(version)]
    (tmp_path / "SHA256SUMS").write_text(checksums(assets))
    return tmp_path, version, archive


def test_codex_archive_ships_with_a_checksum_entry(release_bundle):
    directory, version, archive = release_bundle
    assert archive.name == f"agent-parley-{version}-codex-skills.zip"
    manifest = (directory / "SHA256SUMS").read_text().splitlines()
    assert sum(line.endswith(f"  {archive.name}") for line in manifest) == 1
    assert verify_assets(directory, version) != ""


def test_release_without_the_codex_archive_fails(release_bundle):
    directory, version, archive = release_bundle
    archive.unlink()
    with pytest.raises(ValueError):
        verify_assets(directory, version)


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_package_description_shows_the_video_as_a_still_frame(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--quiet", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        (name,) = [
            n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
        ]
        metadata = archive.read(name).decode()
    description = metadata.split("\n\n", 1)[1]
    readme = (ROOT / "README.md").read_text()
    assert "https://github.com/user-attachments/" in readme
    assert "user-attachments" not in description
    assert re.search(
        r'<a href="https://github\.com/suneel944/agent-parley#readme">'
        r'<img src="https://cdn\.jsdelivr\.net/gh/suneel944/agent-parley@v[\d.]+'
        r'/docs/assets/launch\.webp"',
        description,
    )
    assert (ROOT / "docs/assets/launch.webp").is_file()


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in (
        "pyproject.toml",
        "CHANGELOG.md",
        "scripts/install.sh",
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, root / name)
    shutil.copytree(ROOT / "plugins", root / "plugins")
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "version"
    ]
    (root / "dist").mkdir()
    for name in (
        f"agent_parley-{version}-py3-none-any.whl",
        f"agent_parley-{version}.tar.gz",
    ):
        (root / "dist" / name).write_text(f"Package: {name}\n")

    def export(command, **kwargs):
        Path(command[command.index("--output-file") + 1]).write_text("")
        return subprocess.CompletedProcess(command, 0)

    def codex(source, output):
        archive = output / f"agent-parley-{version}-codex-skills.zip"
        archive.write_text("codex\n")
        return archive

    monkeypatch.setattr(
        release_artifacts, "__file__", str(root / "scripts" / "x.py")
    )
    monkeypatch.setattr(release_artifacts.subprocess, "run", export)
    monkeypatch.setattr(codex_bundle, "build", codex)
    monkeypatch.delenv("RELEASE_TAG", raising=False)
    return root, version


def test_main_writes_every_asset_under_its_release_name(source_tree):
    root, version = source_tree
    release_artifacts.main()
    output = root / "dist" / "release"
    assert {path.name for path in output.iterdir()} == asset_names(version) | {
        "SHA256SUMS"
    }
    assert verify_assets(output, version) != ""
    (output / "install.sh").write_text("tampered\n")
    with pytest.raises(ValueError):
        verify_assets(output, version)


def test_main_rejects_a_tag_that_differs_from_the_package(
    source_tree, monkeypatch
):
    monkeypatch.setenv("RELEASE_TAG", "v0.0.0")
    with pytest.raises(ValueError, match="Release tag"):
        release_artifacts.main()


def test_main_rejects_a_plugin_on_another_version(source_tree):
    root, _ = source_tree
    manifest = root / "plugins/agent-parley/.codex-plugin/plugin.json"
    manifest.write_text('{"version": "0.0.0"}')
    with pytest.raises(ValueError, match="codex plugin"):
        release_artifacts.main()


def test_main_fails_without_the_built_packages(source_tree):
    root, _ = source_tree
    shutil.rmtree(root / "dist")
    with pytest.raises(FileNotFoundError):
        release_artifacts.main()
