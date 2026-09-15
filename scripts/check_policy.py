"""Enforces documentation, dependency, and release metadata boundaries."""

import ast
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path

from agent_parley.policy import has_attribution
from scripts import codex_bundle, release_publish

TOLERATED_WARNINGS = frozenset({"protocol"})

VALIDATOR_FAILED = 2

README_BLOB_PREFIX = "https://github.com/suneel944/agent-parley/blob/main/"

README_LINK = re.compile(r"\]\(([^)\s]+)\)|href=\"([^\"]+)\"")


def contribution_errors(root: Path) -> list[str]:
    """Checks tracked text and commit messages for prohibited credits.

    Args:
        root: Repository root; source archives without Git metadata are skipped.

    Returns:
        Paths or commit IDs containing prohibited attribution.
    """
    if not (root / ".git").exists():
        return []
    files = (
        subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=10,
        )
        .stdout.decode()
        .split("\0")
    )
    errors = []
    for filename in filter(None, files):
        path = root / filename
        if path.is_file() and has_attribution(
            path.read_bytes().decode("utf-8", errors="ignore")
        ):
            errors.append(f"{filename}: prohibited attribution")
    history = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x00"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.split("\0")
    for index in range(0, len(history) - 1, 2):
        if has_attribution(history[index + 1]):
            errors.append(f"Commit {history[index].strip()}: prohibited credit")
    return errors


def readme_link_errors(root: Path) -> list[str]:
    """Checks that every README link also resolves off GitHub.

    PyPI renders README.md as the project description and resolves a
    relative target against pypi.org, so a repository-relative link is a
    dead link there. Only absolute URLs and in-page anchors survive both
    renderings, and an absolute link into this repository must still name
    a file that exists.

    Args:
        root: Repository root holding README.md.

    Returns:
        One message per link target that PyPI cannot resolve.
    """
    errors = []
    for line, text in enumerate(
        (root / "README.md").read_text().splitlines(), 1
    ):
        for markdown, html in README_LINK.findall(text):
            target = markdown or html
            if target.startswith(("#", "mailto:")):
                continue
            if not target.startswith(("https://", "http://")):
                errors.append(
                    f"README.md:{line}: relative link {target!r} breaks on "
                    "PyPI; use an absolute URL"
                )
            elif target.startswith(README_BLOB_PREFIX):
                path = target[len(README_BLOB_PREFIX) :].split("#")[0]
                if not (root / path).exists():
                    errors.append(
                        f"README.md:{line}: link target {path!r} does not exist"
                    )
    return errors


