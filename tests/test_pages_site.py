"""Checks the staged GitHub Pages source and the workflow that builds it."""

from pathlib import Path

import yaml

from scripts import pages_site

ROOT = Path(__file__).resolve().parents[1]


def front_matter(text):
    assert text.startswith("---\n")
    header, body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(header), body


def test_stage_publishes_readme_and_every_doc_with_metadata(tmp_path):
    site = pages_site.stage(ROOT, tmp_path / "site")
    config = yaml.safe_load((site / "_config.yml").read_text())
    assert config["url"] == "https://suneel944.github.io"
    assert config["baseurl"] == "/agent-parley"
    assert {"jekyll-seo-tag", "jekyll-sitemap"} <= set(config["plugins"])
    meta, body = front_matter((site / "index.md").read_text())
    assert meta["title"] == "Agent Parley"
    assert meta["description"] == config["description"]
    assert body.startswith("{% raw %}\n")
    assert body.rstrip().endswith("{% endraw %}")
    sources = sorted(p.name for p in (ROOT / "docs").glob("*.md"))
    staged = sorted(p.name for p in (site / "docs").glob("*.md"))
    assert staged == sources
    for page in (site / "docs").glob("*.md"):
        meta, _ = front_matter(page.read_text())
        assert meta["title"]
        assert 0 < len(meta["description"]) <= 160
    assert (site / "docs/assets/agent-parley.png").is_file()


def test_page_metadata_comes_from_heading_and_first_paragraph():
    text = (
        "# Running lanes\n\n<p>logo</p>\n\n```sh\nx\n```\n\n"
        "See [Commands](commands.md) for `lane` usage.\nMore.\n\nNext."
    )
    assert pages_site.page_title(text, "x") == "Running lanes"
    assert pages_site.page_description(text, "x") == (
        "See Commands for lane usage. More."
    )
    assert pages_site.page_title("no heading", "fallback") == "fallback"
    assert pages_site.page_description("# Only", "fallback") == "fallback"


def test_long_description_is_cut_at_a_word_boundary():
    text = "# T\n\n" + "word " * 100
    description = pages_site.page_description(text, "x")
    assert len(description) <= 160
    assert description.endswith("word...")


def test_existing_front_matter_is_kept_and_titles_are_quoted():
    text = "---\ntitle: Kept\n---\nbody\n"
    assert pages_site.with_front_matter(text, "a", "b") == text
    staged = pages_site.with_front_matter("x", 'A: "b"', "c: d")
    meta, _ = front_matter(staged)
    assert meta == {"title": 'A: "b"', "description": "c: d"}


def test_pages_workflow_deploys_from_main_with_pinned_actions():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/pages.yml").read_text()
    )
    triggers = workflow[True]
    assert triggers["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["group"] == "pages"
    deploy = workflow["jobs"]["deploy"]
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                reference = step["uses"].split("@", 1)[1]
                assert len(reference) == 40
