"""Publishes explicit releases using one immutable set of verified assets."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def command(*args: str, cwd: Path | None = None) -> str:
    """Runs a bounded command and preserves its failure diagnostics."""
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=300,
    ).stdout.strip()


def emit(name: str, value: str) -> None:
    """Records a workflow output after successful validation."""
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"{name}={value}\n")


def digest(path: Path) -> str:
    """Returns the SHA-256 digest of an artifact."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def asset_names(version: str) -> set[str]:
    """Lists the complete release bundle, excluding its checksum manifest."""
    return {
        f"agent_parley-{version}-py3-none-any.whl",
        f"agent_parley-{version}.tar.gz",
        f"agent-parley-plugins-{version}.zip",
        "requirements.txt",
        "CHANGELOG.md",
        "RELEASE_NOTES.md",
    }


def verify_assets(directory: Path, version: str) -> str:
    """Verifies exact filenames and hashes without trusting manifest paths.

    Args:
        directory: Downloaded or locally built release bundle.
        version: Validated release version.

    Returns:
        The checksum manifest's digest, binding later jobs to these bytes.

    Raises:
        ValueError: If files, manifest entries, or hashes disagree.
    """
    expected = asset_names(version)
    entries = {}
    manifest = directory / "SHA256SUMS"
    if manifest.is_symlink():
        raise ValueError("Checksum manifest must be a regular file.")
    for line in manifest.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match or match[2] not in expected or match[2] in entries:
            raise ValueError("Invalid or duplicate checksum entry.")
        entries[match[2]] = match[1]
    if set(entries) != expected or {p.name for p in directory.iterdir()} != (
        expected | {"SHA256SUMS"}
    ):
        raise ValueError("Release assets must match the complete manifest.")
    for name, checksum in entries.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != checksum:
            raise ValueError(f"Release checksum mismatch: {name}")
    return digest(manifest)


def validate_tag(root: Path, tag: str) -> str:
    """Requires a real tag on main history matching approved package metadata.

    A version rollback is not permission to republish different bytes. Existing
    release assets remain authoritative, and a retired tag cannot be dispatched
    while main approves another version.

    Args:
        root: Checkout of the workflow's main revision.
        tag: Explicit tag supplied by the maintainer.

    Returns:
        Exact commit ID for the source checkout.

    Raises:
        ValueError: If the tag or versions disagree.
        subprocess.CalledProcessError: If the tag is absent or outside main.
    """
    version_pattern = r"(?:0|[1-9][0-9]*)"
    if not re.fullmatch(
        rf"v{version_pattern}\.{version_pattern}\.{version_pattern}", tag
    ):
        raise ValueError("Release tag must be vMAJOR.MINOR.PATCH.")
    source = command(
        "git", "rev-parse", f"refs/tags/{tag}^{{commit}}", cwd=root
    )
    command("git", "merge-base", "--is-ancestor", source, "HEAD", cwd=root)
    approved = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    tagged = tomllib.loads(
        command("git", "show", f"{source}:pyproject.toml", cwd=root)
    )["project"]
    if approved["version"] != tag[1:] or tagged["version"] != tag[1:]:
        raise ValueError(
            "Tag must match both main and tagged package versions."
        )
    if approved["name"] != "agent-parley" or tagged["name"] != "agent-parley":
        raise ValueError("Unexpected distribution name.")
    return source