def frontmatter(text: str) -> dict[str, str]:
    """Returns the scalar fields of a leading document block.

    Args:
        text: Document whose optional leading block is read.

    Returns:
        The block's top level fields, empty when the document opens with
        something other than a block delimiter.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key == key.lstrip():
            fields[key.strip()] = value.strip()
    return fields


def skill_errors(plugin: Path) -> list[str]:
    """Checks that every shipped skill directory registers a usable skill.

    The plugin validator reports commands but never lists skills, so a
    skills-only plugin validates with an empty component list and a missing or
    undeclared skill document passes unseen. This covers locally what the
    validator leaves out.

    Args:
        plugin: Plugin directory holding the optional skills tree.

    Returns:
        One message per skill directory without a readable document declaring
        both a name and a description.
    """
    errors = []
    directories = sorted(
        path for path in (plugin / "skills").glob("*") if path.is_dir()
    )
    for directory in directories:
        document = directory / "SKILL.md"
        try:
            declared = frontmatter(document.read_text())
        except OSError:
            errors.append(f"{directory.name}: skill has no readable SKILL.md")
            continue
        missing = [
            field
            for field in ("name", "description")
            if not declared.get(field)
        ]
        if missing:
            errors.append(
                f"{directory.name}: skill declares no {' or '.join(missing)}"
            )
    return errors


def reported_errors(section: dict) -> list[str]:
    """Returns one validated section's errors and unexpected warnings.

    Args:
        section: Manifest or component report the validator produced.

    Returns:
        Every reported error, and every warning whose path is outside the
        tolerated set.
    """
    where = section.get("file") or "plugin"
    entries = list(section.get("errors") or [])
    entries += [
        warning
        for warning in section.get("warnings") or []
        if warning.get("path") not in TOLERATED_WARNINGS
    ]
    return [f"{where}: {entry.get('message', entry)}" for entry in entries]


def plugin_validation_errors(root: Path) -> list[str]:
    """Reports what the plugin runtime loader would refuse to load.

    The validator runs without ``--strict`` and its warnings are judged here,
    because the manifest declares the ``protocol`` field that the
    compatibility contract reads and the client reports every field it does
    not recognise as a warning. Tolerating exactly that one path keeps a
    second unknown field a failure rather than an inherited allowance. A
    machine without the client installed skips the step with a message
    instead of passing silently, and a validation run that itself fails is
    reported separately from a plugin that fails.

    Args:
        root: Repository root holding the plugin directory.

    Returns:
        One message per reported error, per warning outside the tolerated
        set, and per skill directory without a usable document.
    """
    plugin = root / "plugins/agent-parley"
    errors = skill_errors(plugin)
    executable = shutil.which("claude")
    if not executable:
        print("Policy: plugin validation skipped, no claude client")
        return errors
    try:
        result = subprocess.run(
            [executable, "plugin", "validate", str(plugin), "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [*errors, f"Plugin validation could not run: {error}"]
    if result.returncode == VALIDATOR_FAILED:
        detail = result.stderr.strip() or "no detail reported"
        return [*errors, f"Plugin validation did not complete: {detail}"]
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return [*errors, "Plugin validation returned no readable report"]
    sections = [report.get("manifest") or {}, *(report.get("contents") or [])]
    for section in sections:
        errors.extend(reported_errors(section))
    return errors


def main() -> None:
    """Rejects undocumented code, inline comments and runtime dependencies."""
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    errors = contribution_errors(root) + readme_link_errors(root)
    for message in sys.argv[1:]:
        if has_attribution(Path(message).read_text()):
            errors.append("Commit message contains prohibited attribution.")
    if metadata["dependencies"]:
        errors.append("Runtime dependencies must remain empty.")
    for directory in ("agent_parley", "scripts"):
        for path in sorted((root / directory).glob("*.py")):
            text = path.read_text()
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (
                        ast.Module,
                        ast.ClassDef,
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                    ),
                ) and not ast.get_docstring(node):
                    errors.append(
                        f"{path.name}:{getattr(node, 'lineno', 1)}: "
                        "missing docstring"
                    )
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    allowed = sys.stdlib_module_names | {"agent_parley"}
                    if directory == "scripts":
                        allowed |= {"scripts"}
                    if name.split(".")[0] not in allowed:
                        errors.append(f"{path.name}: non-stdlib import {name}")
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT:
                    errors.append(
                        f"{path.name}:{token.start[0]}: "
                        "use a docstring, not an inline comment"
                    )
    for client in ("codex", "claude"):
        path = root / "plugins/agent-parley" / f".{client}-plugin/plugin.json"
        if json.loads(path.read_text())["version"] != metadata["version"]:
            errors.append(f"{client} plugin version differs from package")
    errors.extend(codex_bundle.manifest_errors(root))
    errors.extend(plugin_validation_errors(root))
    marketplace = json.loads(
        (root / ".claude-plugin/marketplace.json").read_text()
    )
    if marketplace["plugins"][0]["version"] != metadata["version"]:
        errors.append("Claude marketplace version differs from package")
    manifest = root / release_publish.MANIFEST_PATH
    if manifest.exists():
        if json.loads(manifest.read_text())["."] != metadata["version"]:
            errors.append("Release manifest version differs from package")
        errors.extend(release_publish.release_history_errors(root))
    elif (root / ".git").exists():
        errors.append("Release manifest is missing")
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = [
        p["version"] for p in lock["package"] if p["name"] == metadata["name"]
    ]
    if locked != [metadata["version"]]:
        errors.append("Locked project version differs from package")
    served = (root / "agent_parley/server.py").read_text()
    if f'"{metadata["name"]}"' not in served:
        errors.append(
            "server.py must report the distribution name from pyproject.toml"
        )
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        "Policy: documented code, no inline comments, stdlib runtime, "
        "aligned versions, directory-ready manifests, portable README links"
    )


if __name__ == "__main__":
    main()
