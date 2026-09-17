"""Builds the versioned release bundle from verified package artifacts."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path


def release_notes(
    changelog: str,
    version: str,
    repository: str = "",
) -> str:
    """Builds GitHub release notes for one version of the changelog.

    The result follows the conventional release-note shape: a "What's Changed"
    heading, the generated subsections for this version, and a full changelog
    link.

    Args:
        changelog: Complete Markdown changelog.
        version: Exact package version to extract.
        repository: Repository URL used for the full changelog link.

    Returns:
        Release notes for this version, excluding adjacent versions.

    Raises:
        ValueError: If there is no matching nonempty version section.
    """
    heading = re.compile(
        rf"^## \[{re.escape(version)}\]"
        r"(?:\([^\n]*\))?(?: - | \()[^\n]+\n",
        re.MULTILINE,
    )
    match = heading.search(changelog)
    if not match:
        raise ValueError("Add this version to CHANGELOG.md before releasing.")
    section = re.split(
        r"^## ", changelog[match.end() :], maxsplit=1, flags=re.MULTILINE
    )[0].strip()
    if not section:
        raise ValueError("Release notes must not be empty.")
    parts = ["## What's Changed", section]
    if repository.strip():
        link = f"{repository.strip().rstrip('/')}/commits/v{version}"
        parts.append(f"**Full Changelog**: {link}")
    return "\n\n".join(parts) + "\n"


def add_entry(archive: zipfile.ZipFile, path: Path, name: str) -> None:
    """Stores one file with fixed metadata so the bundle is reproducible.

    Checkout timestamps and file modes vary between machines and between
    continuous integration runs, and a zip entry records both, so an
    otherwise identical bundle hashes differently each time it is built.
    Fixing them lets a rebuild of a released tag be compared byte for byte
    against the published asset.

    Args:
        archive: Open bundle receiving the entry.
        path: File to store.
        name: Path recorded inside the bundle.

    Raises:
        OSError: If the file cannot be read.
    """
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o644 << 16
    archive.writestr(entry, path.read_bytes())


def checksums(assets: list[Path]) -> str:
    """Builds the checksum manifest that binds the release to these bytes.

    Args:
        assets: Files written into the release directory.

    Returns:
        One ``sha256  filename`` line per asset, ordered by path.

    Raises:
        OSError: If an asset cannot be read.
    """
    return "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(assets)
    )


def main() -> None:
    """Collects release assets and hashes after checking version agreement.

    The Codex submission archive is one of those assets, so every release
    page carries the file the OpenAI plugin portal accepts and no maintainer
    builds it by hand. Its builder is imported here rather than at module
    scope because that module imports this one for its archive entries.

    Raises:
        ValueError: If a tag, plugin version, or changelog does not match.
        OSError: If an expected package or bundle input is unavailable.
    """
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = project["version"]
    repository = project.get("urls", {}).get("Repository", "")
    tag = f"v{version}"
    if os.environ.get("RELEASE_TAG", tag) != tag:
        raise ValueError("Release tag must match the package version.")
    plugin = root / "plugins" / "agent-parley"
    for client in ("claude", "codex"):
        manifest = json.loads(
            (plugin / f".{client}-plugin" / "plugin.json").read_text()
        )
        if manifest["version"] != version:
            raise ValueError(f"{client} plugin version differs from package.")
    changelog = (root / "CHANGELOG.md").read_text()
    notes = release_notes(changelog, version, repository)
    output = root / "dist" / "release"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    assets = []
    for filename in (
        f"agent_parley-{version}-py3-none-any.whl",
        f"agent_parley-{version}.tar.gz",
    ):
        destination = output / filename
        shutil.copyfile(root / "dist" / filename, destination)
        assets.append(destination)
    bundle = output / f"agent-parley-plugins-{version}.zip"
    inputs = [
        root / ".agents" / "plugins" / "marketplace.json",
        root / ".claude-plugin" / "marketplace.json",
        *sorted(path for path in plugin.rglob("*") if path.is_file()),
    ]
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in inputs:
            add_entry(archive, path, str(path.relative_to(root)))
        add_entry(archive, root / "plugins" / "README.md", "README.md")
    assets.append(bundle)
    from scripts.codex_bundle import build as build_codex_archive

    assets.append(build_codex_archive(root, output))
    requirements = output / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(requirements),
        ],
        cwd=root,
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
    )
    assets.append(requirements)
    for filename, text in (
        ("CHANGELOG.md", changelog),
        ("RELEASE_NOTES.md", notes),
    ):
        path = output / filename
        path.write_text(text)
        assets.append(path)
    (output / "SHA256SUMS").write_text(checksums(assets))
    print(f"Release assets for {tag}: {output}")


if __name__ == "__main__":
    main()