def github_release(tag: str) -> dict[str, Any] | None:
    """Distinguishes a missing release from API or permission failures."""
    result = subprocess.run(
        ["gh", "api", f"repos/{os.environ['GH_REPO']}/releases/tags/{tag}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data: dict[str, Any] = json.loads(result.stdout)
    if result.returncode and str(data.get("status")) == "404":
        return None
    if result.returncode:
        raise RuntimeError(f"Cannot inspect GitHub release: {result.stderr}")
    return data


def has_package_changes(root: Path) -> bool:
    """Checks distributed code, plugins and project metadata against the tag.

    Development dependencies, workflow repairs and release scripts alone do not
    justify another package version. An explicit preparation request must still
    contain a package change; old conventional commit titles are insufficient.

    Args:
        root: Main checkout with aligned version metadata.

    Returns:
        Whether the package or plugins differ from the approved release.

    Raises:
        ValueError: If release metadata does not identify the approved tag.
        subprocess.CalledProcessError: If Git cannot compare the source trees.
    """
    version = json.loads((root / ".release-please-manifest.json").read_text())[
        "."
    ]
    source = validate_tag(root, f"v{version}")
    changed = command(
        "git",
        "diff",
        "--name-only",
        source,
        "--",
        "agent_parley",
        "plugins/agent-parley",
        cwd=root,
    )
    previous = tomllib.loads(
        command("git", "show", f"{source}:pyproject.toml", cwd=root)
    )["project"]
    current = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    return bool(changed) or previous != current


def download(tag: str, directory: Path) -> None:
    """Downloads release assets into an empty directory."""
    command("gh", "release", "download", tag, "--dir", str(directory))


def prepare(root: Path, tag: str) -> str:
    """Creates a draft or resumes missing uploads without overwriting assets.

    The manifest is uploaded last. Until it exists, a retry may rebuild but must
    match every previously uploaded file. Once present, retries use those exact
    assets, including when a newer build backend would produce different bytes.
    """
    release = github_release(tag)
    names = {asset["name"] for asset in release["assets"]} if release else set()
    with tempfile.TemporaryDirectory() as scratch:
        downloaded = Path(scratch)
        if names:
            download(tag, downloaded)
        if "SHA256SUMS" in names:
            return verify_assets(downloaded, tag[1:])
        if release and not release["draft"]:
            raise ValueError("Published release has no checksum manifest.")
        source = root / "release-source"
        command("make", "release-artifacts", cwd=source)
        assets = source / "dist" / "release"
        checksum = verify_assets(assets, tag[1:])
        for name in names:
            if name not in asset_names(tag[1:]) or digest(
                downloaded / name
            ) != digest(assets / name):
                raise ValueError(f"Existing draft asset differs: {name}")
        if release is None:
            command(
                "gh",
                "release",
                "create",
                tag,
                "--verify-tag",
                "--draft",
                "--title",
                f"Agent Parley {tag}",
                "--notes-file",
                str(assets / "RELEASE_NOTES.md"),
            )
        for name in sorted(asset_names(tag[1:]) - names):
            command("gh", "release", "upload", tag, str(assets / name))
        command("gh", "release", "upload", tag, str(assets / "SHA256SUMS"))
    with tempfile.TemporaryDirectory() as scratch:
        download(tag, Path(scratch))
        if verify_assets(Path(scratch), tag[1:]) != checksum:
            raise ValueError("Uploaded checksum manifest changed.")
    return checksum


def pypi_files(version: str) -> list[dict[str, Any]]:
    """Reads PyPI file metadata, treating only HTTP 404 as an absent version."""
    url = f"https://pypi.org/pypi/agent-parley/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise
    return list(data["urls"])


def pending_files(
    directory: Path, version: str, existing: list[dict[str, Any]]
) -> list[Path]:
    """Rejects conflicting or yanked PyPI files and returns missing packages."""
    packages = {
        name: directory / name
        for name in asset_names(version)
        if name.endswith((".whl", ".tar.gz"))
    }
    seen = set()
    for item in existing:
        name = item["filename"]
        if name not in packages or name in seen:
            raise ValueError(f"Unexpected PyPI file: {name}")
        if item["yanked"] or item["digests"]["sha256"] != digest(
            packages[name]
        ):
            raise ValueError(
                f"PyPI file is yanked or has different bytes: {name}"
            )
        seen.add(name)
    return [packages[name] for name in sorted(packages.keys() - seen)]


def main() -> None:
    """Runs a release phase; every failure stops before the next side effect."""
    root = Path.cwd()
    phase = sys.argv[1]
    if phase == "candidate":
        eligible = has_package_changes(root)
        emit("eligible", str(eligible).lower())
        if not eligible:
            print("No package changes since the approved release.")
        return
    tag = os.environ["RELEASE_TAG"]
    source = validate_tag(root, tag)
    if phase == "validate":
        emit("source", source)
        return
    if phase == "prepare":
        if (
            command("git", "rev-parse", "HEAD", cwd=root / "release-source")
            != source
        ):
            raise ValueError("Source checkout differs from the validated tag.")
        emit("checksum", prepare(root, tag))
        return
    if phase not in {"stage", "finish"}:
        raise ValueError("Unknown release phase.")
    if source != os.environ["RELEASE_SOURCE"]:
        raise ValueError("Release tag changed between jobs.")
    with tempfile.TemporaryDirectory() as scratch:
        assets = Path(scratch)
        download(tag, assets)
        if verify_assets(assets, tag[1:]) != os.environ["RELEASE_CHECKSUM"]:
            raise ValueError("Release assets changed between jobs.")
        pending = pending_files(assets, tag[1:], pypi_files(tag[1:]))
        if phase == "stage":
            destination = root / "dist" / "pypi"
            destination.mkdir(parents=True, exist_ok=False)
            for path in pending:
                shutil.copyfile(path, destination / path.name)
            emit("pending", str(bool(pending)).lower())
        else:
            if pending:
                raise ValueError(
                    "PyPI publication is incomplete; retry this release."
                )
            command("gh", "release", "edit", tag, "--draft=false", "--latest")


if __name__ == "__main__":
    main()
