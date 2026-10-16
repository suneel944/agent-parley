"""Bounds the fixed context every lane pays for the skill and the tools."""

import json
import re
from pathlib import Path

import pytest

from agent_parley.server import TOOLS

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/agent-parley"
SKILLS = (
    PLUGIN / "skills/coordinate/SKILL.md",
    PLUGIN / ".codex-plugin/listing-skills/coordinate/SKILL.md",
)
REFERENCE = PLUGIN / "skills/coordinate/reference.md"
MAX_SKILL_BYTES = 5000
MAX_TOOLS_BYTES = 6000


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-4])
def test_an_always_loaded_skill_stays_under_its_ceiling(path):
    assert len(path.read_bytes()) <= MAX_SKILL_BYTES


def test_the_served_tool_list_stays_under_its_ceiling():
    served = json.dumps(
        {"tools": TOOLS}, ensure_ascii=False, separators=(",", ":")
    )
    assert len(served.encode()) <= MAX_TOOLS_BYTES


def test_the_skill_points_at_its_reference_and_every_tool_is_documented():
    skill = SKILLS[0].read_text()
    reference = REFERENCE.read_text()
    assert "(reference.md)" in skill
    for tool in TOOLS:
        assert f"`{tool['name']}`" in reference, tool["name"]
        for name in tool["inputSchema"]["properties"]:
            assert re.search(rf"\b{name}\b", reference), (tool["name"], name)
