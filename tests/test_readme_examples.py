"""Keeps the README's lane examples on lanes the README creates."""

import re
from pathlib import Path

from agent_parley import roster

ROOT = Path(__file__).resolve().parents[1]
LANE = re.compile(
    r"agent-parley (?:say|participant [a-z]+) ([a-z][a-z0-9-]*)\b"
)
RUN = re.compile(r"agent-parley run ([a-z][a-z0-9-]*)\b")


def test_every_readme_lane_example_names_a_lane_the_readme_starts():
    text = (ROOT / "README.md").read_text()
    started = set(roster.PRESETS)
    for line in text.splitlines():
        started.update(RUN.findall(line))
        for name in LANE.findall(line):
            assert name in started, line
