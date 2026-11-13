"""Validates the MCP directory manifests against each directory's schema.

Two directories read a file from this repository. The official MCP registry
reads `server.json`, checked against the rules of
https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json,
the version its documentation named when fetched on 2026-09-30. Glama reads
`glama.json`, checked against https://glama.ai/mcp/schemas/server.json as
fetched on the same date. Neither schema is vendored: the checks below restate
the required fields, types, lengths and patterns this project's manifests use,
and reject any field the schema does not define, so an unexpected field fails
here rather than at submission. Smithery and mcp.so read no repository file;
`docs/mcp-directories.md` records why.

Every manifest must also carry the release version from `pyproject.toml`,
say that the server runs only under `agent-parley run`, and hold no
credential or private path.
"""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/"
    "server.schema.json"
)
GLAMA_SCHEMA = "https://glama.ai/mcp/schemas/server.json"
SERVER_NAME = "io.github.suneel944/agent-parley"
MAINTAINER = "suneel944"
LAUNCH_NOTICE = "agent-parley run"
REGISTRY_FIELDS = frozenset(
    {
        "$schema",
        "_meta",
        "description",
        "icons",
        "name",
        "packages",
        "remotes",
        "repository",
        "title",
        "version",
        "websiteUrl",
    }
)
REPOSITORY_FIELDS = frozenset({"id", "source", "subfolder", "url"})
GLAMA_FIELDS = frozenset({"$schema", "maintainers"})
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
URL_PATTERN = re.compile(r"^https://[^\s]+$")
PRIVATE = re.compile(
    r"/home/|/Users/|/tmp/|[A-Za-z]:\\\\|token|secret|password|bearer"
    r"|ghp_|github_pat_|pypi-",
    re.IGNORECASE,
)


def text_errors(
    manifest: str, data: dict[str, Any], field: str, limits: tuple[int, int]
) -> list[str]:
    """Checks one string field against its schema length bounds.

    Args:
        manifest: File name used in the error text.
        data: Parsed manifest object.
        field: Field to check.
        limits: Inclusive minimum and maximum length.

    Returns:
        One error when the field is not a string within the bounds.
    """
    value = data.get(field)
    low, high = limits
    if not isinstance(value, str) or not low <= len(value) <= high:
        return [f"{manifest}: {field} must be a string of {low}-{high} chars"]
    return []


def registry_errors(data: dict[str, Any], version: str) -> list[str]:
    """Checks `server.json` against the MCP registry schema rules.

    Args:
        data: Parsed `server.json` object.
        version: Release version from `pyproject.toml`.

    Returns:
        Every rule the manifest breaks.
    """
    name = "server.json"
    errors = [
        f"{name}: field {field} is not in the registry schema"
        for field in sorted(set(data) - REGISTRY_FIELDS)
    ]
    if data.get("$schema") != REGISTRY_SCHEMA:
        errors.append(f"{name}: $schema must be {REGISTRY_SCHEMA}")
    errors += text_errors(name, data, "name", (3, 200))
    if not NAME_PATTERN.match(str(data.get("name", ""))):
        errors.append(f"{name}: name must match {NAME_PATTERN.pattern}")
    if data.get("name") != SERVER_NAME:
        errors.append(f"{name}: name must be {SERVER_NAME}")
    errors += text_errors(name, data, "description", (1, 100))
    if "title" in data:
        errors += text_errors(name, data, "title", (1, 100))
    errors += text_errors(name, data, "version", (1, 255))
    if data.get("version") != version:
        errors.append(f"{name}: version differs from pyproject.toml")
    if "websiteUrl" in data and not URL_PATTERN.match(str(data["websiteUrl"])):
        errors.append(f"{name}: websiteUrl must be an https URL")
    repository = data.get("repository")
    if repository is not None:
        if not isinstance(repository, dict):
            errors.append(f"{name}: repository must be an object")
        else:
            errors += [
                f"{name}: repository.{field} is not in the registry schema"
                for field in sorted(set(repository) - REPOSITORY_FIELDS)
            ]
            for field in ("url", "source"):
                if not isinstance(repository.get(field), str):
                    errors.append(f"{name}: repository.{field} is required")
            if not URL_PATTERN.match(str(repository.get("url", ""))):
                errors.append(f"{name}: repository.url must be an https URL")
    for field in ("packages", "remotes", "icons"):
        if field in data and not isinstance(data[field], list):
            errors.append(f"{name}: {field} must be an array")
    if LAUNCH_NOTICE not in str(data.get("description", "")):
        errors.append(f"{name}: description must name `{LAUNCH_NOTICE}`")
    return errors


def glama_errors(data: dict[str, Any]) -> list[str]:
    """Checks `glama.json` against the Glama server schema rules.

    Args:
        data: Parsed `glama.json` object.

    Returns:
        Every rule the manifest breaks.
    """
    name = "glama.json"
    errors = [
        f"{name}: field {field} is not in the Glama schema"
        for field in sorted(set(data) - GLAMA_FIELDS)
    ]
    if data.get("$schema") != GLAMA_SCHEMA:
        errors.append(f"{name}: $schema must be {GLAMA_SCHEMA}")
    maintainers = data.get("maintainers")
    if (
        not isinstance(maintainers, list)
        or not all(isinstance(entry, str) for entry in maintainers)
        or len(set(maintainers)) != len(maintainers)
    ):
        errors.append(f"{name}: maintainers must be unique strings")
    elif MAINTAINER not in maintainers:
        errors.append(f"{name}: maintainers must include {MAINTAINER}")
    return errors


def manifest_errors(root: Path) -> list[str]:
    """Validates every MCP directory manifest in a checkout.

    Args:
        root: Repository root holding `pyproject.toml` and the manifests.

    Returns:
        Every schema, version, notice and privacy rule a manifest breaks.
    """
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "version"
    ]
    errors: list[str] = []
    for name in ("server.json", "glama.json"):
        path = root / name
        if not path.exists():
            errors.append(f"{name} is missing")
            continue
        text = path.read_text()
        if match := PRIVATE.search(text):
            errors.append(f"{name}: holds a credential or path: {match[0]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            errors.append(f"{name}: invalid JSON: {error}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{name}: must be a JSON object")
            continue
        errors += (
            registry_errors(data, version)
            if name == "server.json"
            else glama_errors(data)
        )
    return errors


def main() -> None:
    """Exits non-zero with every manifest error, or reports success."""
    errors = manifest_errors(Path(__file__).resolve().parents[1])
    if errors:
        raise SystemExit("\n".join(errors))
    print("MCP directory manifests: server.json, glama.json valid")


if __name__ == "__main__":
    main()
