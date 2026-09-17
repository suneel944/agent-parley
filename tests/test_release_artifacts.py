"""Checks release-note extraction and the published release asset set."""

import tomllib
from pathlib import Path

import pytest

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
