"""Checks the Codex directory archive and the manifest shape it depends on."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from scripts import codex_bundle

ROOT = Path(__file__).resolve().parents[1]


def copy_repository(tmp_path):
    """Copies the files the bundle reads so a test can corrupt one of them."""
    shutil.copytree(
        ROOT / codex_bundle.PLUGIN_DIRECTORY,
        tmp_path / codex_bundle.PLUGIN_DIRECTORY,
    )
    shutil.copyfile(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def rewrite_codex_manifest(root, change):
    """Applies one change to the copied Codex manifest's interface block."""
    path = root / codex_bundle.PLUGIN_DIRECTORY / ".codex-plugin/plugin.json"
    manifest = json.loads(path.read_text())
    change(manifest["interface"])
    path.write_text(json.dumps(manifest))


def test_repository_manifests_satisfy_both_directories():
    assert codex_bundle.manifest_errors(ROOT) == []


def test_archive_holds_merged_manifest_skills_and_square_images(tmp_path):
    bundle = codex_bundle.build(ROOT, tmp_path)
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(".claude-plugin/plugin.json"))
        images = {
            field: archive.read(manifest["interface"][field][2:])
            for field in codex_bundle.IMAGE_FIELDS
        }
    assert ".codex-plugin/plugin.json" not in names
    assert "skills/coordinate/SKILL.md" in names
    assert manifest["name"] == "agent-parley"
    assert manifest["interface"]["displayName"] == "Agent Parley"
    assert (
        len(manifest["interface"]["shortDescription"])
        <= codex_bundle.SHORT_DESCRIPTION_LIMIT
    )
    for data in images.values():
        width, height = codex_bundle.png_size(data)
        assert width == height


def test_archive_build_is_reproducible(tmp_path):
    first = codex_bundle.build(ROOT, tmp_path / "one").read_bytes()
    second = codex_bundle.build(ROOT, tmp_path / "two").read_bytes()
    assert first == second


def test_rejects_long_short_description(tmp_path):
    root = copy_repository(tmp_path)
    rewrite_codex_manifest(
        root,
        lambda interface: interface.update(
            shortDescription="x" * (codex_bundle.SHORT_DESCRIPTION_LIMIT + 1)
        ),
    )
    errors = codex_bundle.manifest_errors(root)
    assert errors == ["Codex interface.shortDescription exceeds 30 characters"]
    with pytest.raises(ValueError):
        codex_bundle.build(root, tmp_path / "dist")


def test_rejects_missing_and_non_square_images(tmp_path):
    root = copy_repository(tmp_path)
    assets = root / codex_bundle.PLUGIN_DIRECTORY / "assets"
    (assets / "logo.png").unlink()
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
    (assets / "icon.png").write_bytes(
        header + (512).to_bytes(4, "big") + (256).to_bytes(4, "big")
    )
    assert codex_bundle.manifest_errors(root) == [
        "Codex interface.logo file is missing",
        "Codex interface.composerIcon must be square",
    ]


def test_rejects_images_below_the_listing_edge(tmp_path):
    root = copy_repository(tmp_path)
    assets = root / codex_bundle.PLUGIN_DIRECTORY / "assets"
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
    small = codex_bundle.IMAGE_EDGE_MINIMUM - 1
    for name in ("logo.png", "icon.png"):
        (assets / name).write_bytes(
            header + small.to_bytes(4, "big") + small.to_bytes(4, "big")
        )
    assert codex_bundle.manifest_errors(root) == [
        "Codex interface.logo must be at least 1024 pixels square",
        "Codex interface.composerIcon must be at least 1024 pixels square",
    ]


def test_rejects_a_prompt_list_the_directory_would_truncate(tmp_path):
    root = copy_repository(tmp_path)
    rewrite_codex_manifest(
        root,
        lambda interface: interface.update(
            defaultPrompt=["a situation"] * (codex_bundle.PROMPT_LIMIT + 1)
        ),
    )
    assert codex_bundle.manifest_errors(root) == [
        "Codex interface.defaultPrompt keeps at most 3 prompts; later "
        "entries are dropped by the directory"
    ]
    rewrite_codex_manifest(
        root,
        lambda interface: interface.update(
            defaultPrompt=["x" * (codex_bundle.PROMPT_LENGTH_LIMIT + 1)]
        ),
    )
    assert codex_bundle.manifest_errors(root) == [
        "Codex interface.defaultPrompt exceeds 128 characters"
    ]


def test_rejects_a_listing_without_capabilities(tmp_path):
    root = copy_repository(tmp_path)
    rewrite_codex_manifest(
        root, lambda interface: interface.update(capabilities=[])
    )
    assert codex_bundle.manifest_errors(root) == [
        "Codex manifest is missing interface.capabilities"
    ]


def test_rejects_interface_block_in_claude_manifest(tmp_path):
    root = copy_repository(tmp_path)
    path = root / codex_bundle.PLUGIN_DIRECTORY / ".claude-plugin/plugin.json"
    manifest = json.loads(path.read_text())
    manifest["interface"] = {"displayName": "Agent Parley"}
    path.write_text(json.dumps(manifest))
    assert codex_bundle.manifest_errors(root)[0].startswith(
        "Claude manifest must not carry interface"
    )
