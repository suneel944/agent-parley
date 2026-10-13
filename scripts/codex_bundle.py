"""Builds and checks the skills-only archive the Codex plugin directory reviews.

The Claude plugin directory reads the repository, so the Claude manifest in
``.claude-plugin`` must stay one that ``claude plugin validate --strict``
accepts. The Codex plugin directory takes an uploaded archive instead, reads
the Claude manifest inside it, and requires the listing fields it converts
into ``.codex-plugin/plugin.json``: an ``interface`` block with a short
description that also serves as the 30 character listing subtitle, a logo
and a composer icon that resolve to square images inside the archive, one of
the portal's supported categories, and the website, support, privacy policy
and terms URLs the portal shows and checks. The portal could not confirm the
``Productivity`` category for a tool that coordinates coding agents, so the
listing declares ``Developer Tools``.
Those fields live in the repository's Codex manifest, and this module merges
them into the archived Claude manifest so neither directory's format leaks
into the other's.

The portal also rejects a listing whose name or description references
another AI assistant, model or platform. The Claude manifest's keywords name
the supported agent command line tools, so the archive takes its keywords
from the Codex manifest instead, and the check rejects any archived manifest
or listing skill text that names an assistant.

The portal scans each archived skill and flagged the repository's
``coordinate`` skill as a security risk once it carried operator,
integration, permission and credential guidance. The archive therefore packs
the shorter skills under ``.codex-plugin/listing-skills``, which keep the
repository skills' names and descriptions and leave that guidance to the
repository skills and the launcher's injected protocol.
"""

import json
import re
import struct
import tomllib
import zipfile
from pathlib import Path

from scripts.release_artifacts import add_entry

PLUGIN_DIRECTORY = Path("plugins") / "agent-parley"
LISTING_SKILLS = Path(".codex-plugin") / "listing-skills"
SHORT_DESCRIPTION_LIMIT = 30
IMAGE_EDGE_MINIMUM = 1024
PROMPT_LIMIT = 3
PROMPT_LENGTH_LIMIT = 128
IMAGE_FIELDS = ("logo", "composerIcon")
URL_FIELDS = (
    "websiteURL",
    "supportURL",
    "privacyPolicyURL",
    "termsOfServiceURL",
)
CATEGORIES = (
    "Productivity",
    "Creativity",
    "Developer Tools",
    "Business & Operations",
    "Data & Analytics",
    "Communication",
    "Education & Research",
    "Security",
    "Finance",
    "Healthcare",
    "Travel",
    "Entertainment",
    "Other",
)
ASSISTANT_NAMES = re.compile(
    r"\b(anthropic|chatgpt|claude|codex|copilot|cursor|gemini|gpt|openai)\b",
    re.IGNORECASE,
)
CLAUDE_LISTING_FIELDS = (
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
)


def add_bytes(archive: zipfile.ZipFile, data: bytes, name: str) -> None:
    """Stores generated content with the same fixed metadata as a file entry.

    Args:
        archive: Open bundle receiving the entry.
        data: Content to store.
        name: Path recorded inside the bundle.
    """
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o644 << 16
    archive.writestr(entry, data)


