"""Stages the README and documentation as a GitHub Pages source tree.

The Pages workflow builds the staged tree with the GitHub Pages Jekyll build.
The README becomes the site index, every Markdown page gains front matter
with a title and a description for the search engine metadata that
``jekyll-seo-tag`` renders, and the site configuration sets the canonical
address and enables the sitemap. Page bodies are wrapped in a Liquid ``raw``
block so template syntax quoted in the documentation is published verbatim.
"""

import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

SITE_URL = "https://suneel944.github.io"
BASE_URL = "/agent-parley"
SITE_TITLE = "Agent Parley"
PLUGINS = ("jekyll-seo-tag", "jekyll-sitemap", "jekyll-relative-links")
DESCRIPTION_LIMIT = 160
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
SKIPPED_PREFIXES = ("<", "#", "|", "```", "-", "*", ">", "!")


def project_description(root: Path) -> str:
    """Returns the package description declared in ``pyproject.toml``.

    Args:
        root: Repository root.

    Returns:
        The ``project.description`` value.
    """
    data = tomllib.loads((root / "pyproject.toml").read_text())
    return str(data["project"]["description"])


def page_title(text: str, fallback: str) -> str:
    """Returns the first level-one heading of a Markdown page.

    Args:
        text: Markdown source.
        fallback: Title used when the page has no level-one heading.

    Returns:
        The heading text, or ``fallback``.
    """
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def page_description(text: str, fallback: str) -> str:
    """Returns the first prose paragraph of a page as plain text.

    Headings, HTML, tables, lists, quotes, images and code fences are skipped.
    Link markup is reduced to its text, and the result is shortened at a word
    boundary to the length search engines display.

    Args:
        text: Markdown source.
        fallback: Description used when the page has no prose paragraph.

    Returns:
        A single-line description of at most 160 characters.
    """
    paragraph: list[str] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if not paragraph and stripped.startswith(SKIPPED_PREFIXES):
            continue
        paragraph.append(stripped)
    plain = LINK.sub(r"\1", " ".join(paragraph)).replace("`", "")
    plain = " ".join(plain.replace("**", "").split()) or fallback
    if len(plain) <= DESCRIPTION_LIMIT:
        return plain
    cut = plain[: DESCRIPTION_LIMIT - 3].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "..."


def with_front_matter(text: str, title: str, description: str) -> str:
    """Returns a page with front matter and a Liquid-safe body.

    A page that already starts with front matter keeps it unchanged.

    Args:
        text: Markdown source.
        title: Page title.
        description: Page description.

    Returns:
        The page source ready for the Jekyll build.
    """
    if text.startswith("---\n"):
        return text
    header = (
        f"---\ntitle: {json.dumps(title)}\n"
        f"description: {json.dumps(description)}\n---\n"
    )
    return f"{header}{{% raw %}}\n{text}\n{{% endraw %}}\n"


def site_config(description: str) -> str:
    """Returns the Jekyll ``_config.yml`` for the site.

    Args:
        description: Site description.

    Returns:
        The configuration as YAML text.
    """
    plugins = "".join(f"  - {name}\n" for name in PLUGINS)
    return (
        f"title: {json.dumps(SITE_TITLE)}\n"
        f"description: {json.dumps(description)}\n"
        f"url: {json.dumps(SITE_URL)}\n"
        f"baseurl: {json.dumps(BASE_URL)}\n"
        f"plugins:\n{plugins}"
    )


def stage(root: Path, output: Path) -> Path:
    """Stages the site source tree.

    Args:
        root: Repository root.
        output: Directory to create; it must not exist yet.

    Returns:
        The staged directory.
    """
    description = project_description(root)
    shutil.copytree(root / "docs", output / "docs")
    for page in sorted((output / "docs").rglob("*.md")):
        text = page.read_text()
        page.write_text(
            with_front_matter(
                text,
                page_title(text, page.stem),
                page_description(text, description),
            )
        )
    readme = (root / "README.md").read_text()
    (output / "index.md").write_text(
        with_front_matter(readme, SITE_TITLE, description)
    )
    (output / "_config.yml").write_text(site_config(description))
    return output


def main() -> None:
    """Stages the site into the directory named on the command line."""
    stage(Path(__file__).resolve().parents[1], Path(sys.argv[1]))


if __name__ == "__main__":
    main()
