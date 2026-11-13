"""Rewrites the README launch video line for the GitHub Pages build.

GitHub renders a bare ``user-attachments`` URL on its own line as a video
player only on github.com. Jekyll leaves the line as plain text, and the
attachment URL returns 404 to anonymous requests, so the Pages workflow
downloads the release copy of the video into the site and this script
replaces the line with a ``<video>`` element that plays that copy.
"""

import re
import sys
from pathlib import Path

ATTACHMENT_LINE = re.compile(
    r"(?m)^https://github\.com/user-attachments/assets/[0-9a-f-]+$"
)
VIDEO = (
    '<p align="center"><video src="docs/assets/launch.mp4" '
    'poster="docs/assets/launch.webp" width="800" controls muted playsinline '
    'preload="metadata" style="max-width: 100%; height: auto;">'
    '<a href="https://github.com/suneel944/agent-parley#readme">Watch the '
    "Agent Parley launch video on GitHub.</a></video></p>"
)


def rewrite(text: str) -> str:
    """Returns the README text with the attachment line replaced.

    Args:
        text: README Markdown as GitHub renders it.

    Returns:
        The same Markdown with the launch video as a ``<video>`` element.

    Raises:
        ValueError: The text does not hold exactly one attachment line.
    """
    result, count = ATTACHMENT_LINE.subn(VIDEO, text)
    if count != 1:
        raise ValueError(f"expected one attachment line, found {count}")
    return result


def main(path: str = "README.md") -> None:
    """Rewrites the README in place for the Pages build.

    Args:
        path: README path, relative to the working directory.
    """
    readme = Path(path)
    readme.write_text(rewrite(readme.read_text()))


if __name__ == "__main__":
    main(*sys.argv[1:])
