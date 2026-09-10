"""Exercises release retries, trust boundaries, and publication ordering."""

import io
import json
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

from scripts import release_publish as release

VERSION = "0.1.1"
TAG = f"v{VERSION}"


@pytest.fixture
def assets(tmp_path):
    directory = tmp_path / "release-source" / "dist" / "release"
    directory.mkdir(parents=True)
    for name in release.asset_names(VERSION):
        (directory / name).write_text(f"Artifact: {name}\n")
    (directory / "SHA256SUMS").write_text(
        "".join(
            f"{release.digest(directory / name)}  {name}\n"
            for name in sorted(release.asset_names(VERSION))
        )
    )
    return directory


def pypi_metadata(assets):
    return [
        {
            "filename": path.name,
            "yanked": False,
            "digests": {"sha256": release.digest(path)},
        }
        for path in release.pending_files(assets, VERSION, [])
    ]


def test_complete_bundle_and_pypi_retry_have_no_pending_uploads(assets):
    assert release.verify_assets(assets, VERSION) == release.digest(
        assets / "SHA256SUMS"
    )
    metadata = pypi_metadata(assets)
    assert release.pending_files(assets, VERSION, metadata) == []
    assert len(release.pending_files(assets, VERSION, metadata[:1])) == 1
    assert len(release.pending_files(assets, VERSION, [])) == 2


@pytest.mark.parametrize("damage", ["changed", "missing", "extra", "symlink"])
def test_damaged_assets_are_rejected(assets, damage):
    path = assets / "CHANGELOG.md"
    if damage == "changed":
        path.write_text("Changed")
    elif damage == "missing":
        path.unlink()
    elif damage == "extra":
        (assets / "unexpected.whl").write_text("Extra")
    else:
        path.unlink()
        path.symlink_to(assets / "RELEASE_NOTES.md")
    with pytest.raises(ValueError):
        release.verify_assets(assets, VERSION)


@pytest.mark.parametrize(
    "entry", ["../outside", "/absolute", "duplicate", "empty"]
)
def test_checksum_manifest_cannot_select_arbitrary_files(assets, entry):
    manifest = assets / "SHA256SUMS"
    if entry == "duplicate":
        manifest.write_text(manifest.read_text() * 2)
    elif entry == "empty":
        manifest.write_text("")
    else:
        manifest.write_text(f"{'0' * 64}  {entry}\n")
    with pytest.raises(ValueError):
        release.verify_assets(assets, VERSION)


@pytest.mark.parametrize("damage", ["hash", "yanked", "unknown", "duplicate"])
def test_existing_pypi_files_must_match_exactly(assets, damage):
    metadata = pypi_metadata(assets)
    if damage == "hash":
        metadata[0]["digests"]["sha256"] = "0" * 64
    elif damage == "yanked":
        metadata[0]["yanked"] = True
    elif damage == "unknown":
        metadata[0]["filename"] = "unexpected.whl"
    else:
        metadata.append(metadata[0])
    with pytest.raises(ValueError):
        release.pending_files(assets, VERSION, metadata)


@pytest.fixture
def git_repo(tmp_path):
    release.command("git", "init", "-b", "main", str(tmp_path))
    release.command("git", "config", "user.name", "Test", cwd=tmp_path)
    release.command(
        "git", "config", "user.email", "test@example.com", cwd=tmp_path
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "agent-parley"\nversion = "0.1.1"\n'
    )
    release.command("git", "add", "pyproject.toml", cwd=tmp_path)
    release.command("git", "commit", "-m", "Initial version", cwd=tmp_path)
    return tmp_path


def product_commit(repo, subject, path="agent_parley/module.py", body=""):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as stream:
        stream.write(f"{subject}\n")
    release.command("git", "add", "-A", cwd=repo)
    release.command(
        "git",
        "commit",
        "-m",
        f"{subject}\n\n{body}" if body else subject,
        cwd=repo,
    )


