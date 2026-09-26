"""Checks the GitHub Pages workflow and the site configuration it builds."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_site_config_sets_canonical_address_sitemap_and_page_metadata():
    config = yaml.safe_load((ROOT / "_config.yml").read_text())
    assert config["url"] == "https://suneel944.github.io"
    assert config["baseurl"] == "/agent-parley"
    assert {"jekyll-seo-tag", "jekyll-sitemap"} <= set(config["plugins"])
    described = {
        entry["scope"]["path"]
        for entry in config["defaults"]
        if entry["values"].get("title") and entry["values"].get("description")
    }
    pages = {f"docs/{page.name}" for page in (ROOT / "docs").glob("*.md")}
    assert pages | {"README.md"} <= described


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
                assert len(step["uses"].split("@", 1)[1]) == 40