def png_size(data: bytes) -> tuple[int, int]:
    """Reads the pixel dimensions from a PNG header.

    Args:
        data: Complete or leading bytes of the image file.

    Returns:
        Width and height in pixels.

    Raises:
        ValueError: If the bytes do not start with a PNG signature and header.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG image")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def load_manifests(root: Path) -> tuple[dict, dict]:
    """Reads both plugin manifests from the repository.

    Args:
        root: Repository root.

    Returns:
        The Claude manifest and the Codex manifest, in that order.

    Raises:
        OSError: If either manifest is missing.
        ValueError: If either manifest is not valid JSON.
    """
    plugin = root / PLUGIN_DIRECTORY
    return tuple(
        json.loads((plugin / f".{client}-plugin" / "plugin.json").read_text())
        for client in ("claude", "codex")
    )


def submission_manifest(root: Path) -> dict:
    """Builds the manifest the Codex directory reads from the archive.

    Args:
        root: Repository root.

    Returns:
        The Claude manifest with the Codex ``keywords`` and ``interface``
        merged in.
    """
    claude, codex = load_manifests(root)
    return {
        **claude,
        "keywords": codex["keywords"],
        "interface": codex["interface"],
    }


def manifest_errors(root: Path) -> list[str]:
    """Checks that both manifests keep the shape their directory demands.

    Args:
        root: Repository root.

    Returns:
        Human-readable problems; empty when both manifests are acceptable.
    """
    claude, codex = load_manifests(root)
    plugin = root / PLUGIN_DIRECTORY
    errors = []
    if "interface" in claude:
        errors.append(
            "Claude manifest must not carry interface; "
            "listing fields belong in .codex-plugin/plugin.json"
        )
    for field in CLAUDE_LISTING_FIELDS:
        if not claude.get(field):
            errors.append(f"Claude manifest is missing {field}")
    interface = codex.get("interface", {})
    short = interface.get("shortDescription", "")
    if not short:
        errors.append("Codex manifest is missing interface.shortDescription")
    elif len(short) > SHORT_DESCRIPTION_LIMIT:
        errors.append(
            "Codex interface.shortDescription exceeds "
            f"{SHORT_DESCRIPTION_LIMIT} characters"
        )
    for field in IMAGE_FIELDS:
        reference = interface.get(field, "")
        if not reference.startswith("./assets/"):
            errors.append(f"Codex interface.{field} must point under ./assets/")
            continue
        image = plugin / reference
        if not image.is_file():
            errors.append(f"Codex interface.{field} file is missing")
            continue
        try:
            width, height = png_size(image.read_bytes())
        except ValueError:
            errors.append(f"Codex interface.{field} must be a PNG image")
            continue
        if width != height:
            errors.append(f"Codex interface.{field} must be square")
        elif width < IMAGE_EDGE_MINIMUM:
            errors.append(
                f"Codex interface.{field} must be at least "
                f"{IMAGE_EDGE_MINIMUM} pixels square"
            )
    prompts = interface.get("defaultPrompt", [])
    if not isinstance(prompts, list) or not prompts:
        errors.append("Codex interface.defaultPrompt must be a list of prompts")
    elif len(prompts) > PROMPT_LIMIT:
        errors.append(
            f"Codex interface.defaultPrompt keeps at most {PROMPT_LIMIT} "
            "prompts; later entries are dropped by the directory"
        )
    elif any(
        not isinstance(prompt, str) or not prompt.strip() for prompt in prompts
    ):
        errors.append("Codex interface.defaultPrompt holds an empty prompt")
    elif any(len(prompt) > PROMPT_LENGTH_LIMIT for prompt in prompts):
        errors.append(
            "Codex interface.defaultPrompt exceeds "
            f"{PROMPT_LENGTH_LIMIT} characters"
        )
    if not interface.get("capabilities"):
        errors.append("Codex manifest is missing interface.capabilities")
    if interface.get("category") not in CATEGORIES:
        errors.append(
            "Codex interface.category must be one of the portal's categories"
        )
    for field in URL_FIELDS:
        if not interface.get(field, "").startswith("https://"):
            errors.append(f"Codex interface.{field} must be an https URL")
    errors.extend(listing_skill_errors(plugin))
    texts = [json.dumps(submission_manifest(root))] + [
        path.read_text() for path in (plugin / LISTING_SKILLS).glob("*/*.md")
    ]
    names = sorted(
        {
            match.lower()
            for text in texts
            for match in ASSISTANT_NAMES.findall(text)
        }
    )
    if names:
        errors.append(
            "Codex listing names another AI assistant: " + ", ".join(names)
        )
    return errors


def listing_skill_errors(plugin: Path) -> list[str]:
    """Checks that the listing skills mirror the repository skills' headers.

    Args:
        plugin: Plugin directory holding both skill trees.

    Returns:
        One message per skill missing from either tree or whose frontmatter
        differs between them.
    """
    trees = [plugin / "skills", plugin / LISTING_SKILLS]
    repository, listing = (
        {path.parent.name: path for path in tree.glob("*/SKILL.md")}
        for tree in trees
    )
    errors = [
        f"listing skill {name} is missing or has no repository skill"
        for name in sorted(repository.keys() ^ listing.keys())
    ]
    for name in sorted(repository.keys() & listing.keys()):
        headers = [
            path.read_text().split("---")[1]
            for path in (repository[name], listing[name])
        ]
        if headers[0] != headers[1]:
            errors.append(f"listing skill {name} frontmatter differs")
    return errors


def build(root: Path, output: Path) -> Path:
    """Writes the submission archive for the current package version.

    The archive holds only what the skills-only path accepts: the merged
    manifest, the listing skills stored as ``skills`` and the listing images.
    Marketplace manifests, the Codex manifest, the repository skills and the
    plugin README stay out.

    Args:
        root: Repository root.
        output: Directory receiving the archive; created when missing.

    Returns:
        Path of the written archive.

    Raises:
        ValueError: If a manifest fails the directory checks.
        OSError: If an input cannot be read or the archive cannot be written.
    """
    errors = manifest_errors(root)
    if errors:
        raise ValueError("\n".join(errors))
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "version"
    ]
    plugin = root / PLUGIN_DIRECTORY
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / f"agent-parley-{version}-codex-skills.zip"
    manifest = json.dumps(submission_manifest(root), indent=2) + "\n"
    sources = {"skills": plugin / LISTING_SKILLS, "assets": plugin / "assets"}
    inputs = sorted(
        (f"{name}/{path.relative_to(source).as_posix()}", path)
        for name, source in sources.items()
        for path in source.rglob("*")
        if path.is_file()
    )
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        add_bytes(archive, manifest.encode(), ".claude-plugin/plugin.json")
        for name, path in inputs:
            add_entry(archive, path, name)
    return bundle


def main() -> None:
    """Builds the archive under ``dist`` and prints where it landed."""
    root = Path(__file__).resolve().parents[1]
    print(f"Codex submission archive: {build(root, root / 'dist')}")


if __name__ == "__main__":
    main()