@pytest.fixture
def local(monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(release, "pypi_files", lambda version: [])
    return monkeypatch


@pytest.fixture
def counted_repo(git_repo):
    (git_repo / ".release-please-manifest.json").write_text('{".": "0.1.1"}')
    (git_repo / "release-please-config.json").write_text(
        '{"packages": {".": {}}}'
    )
    release.command("git", "add", ".", cwd=git_repo)
    release.command("git", "commit", "-m", "chore: configure", cwd=git_repo)
    release.command("git", "tag", TAG, cwd=git_repo)
    return git_repo


@pytest.mark.parametrize("annotated", [False, True])
def test_real_tag_on_main_resolves_to_exact_commit(git_repo, annotated):
    args = ("-a", "-m", "Release") if annotated else ()
    release.command("git", "tag", *args, TAG, cwd=git_repo)
    assert release.validate_tag(git_repo, TAG) == release.command(
        "git", "rev-parse", "HEAD", cwd=git_repo
    )


@pytest.mark.parametrize(
    "tag", ["main", "--help", "v01.1.1", "v0.1.1/../main", "v0.1.1\n"]
)
def test_invalid_tags_are_rejected_before_git(git_repo, tag):
    with pytest.raises(ValueError, match="Release tag"):
        release.validate_tag(git_repo, tag)


def test_branch_with_tag_name_cannot_substitute_for_a_tag(git_repo):
    release.command("git", "branch", TAG, cwd=git_repo)
    with pytest.raises(subprocess.CalledProcessError):
        release.validate_tag(git_repo, TAG)


def test_tag_outside_main_history_is_rejected(git_repo):
    release.command("git", "checkout", "-b", "other", cwd=git_repo)
    release.command(
        "git", "commit", "--allow-empty", "-m", "Other", cwd=git_repo
    )
    release.command("git", "tag", TAG, cwd=git_repo)
    release.command("git", "checkout", "main", cwd=git_repo)
    with pytest.raises(subprocess.CalledProcessError):
        release.validate_tag(git_repo, TAG)


def test_unapproved_version_cannot_be_republished(git_repo):
    release.command("git", "tag", "v0.1.2", cwd=git_repo)
    with pytest.raises(ValueError, match="versions"):
        release.validate_tag(git_repo, "v0.1.2")


@pytest.fixture
def migrated_repo(git_repo):
    release.command("git", "tag", TAG, cwd=git_repo)
    original = release.command("git", "rev-parse", "HEAD", cwd=git_repo)
    release.command(
        "git", "commit", "--amend", "-m", "Reworded version", cwd=git_repo
    )
    rewritten = release.command("git", "rev-parse", "HEAD", cwd=git_repo)
    (git_repo / ".github").mkdir()
    (git_repo / ".github/release-history.json").write_text(
        json.dumps(
            {
                "migrations": {
                    TAG: {"original": original, "rewritten": rewritten}
                },
                "retired": ["0.1.2"],
            }
        )
    )
    (git_repo / ".release-please-manifest.json").write_text(
        json.dumps({".": VERSION})
    )
    (git_repo / "release-please-config.json").write_text(
        json.dumps({"packages": {".": {}}, "last-release-sha": rewritten})
    )
    release.command("git", "add", ".", cwd=git_repo)
    release.command("git", "commit", "-m", "Map release", cwd=git_repo)
    return git_repo


def test_migration_preserves_tag_source_and_ignores_tooling(migrated_repo):
    original = release.command(
        "git", "rev-parse", f"refs/tags/{TAG}", cwd=migrated_repo
    )
    assert release.validate_tag(migrated_repo, TAG) == original
    assert not release.has_package_changes(migrated_repo)
    assert release.migration_config_errors(migrated_repo) == []
    package = migrated_repo / "agent_parley"
    package.mkdir()
    (package / "cli.py").write_text('"""A new package change."""\n')
    release.command("git", "add", ".", cwd=migrated_repo)
    release.command("git", "commit", "-m", "Package change", cwd=migrated_repo)
    assert release.has_package_changes(migrated_repo)


@pytest.mark.parametrize("damage", ["moved_tag", "different_tree", "missing"])
def test_migration_cannot_authorize_changed_provenance(migrated_repo, damage):
    path = migrated_repo / ".github/release-history.json"
    history = json.loads(path.read_text())
    mapping = history["migrations"]
    if damage == "moved_tag":
        release.command(
            "git",
            "tag",
            "-f",
            TAG,
            mapping[TAG]["rewritten"],
            cwd=migrated_repo,
        )
        expected = ValueError
    elif damage == "different_tree":
        mapping[TAG]["rewritten"] = release.command(
            "git", "rev-parse", "HEAD", cwd=migrated_repo
        )
        path.write_text(json.dumps(history))
        expected = ValueError
    else:
        path.unlink()
        expected = subprocess.CalledProcessError
    with pytest.raises(expected):
        release.validate_tag(migrated_repo, TAG)


def test_equivalent_migration_commit_must_be_on_current_history(migrated_repo):
    path = migrated_repo / ".github/release-history.json"
    history = json.loads(path.read_text())
    mapping = history["migrations"]
    release.command(
        "git",
        "checkout",
        "-b",
        "other",
        mapping[TAG]["original"],
        cwd=migrated_repo,
    )
    release.command(
        "git",
        "commit",
        "--amend",
        "-m",
        "Unrelated rewrite",
        cwd=migrated_repo,
    )
    mapping[TAG]["rewritten"] = release.command(
        "git", "rev-parse", "HEAD", cwd=migrated_repo
    )
    release.command("git", "checkout", "main", cwd=migrated_repo)
    path.write_text(json.dumps(history))
    with pytest.raises(subprocess.CalledProcessError):
        release.validate_tag(migrated_repo, TAG)


@pytest.mark.parametrize(
    "entry", [None, {}, {"original": "HEAD", "rewritten": "HEAD"}]
)
def test_migration_requires_exact_commit_ids(migrated_repo, entry):
    (migrated_repo / ".github/release-history.json").write_text(
        json.dumps({"migrations": {TAG: entry}, "retired": []})
    )
    with pytest.raises(ValueError, match="exact commit IDs"):
        release.validate_tag(migrated_repo, TAG)


@pytest.mark.parametrize("damage", ["absent", "wrong", "package", "stale"])
def test_release_scan_boundary_cannot_drift(migrated_repo, damage):
    path = migrated_repo / "release-please-config.json"
    config = json.loads(path.read_text())
    if damage == "absent":
        config.pop("last-release-sha")
    elif damage == "wrong":
        config["last-release-sha"] = "0" * 40
    elif damage == "package":
        config["packages"]["."]["last-release-sha"] = config["last-release-sha"]
    else:
        (migrated_repo / ".release-please-manifest.json").write_text(
            '{".": "0.1.3"}'
        )
    path.write_text(json.dumps(config))
    assert release.migration_config_errors(migrated_repo)
    if damage == "stale":
        config.pop("last-release-sha")
        path.write_text(json.dumps(config))
        assert release.migration_config_errors(migrated_repo) == []


def test_next_release_uses_normal_ancestry_after_migration(migrated_repo):
    path = migrated_repo / "pyproject.toml"
    path.write_text('[project]\nname = "agent-parley"\nversion = "0.1.3"\n')
    release.command("git", "add", ".", cwd=migrated_repo)
    release.command("git", "commit", "-m", "Next release", cwd=migrated_repo)
    release.command("git", "tag", "v0.1.3", cwd=migrated_repo)
    assert release.validate_tag(migrated_repo, "v0.1.3") == release.command(
        "git", "rev-parse", "HEAD", cwd=migrated_repo
    )
    with pytest.raises(ValueError, match="versions"):
        release.validate_tag(migrated_repo, TAG)


def test_retired_version_cannot_be_prepared_or_published(migrated_repo):
    (migrated_repo / "pyproject.toml").write_text(
        '[project]\nname = "agent-parley"\nversion = "0.1.2"\n'
    )
    (migrated_repo / ".release-please-manifest.json").write_text(
        '{".": "0.1.2"}'
    )
    (migrated_repo / "release-please-config.json").write_text(
        '{"packages": {".": {}}}'
    )
    release.command("git", "add", ".", cwd=migrated_repo)
    release.command("git", "commit", "-m", "Retired version", cwd=migrated_repo)
    release.command("git", "tag", "v0.1.2", cwd=migrated_repo)
    assert release.migration_config_errors(migrated_repo) == [
        "Retired release versions cannot be reused."
    ]
    with pytest.raises(ValueError, match="Retired"):
        release.validate_tag(migrated_repo, "v0.1.2")


def test_patch_bumping_onto_a_retired_version_is_skipped(migrated_repo, local):
    product_commit(migrated_repo, "fix(urgent): repair the package")
    assert release.advance_version("0.1.1", "patch", ["0.1.2"]) == "0.1.3"
    assert release.release_candidate(migrated_repo)[0] == "0.1.3"
    assert release.migration_config_errors(migrated_repo) == []
    path = migrated_repo / ".github/release-history.json"
    history = json.loads(path.read_text())
    history["retired"] = [
        f"0.1.{number}" for number in range(2, release.RETIREMENT_LIMIT + 3)
    ]
    path.write_text(json.dumps(history))
    assert release.migration_config_errors(migrated_repo) == [
        "Every candidate version of one release kind is retired; "
        "preparation would have no version left to propose."
    ]
    with pytest.raises(ValueError, match="No patch version is available"):
        release.release_candidate(migrated_repo)


def test_infrastructure_commits_cannot_raise_a_version(counted_repo, local):
    product_commit(counted_repo, "ci: retune", ".github/workflows/check.yml")
    product_commit(counted_repo, "docs: explain", "docs/releases.md")
    product_commit(counted_repo, "feat: write a plan", "docs/plan.md")
    product_commit(counted_repo, "feat: rework tooling", "scripts/tool.py")
    product_commit(counted_repo, "chore: touch the package")
    assert release.release_candidate(counted_repo) == ("", 0, 0)


def test_package_and_plugin_commits_each_count_one_unit(counted_repo, local):
    product_commit(counted_repo, "feat: add a command", "agent_parley/cli.py")
    assert release.release_candidate(counted_repo) == ("", 1, 1)
    product_commit(
        counted_repo,
        "fix: repair the skill",
        "plugins/agent-parley/skills/coordinate/SKILL.md",
    )
    assert release.release_candidate(counted_repo) == ("", 2, 1)


def test_one_issue_across_three_pull_requests_counts_once(counted_repo, local):
    for number in range(3):
        product_commit(counted_repo, f"feat: part {number}", body="Refs #7")
    assert release.release_candidate(counted_repo) == ("", 1, 1)
    product_commit(counted_repo, "fix: repair part one", body="Fixes #7")
    product_commit(counted_repo, "perf: speed up parts", body="closes #8")
    assert release.release_candidate(counted_repo) == ("", 2, 1)


def test_a_qualifying_commit_without_an_issue_still_counts(counted_repo, local):
    product_commit(counted_repo, "feat: unreferenced work")
    product_commit(counted_repo, "feat: further unreferenced work")
    assert release.release_candidate(counted_repo) == ("", 2, 2)


def test_ten_product_issues_propose_the_next_minor(counted_repo, local):
    for number in range(release.MINOR_THRESHOLD - 1):
        product_commit(
            counted_repo, f"fix: repair {number}", body=f"Refs #{number}"
        )
    assert release.release_candidate(counted_repo) == ("", 9, 0)
    product_commit(counted_repo, "feat: the tenth unit", body="Resolves #99")
    assert release.release_candidate(counted_repo) == ("0.2.0", 10, 1)


def test_fifty_product_features_propose_the_next_major(counted_repo, local):
    for number in range(release.MAJOR_THRESHOLD - 1):
        product_commit(
            counted_repo, f"feat: feature {number}", body=f"Refs #{number}"
        )
    assert release.release_candidate(counted_repo) == ("0.2.0", 49, 49)
    product_commit(counted_repo, "feat: the fiftieth", body="Refs #999")
    assert release.release_candidate(counted_repo) == ("1.0.0", 50, 50)


def test_only_an_urgent_fix_proposes_a_patch(counted_repo, local):
    product_commit(counted_repo, "fix: a quiet repair", body="Refs #3")
    assert release.release_candidate(counted_repo) == ("", 1, 0)
    product_commit(counted_repo, "fix(urgent): stop the bleeding")
    assert release.release_candidate(counted_repo) == ("0.1.2", 2, 0)


def test_an_existing_tag_alone_makes_a_version_unavailable(counted_repo, local):
    product_commit(counted_repo, "fix(urgent): stop the bleeding")
    release.command("git", "tag", "v0.1.2", cwd=counted_repo)
    assert release.release_candidate(counted_repo)[0] == "0.1.3"


def test_a_draft_release_alone_makes_a_version_unavailable(counted_repo, local):
    product_commit(counted_repo, "fix(urgent): stop the bleeding")
    local.setenv("GH_REPO", "owner/repo")
    local.setattr(
        release,
        "github_release",
        lambda tag: {"draft": True} if tag == "v0.1.2" else None,
    )
    assert release.release_candidate(counted_repo)[0] == "0.1.3"


def test_a_published_package_version_is_unavailable(counted_repo, local):
    product_commit(counted_repo, "fix(urgent): stop the bleeding")
    local.setattr(
        release,
        "pypi_files",
        lambda version: [{"filename": "wheel"}] if version == "0.1.2" else [],
    )
    assert release.release_candidate(counted_repo)[0] == "0.1.3"


@pytest.mark.parametrize("failure", ["github", "package index"])
def test_a_failing_availability_check_stops_the_proposal(
    counted_repo, local, failure
):
    product_commit(counted_repo, "fix(urgent): stop the bleeding")
    if failure == "github":
        local.setenv("GH_REPO", "owner/repo")

        def broken(tag):
            raise RuntimeError("Cannot inspect GitHub release")

        local.setattr(release, "github_release", broken)
        expected = RuntimeError
    else:

        def broken(version):
            raise urllib.error.URLError("Network is unreachable")

        local.setattr(release, "pypi_files", broken)
        expected = urllib.error.URLError
    with pytest.raises(expected):
        release.release_candidate(counted_repo)


def test_a_local_checkout_proposes_without_a_github_repository(
    counted_repo, local
):
    def refuse(tag):
        raise AssertionError("GitHub must not be consulted without GH_REPO")

    local.setattr(release, "github_release", refuse)
    product_commit(counted_repo, "fix(urgent): stop the bleeding")
    assert release.release_candidate(counted_repo)[0] == "0.1.2"


@pytest.mark.parametrize("delivered", [2, release.MINOR_THRESHOLD])
def test_candidate_phase_reports_measured_eligibility(
    counted_repo, local, capsys, delivered
):
    for number in range(delivered):
        product_commit(
            counted_repo, f"feat: work {number}", body=f"Refs #{number}"
        )
    output = counted_repo / ".git/candidate-output"
    local.setenv("GITHUB_OUTPUT", str(output))
    local.chdir(counted_repo)
    local.setattr(sys, "argv", ["release_publish", "candidate"])
    release.main()
    printed = capsys.readouterr().out.strip()
    emitted = dict(
        line.split("=", 1) for line in output.read_text().splitlines()
    )
    if delivered == 2:
        assert emitted == {
            "eligible": "false",
            "version": "",
            "issues": "2",
            "features": "2",
        }
        assert printed == (
            "2 product issues and 2 product features since v0.1.1; "
            "10 issues or 50 features or one fix(urgent) commit are required."
        )
    else:
        assert emitted == {
            "eligible": "true",
            "version": "0.2.0",
            "issues": "10",
            "features": "10",
        }
        assert printed == (
            "10 product issues and 10 product features since v0.1.1; "
            "proposing 0.2.0."
        )


@pytest.mark.parametrize(
    ("path", "eligible"),
    [
        (".github/workflows/release.yml", False),
        ("scripts/release_artifacts.py", False),
        ("docs/releases.md", False),
        ("agent_parley/cli.py", True),
        ("plugins/agent-parley/skills/coordinate/SKILL.md", True),
    ],
)
def test_package_guard_ignores_old_fix_titles_for_tooling(
    git_repo, path, eligible
):
    (git_repo / ".release-please-manifest.json").write_text('{".": "0.1.1"}')
    release.command("git", "tag", TAG, cwd=git_repo)
    changed = git_repo / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("Changed content")
    release.command("git", "add", ".", cwd=git_repo)
    release.command("git", "commit", "-m", "fix: repair tooling", cwd=git_repo)
    assert release.has_package_changes(git_repo) is eligible


@pytest.mark.parametrize("development_only", [True, False])
def test_project_metadata_is_distinct_from_development_dependencies(
    git_repo, development_only
):
    (git_repo / ".release-please-manifest.json").write_text('{".": "0.1.1"}')
    release.command("git", "tag", TAG, cwd=git_repo)
    path = git_repo / "pyproject.toml"
    addition = (
        '\n[dependency-groups]\ndev = ["pytest"]\n'
        if development_only
        else '\ndescription = "New package description"\n'
    )
    path.write_text(path.read_text() + addition)
    assert release.has_package_changes(git_repo) is not development_only


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_only_pypi_404_means_a_version_is_absent(monkeypatch, status):
    def request(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://pypi.org", status, "Failed", {}, None
        )

    monkeypatch.setattr(release.urllib.request, "urlopen", request)
    if status == 404:
        assert release.pypi_files(VERSION) == []
    else:
        with pytest.raises(urllib.error.HTTPError):
            release.pypi_files(VERSION)


def test_pypi_response_requires_the_expected_schema(monkeypatch):
    monkeypatch.setattr(
        release.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(b'{"error": "unavailable"}'),
    )
    with pytest.raises(KeyError):
        release.pypi_files(VERSION)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_only_github_404_means_a_release_is_absent(monkeypatch, status):
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, json.dumps({"status": str(status)}), "API failure"
        ),
    )
    if status == 404:
        assert release.github_release(TAG) is None
    else:
        with pytest.raises(RuntimeError, match="API failure"):
            release.github_release(TAG)


