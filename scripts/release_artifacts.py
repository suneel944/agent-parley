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
    overview: str = "",
    repository: str = "",
) -> str:
    """Builds GitHub release notes for one version of the changelog.

    The result follows the conventional release-note shape: a "What's Changed"
    heading, a short standing description of the project, the generated
    subsections for this version, and a full changelog link.

    Args:
        changelog: Complete Markdown changelog.
        version: Exact package version to extract.
        overview: Standing product description placed before the section, so
            that every release states what the project is without a per-release
            edit.
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
    parts = ["## What's Changed"]
    if overview.strip():
        parts.append(overview.strip())
    parts.append(section)
    if repository.strip():
        link = f"{repository.strip().rstrip('/')}/commits/v{version}"
        parts.append(f"**Full Changelog**: {link}")
    return "\n\n".join(parts) + "\n"


def main() -> None:
    """Collects release assets and hashes after checking version agreement.

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
    overview = (root / "docs" / "release-overview.md").read_text()
    notes = release_notes(changelog, version, overview, repository)
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
            archive.write(path, path.relative_to(root))
        archive.write(root / "plugins" / "README.md", "README.md")
    assets.append(bundle)
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
    sums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(assets)
    ]
    (output / "SHA256SUMS").write_text("".join(sums))
    print(f"Release assets for {tag}: {output}")


if __name__ == "__main__":
    main()
