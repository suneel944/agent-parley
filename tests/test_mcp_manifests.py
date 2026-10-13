import json
import shutil
import tomllib
from pathlib import Path

import pytest

from scripts import check_mcp_manifests as manifests

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    for name in ("pyproject.toml", "server.json", "glama.json"):
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def edit(root, name, change):
    path = root / name
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def test_checked_in_manifests_are_valid():
    assert manifests.manifest_errors(ROOT) == []


def test_registry_version_is_the_release_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    server = json.loads((ROOT / "server.json").read_text())
    assert server["version"] == project["project"]["version"]


def test_version_drift_fails(checkout):
    edit(checkout, "server.json", lambda data: data.update(version="9.9.9"))
    assert manifests.manifest_errors(checkout) == [
        "server.json: version differs from pyproject.toml"
    ]


def test_every_listing_names_the_launcher(checkout):
    edit(
        checkout,
        "server.json",
        lambda data: data.update(description="A coordination server."),
    )
    assert manifests.manifest_errors(checkout) == [
        "server.json: description must name `agent-parley run`"
    ]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            lambda data: data.pop("name"),
            "server.json: name must be a string of 3-200 chars",
        ),
        (
            lambda data: data.update(description="x" * 101),
            "server.json: description must be a string of 1-100 chars",
        ),
        (
            lambda data: data.update(extra=1),
            "server.json: field extra is not in the registry schema",
        ),
        (
            lambda data: data["repository"].pop("source"),
            "server.json: repository.source is required",
        ),
        (
            lambda data: data.update(packages={}),
            "server.json: packages must be an array",
        ),
    ],
)
def test_registry_schema_rules(checkout, change, error):
    edit(checkout, "server.json", change)
    assert error in manifests.manifest_errors(checkout)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            lambda data: data.pop("maintainers"),
            "glama.json: maintainers must be unique strings",
        ),
        (
            lambda data: data.update(maintainers=["a", "a"]),
            "glama.json: maintainers must be unique strings",
        ),
        (
            lambda data: data.update(owner="a"),
            "glama.json: field owner is not in the Glama schema",
        ),
    ],
)
def test_glama_schema_rules(checkout, change, error):
    edit(checkout, "glama.json", change)
    assert error in manifests.manifest_errors(checkout)


@pytest.mark.parametrize("leak", ["/home/someone/x", "ghp_abc", "Bearer x"])
def test_credentials_and_private_paths_are_refused(checkout, leak):
    edit(checkout, "glama.json", lambda data: data.update(note=leak))
    errors = manifests.manifest_errors(checkout)
    assert any("holds a credential or path" in error for error in errors)


def test_a_missing_manifest_fails(checkout):
    (checkout / "glama.json").unlink()
    assert manifests.manifest_errors(checkout) == ["glama.json is missing"]