def test_published_release_without_manifest_cannot_be_rebuilt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        release, "github_release", lambda tag: {"draft": False, "assets": []}
    )
    calls = []
    monkeypatch.setattr(release, "command", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="no checksum manifest"):
        release.prepare(tmp_path, TAG)
    assert calls == []


@pytest.mark.parametrize("interrupted_after", [0, 1, 5, 6, 7])
def test_upload_retries_preserve_assets_and_commit_manifest_last(
    assets, tmp_path, monkeypatch, interrupted_after
):
    remote = {}
    created = False
    uploads = []
    failed = False

    def view(tag):
        return (
            {"draft": True, "assets": [{"name": name} for name in remote]}
            if created
            else None
        )

    def download(tag, destination):
        for name, content in remote.items():
            (destination / name).write_bytes(content)

    def command(*args, **kwargs):
        nonlocal created, failed
        if args[:3] == ("gh", "release", "create"):
            assert not created
            created = True
        elif args[:3] == ("gh", "release", "upload"):
            path = Path(args[-1])
            assert path.name not in remote
            if len(uploads) == interrupted_after and not failed:
                failed = True
                raise RuntimeError("Interrupted upload")
            remote[path.name] = path.read_bytes()
            uploads.append(path.name)
        else:
            assert args == ("make", "release-artifacts")
        return ""

    monkeypatch.setattr(release, "github_release", view)
    monkeypatch.setattr(release, "download", download)
    monkeypatch.setattr(release, "command", command)
    if interrupted_after < 7:
        with pytest.raises(RuntimeError, match="Interrupted"):
            release.prepare(tmp_path, TAG)
    checksum = release.prepare(tmp_path, TAG)
    assert checksum == release.digest(assets / "SHA256SUMS")
    assert uploads[-1] == "SHA256SUMS"
    assert len(uploads) == 7
    (assets / "CHANGELOG.md").write_text("A later rebuild differs")
    assert release.prepare(tmp_path, TAG) == checksum
    assert len(uploads) == 7


