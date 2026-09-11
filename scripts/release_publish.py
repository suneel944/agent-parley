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

MINOR_THRESHOLD = 10
MAJOR_THRESHOLD = 50
RETIREMENT_LIMIT = 100
PACKAGE_PATHS = ("agent_parley", "plugins/agent-parley")
RELEASING_SUBJECT = re.compile(r"(feat|fix|perf)(?:\(([^()]*)\))?!?:")
ISSUE_REFERENCE = re.compile(
    r"\b(?:refs|fixes|closes|resolves)\s+#(\d+)\b", re.IGNORECASE
)


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


def release_history(root: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Loads reviewed tag mappings and versions that cannot be reused.

    Args:
        root: Checkout containing release configuration.

    Returns:
        Commit mappings indexed by release tag, and retired versions.

    Raises:
        ValueError: If the migration file contains invalid entries.
    """
    path = root / ".github/release-history.json"
    if not path.exists():
        return {}, []
    history = json.loads(path.read_text())
    if not isinstance(history, dict) or set(history) != {
        "migrations",
        "retired",
    }:
        raise ValueError("Release history requires migrations and retirement.")
    mappings, retired = history["migrations"], history["retired"]
    if (
        not isinstance(mappings, dict)
        or not isinstance(retired, list)
        or any(
            not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", version)
            for version in retired
        )
    ):
        raise ValueError("Release history contains invalid versions.")
    for tag, entry in mappings.items():
        if (
            not re.fullmatch(r"v\d+\.\d+\.\d+", tag)
            or not isinstance(entry, dict)
            or set(entry) != {"original", "rewritten"}
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[0-9a-f]{40}", value)
                for value in entry.values()
            )
        ):
            raise ValueError("Release history requires exact commit IDs.")
    return mappings, retired


def release_baseline(root: Path, tag: str, source: str) -> str:
    """Requires source ancestry or an exact, tree-equivalent migration.

    Args:
        root: Checkout of the proposed main revision.
        tag: Validated release tag.
        source: Resolved original tag commit.

    Returns:
        The equivalent release commit on the current branch's ancestry.

    Raises:
        ValueError: If a mapped tag moved or its rewritten tree differs.
        subprocess.CalledProcessError: If a commit is absent or outside HEAD.
    """
    migrations, retired = release_history(root)
    if tag[1:] in retired:
        raise ValueError("Retired release versions cannot be reused.")
    migration = migrations.get(tag)
    baseline = source
    if migration:
        if source != migration["original"]:
            raise ValueError("Migrated tag changed its original commit.")
        baseline = migration["rewritten"]
        original_tree = command(
            "git", "rev-parse", f"{source}^{{tree}}", cwd=root
        )
        rewritten_tree = command(
            "git", "rev-parse", f"{baseline}^{{tree}}", cwd=root
        )
        if original_tree != rewritten_tree:
            raise ValueError("Migrated release trees must match exactly.")
    command("git", "merge-base", "--is-ancestor", baseline, "HEAD", cwd=root)
    return baseline


def next_version(approved: str, kind: str) -> str:
    """Raises one component of a version and resets the lower ones.

    Args:
        approved: Version to advance, as MAJOR.MINOR.PATCH.
        kind: Release kind, one of major, minor or patch.

    Returns:
        The immediately following version of the requested kind.
    """
    major, minor, patch = (int(part) for part in approved.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def advance_version(approved: str, kind: str, retired: list[str]) -> str:
    """Projects the next version from configuration alone, skipping retirement.

    This is the offline projection used by configuration checks. It answers
    what preparation would reach using only the retirement list, without
    consulting Git, GitHub or the package index.

    Args:
        approved: Approved release version.
        kind: Release kind, one of major, minor or patch.
        retired: Versions that may never be reused.

    Returns:
        The first version of that kind outside the retirement list, or the
        last candidate examined when the bounded search finds none.
    """
    candidate = approved
    for _ in range(RETIREMENT_LIMIT):
        candidate = next_version(candidate, kind)
        if candidate not in retired:
            break
    return candidate


def version_available(root: Path, version: str, retired: list[str]) -> bool:
    """Reports whether a version can still be created everywhere it must exist.

    A retirement list is maintained by hand and cannot be the only evidence.
    An existing tag, an existing GitHub release including a draft, or an
    existing package index version all make a release run fail, so each is
    consulted directly. Only a definite absence makes a version available:
    a failing check raises rather than reporting availability, because a
    proposal built on an unanswered question is the failure it should have
    prevented. Remote checks are skipped only when their environment is
    genuinely absent, never because they errored.

    Args:
        root: Checkout used for tag lookups.
        version: Candidate version without its leading v.
        retired: Versions that may never be reused.

    Returns:
        Whether the version is free of retirement, tags, releases and files.

    Raises:
        RuntimeError: If the GitHub release API cannot be inspected.
        urllib.error.URLError: If the package index cannot be inspected.
        subprocess.CalledProcessError: If Git cannot list local or remote tags.
    """
    if version in retired:
        return False
    tag = f"v{version}"
    if command("git", "tag", "--list", tag, cwd=root):
        return False
    if "origin" in command("git", "remote", cwd=root).split():
        if command(
            "git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", cwd=root
        ):
            return False
    if os.environ.get("GH_REPO") and github_release(tag) is not None:
        return False
    return not pypi_files(version)


def available_version(
    root: Path, approved: str, kind: str, retired: list[str]
) -> str:
    """Looks ahead for the first version of a kind that nothing else claims.

    Args:
        root: Checkout used for tag lookups.
        approved: Approved release version.
        kind: Release kind, one of major, minor or patch.
        retired: Versions that may never be reused.

    Returns:
        The first available version of the requested kind.

    Raises:
        ValueError: If the bounded search finds no available version.
    """
    candidate = approved
    for _ in range(RETIREMENT_LIMIT):
        candidate = next_version(candidate, kind)
        if version_available(root, candidate, retired):
            return candidate
    raise ValueError(f"No {kind} version is available after {approved}.")


def product_units(root: Path, baseline: str) -> tuple[set[str], set[str], bool]:
    """Measures delivered product work between the approved release and HEAD.

    A commit contributes only when a releasing conventional type introduces
    it and it touches the distributed package or the plugins. Workflow,
    script, test and documentation commits are therefore structurally
    incapable of raising a version. Issue references collapse repeated pull
    requests for one issue into a single unit, and a qualifying commit that
    names no issue counts as one unit of its own so delivered work is never
    under-reported. Measurement is local: one Git history read, no network.
    Records and fields are separated with control bytes that can appear in
    neither a commit message nor a path, so a crafted message cannot forge
    a commit boundary.

    Args:
        root: Checkout containing the release baseline and HEAD.
        baseline: Approved release commit on the current branch's ancestry.

    Returns:
        Distinct units from every releasing type, the units introduced by
        feature commits, and whether an urgent fix requests a patch release.

    Raises:
        subprocess.CalledProcessError: If Git cannot read the commit range.
    """
    log = command(
        "git",
        "log",
        "--no-merges",
        "--name-only",
        "--format=%x00%H%x01%B%x01",
        f"{baseline}..HEAD",
        cwd=root,
    )
    issues: set[str] = set()
    features: set[str] = set()
    urgent = False
    for record in log.split("\x00")[1:]:
        commit, message, names = record.split("\x01")
        subject = RELEASING_SUBJECT.match(message)
        if not subject or not any(
            name == path or name.startswith(f"{path}/")
            for name in names.splitlines()
            for path in PACKAGE_PATHS
        ):
            continue
        units = {
            f"#{number}" for number in ISSUE_REFERENCE.findall(message)
        } or {commit}
        issues |= units
        if subject[1] == "feat":
            features |= units
        urgent = urgent or (subject[1] == "fix" and subject[2] == "urgent")
    return issues, features, urgent


def release_candidate(root: Path) -> tuple[str, int, int]:
    """Returns the version the measured product changes propose, with counts.

    Eligibility is measured rather than judged so preparation can run
    unattended. Fifty product features propose the next major version, ten
    product issues propose the next minor version, and a single commit
    titled fix(urgent) is the only mechanical marker that proposes a patch
    version. Nothing else raises a version. The proposal then looks ahead
    for a version that is genuinely free.

    Args:
        root: Checkout containing the version manifest and release history.

    Returns:
        The proposed version, empty when nothing is proposed, with the
        distinct issue count and the distinct feature count.

    Raises:
        ValueError: If the approved version is not MAJOR.MINOR.PATCH, or no
            version of the proposed kind is available.
        subprocess.CalledProcessError: If Git cannot resolve the baseline.
    """
    approved = json.loads((root / ".release-please-manifest.json").read_text())[
        "."
    ]
    if not re.fullmatch(r"\d+\.\d+\.\d+", approved):
        raise ValueError("Approved version must be MAJOR.MINOR.PATCH.")
    tag = f"v{approved}"
    baseline = release_baseline(root, tag, validate_tag(root, tag))
    issues, features, urgent = product_units(root, baseline)
    if len(features) >= MAJOR_THRESHOLD:
        kind = "major"
    elif len(issues) >= MINOR_THRESHOLD:
        kind = "minor"
    elif urgent:
        kind = "patch"
    else:
        return "", len(issues), len(features)
    _, retired = release_history(root)
    proposal = available_version(root, approved, kind, retired)
    return proposal, len(issues), len(features)


def align_scan_boundary(root: Path, version: str) -> bool:
    """Aligns the release scan boundary with the version being prepared.

    A history migration pins Release Please to the rewritten commit of the
    approved release so preparation never walks into rewritten history. That
    pin is specific to one version: the moment a proposal advances past it,
    the configuration the policy gate accepts is the one that carries the
    boundary recorded for the proposed version, and no boundary at all when
    that version was never migrated. Writing it here keeps a proposal from
    arriving with a boundary the gate refuses.

    Args:
        root: Checkout holding the configuration to align.
        version: Version the proposal prepares.

    Returns:
        Whether the configuration on disk changed.
    """
    path = root / "release-please-config.json"
    config = json.loads(path.read_text())
    migrations, _ = release_history(root)
    migration = migrations.get(f"v{version}")
    expected = migration["rewritten"] if migration else None
    if config.get("last-release-sha") == expected:
        return False
    if expected is None:
        config.pop("last-release-sha")
    else:
        config["last-release-sha"] = expected
    path.write_text(json.dumps(config, indent=2) + "\n")
    return True


def migration_config_errors(root: Path) -> list[str]:
    """Rejects missing, stale or misplaced release-history scan boundaries.

    Preparation now advances past a retired number instead of failing on it,
    so this check guards the remaining configuration mistake: a retirement
    list so long that some release kind has no unretired number left within
    the bounded search. Every check here is offline and reads only files.

    Args:
        root: Checkout containing release configuration and version manifest.

    Returns:
        Configuration errors that block policy checks and preparation.
    """
    config = json.loads((root / "release-please-config.json").read_text())
    version = json.loads((root / ".release-please-manifest.json").read_text())[
        "."
    ]
    migrations, retired = release_history(root)
    migration = migrations.get(f"v{version}")
    expected = migration["rewritten"] if migration else None
    errors = []
    if version in retired:
        errors.append("Retired release versions cannot be reused.")
    if any(
        advance_version(version, kind, retired) in retired
        for kind in ("major", "minor", "patch")
    ):
        errors.append(
            "Every candidate version of one release kind is retired; "
            "preparation would have no version left to propose."
        )
    if config.get("last-release-sha") != expected:
        errors.append(
            "Release scan boundary must match the approved migration; "
            "remove it when preparing the next version."
        )
    if any(
        "last-release-sha" in package for package in config["packages"].values()
    ):
        errors.append("Release scan boundaries belong at configuration root.")
    return errors


def validate_tag(root: Path, tag: str) -> str:
    """Requires a real tag with approved metadata and verified provenance.

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
        subprocess.CalledProcessError: If tag provenance is absent from main.
    """
    version_pattern = r"(?:0|[1-9][0-9]*)"
    if not re.fullmatch(
        rf"v{version_pattern}\.{version_pattern}\.{version_pattern}", tag
    ):
        raise ValueError("Release tag must be vMAJOR.MINOR.PATCH.")
    source = command(
        "git", "rev-parse", f"refs/tags/{tag}^{{commit}}", cwd=root
    )
    release_baseline(root, tag, source)
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
        *PACKAGE_PATHS,
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
        errors = migration_config_errors(root)
        if errors:
            raise ValueError("\n".join(errors))
        approved = json.loads(
            (root / ".release-please-manifest.json").read_text()
        )["."]
        version, issues, features = release_candidate(root)
        changed = has_package_changes(root)
        eligible = bool(version) and changed
        emit("eligible", str(eligible).lower())
        emit("version", version)
        emit("issues", str(issues))
        emit("features", str(features))
        measured = (
            f"{issues} product issues and {features} product features "
            f"since v{approved}"
        )
        if not version:
            print(
                f"{measured}; {MINOR_THRESHOLD} issues or "
                f"{MAJOR_THRESHOLD} features or one fix(urgent) commit "
                "are required."
            )
        elif not changed:
            print(f"{measured}; no package changes since the approved release.")
        else:
            print(f"{measured}; proposing {version}.")
        return
    if phase == "boundary":
        version = json.loads(
            (root / ".release-please-manifest.json").read_text()
        )["."]
        changed = align_scan_boundary(root, version)
        print(
            f"Scan boundary aligned with {version}."
            if changed
            else f"Scan boundary already matches {version}."
        )
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