def test_partial_draft_with_conflicting_bytes_is_not_overwritten(
    assets, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        release,
        "github_release",
        lambda tag: {
            "draft": True,
            "assets": [{"name": "CHANGELOG.md"}],
        },
    )
    monkeypatch.setattr(
        release,
        "download",
        lambda tag, directory: (directory / "CHANGELOG.md").write_text(
            "Conflicting content"
        ),
    )
    calls = []
    monkeypatch.setattr(
        release, "command", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(ValueError, match="differs"):
        release.prepare(tmp_path, TAG)
    assert calls == [("make", "release-artifacts")]


@pytest.mark.parametrize(
    "state",
    ["complete", "missing", "conflict", "changed_assets", "changed_tag"],
)
def test_github_publication_requires_verified_pypi_completion(
    assets, tmp_path, monkeypatch, state
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_TAG", TAG)
    monkeypatch.setenv("RELEASE_SOURCE", "expected")
    monkeypatch.setenv(
        "RELEASE_CHECKSUM", release.digest(assets / "SHA256SUMS")
    )
    monkeypatch.setattr(sys, "argv", ["release_publish", "finish"])
    monkeypatch.setattr(
        release,
        "validate_tag",
        lambda *args: "changed" if state == "changed_tag" else "expected",
    )
    monkeypatch.setattr(
        release,
        "download",
        lambda tag, directory: shutil.copytree(
            assets, directory, dirs_exist_ok=True
        ),
    )
    metadata = pypi_metadata(assets)
    if state == "missing":
        metadata.pop()
    elif state == "conflict":
        metadata[0]["digests"]["sha256"] = "0" * 64
    elif state == "changed_assets":
        (assets / "CHANGELOG.md").write_text("Changed")
    monkeypatch.setattr(release, "pypi_files", lambda version: metadata)
    calls = []
    monkeypatch.setattr(release, "command", lambda *args: calls.append(args))
    if state == "complete":
        release.main()
        assert calls == [
            ("gh", "release", "edit", TAG, "--draft=false", "--latest")
        ]
    else:
        with pytest.raises(ValueError):
            release.main()
        assert calls == []


def test_workflow_cannot_recurse_or_publish_from_an_automatic_event():
    root = Path(__file__).resolve().parents[1]
    for filename in ("release.yml", "release-please.yml"):
        text = (root / ".github" / "workflows" / filename).read_text()
        workflow = yaml.safe_load(text)
        assert set(workflow[True]) == {"workflow_dispatch"}
        assert "gh pr merge" not in text
        assert "createReview" not in text
        assert "skip-existing" not in text
    preparation = yaml.safe_load(
        (root / ".github/workflows/release-please.yml").read_text()
    )
    action = next(
        step
        for step in preparation["jobs"]["prepare"]["steps"]
        if step.get("id") == "release"
    )
    assert action["with"]["skip-github-release"] is True
    publication = yaml.safe_load(
        (root / ".github/workflows/release.yml").read_text()
    )
    assert "id-token" not in publication["jobs"]["build"]["permissions"]
    assert publication["jobs"]["publish"]["needs"] == "build"
    config = json.loads((root / "release-please-config.json").read_text())
    assert release.migration_config_errors(root) == []
    assert "last-release-sha" not in config["packages"]["."]
