"""Validates live issue metadata through read-only GitHub requests.

Issue forms in ``.github/ISSUE_TEMPLATE`` shape only issues filed through the
web form; ``gh issue create --body`` skips them. This check reads the live
issue after every open or edit and names each rule it breaks, using the
template files themselves as the source of the required title prefix and
section headings so the check and the forms cannot drift apart.
"""

import os
import re
from pathlib import Path
from typing import Any

from scripts.check_policy import has_attribution
from scripts.check_pr_hygiene import api

TEMPLATES = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE"
TYPE_LABELS = {
    "bug",
    "enhancement",
    "documentation",
    "dependencies",
    "ci",
    "security",
    "performance",
    "release",
    "refactor",
}


def template_rules(directory: Path = TEMPLATES) -> dict[str, list[str]]:
    """Reads each issue form's title prefix and required section headings.

    Args:
        directory: Folder holding the YAML issue forms.

    Returns:
        Each form's title prefix, such as ``bug: ``, mapped to the labels of
        its required fields in form order.
    """
    rules = {}
    for path in sorted(directory.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        prefix = re.search(r'^title:\s*"([^"]+)"', text, re.MULTILINE)
        if not prefix:
            continue
        required = []
        for block in re.split(r"^  - type:", text, flags=re.MULTILINE)[1:]:
            label = re.search(r"^\s+label:\s*(.+?)\s*$", block, re.MULTILINE)
            required_field = re.search(
                r"^\s+required:\s*true\b", block, re.MULTILINE
            )
            if label and required_field:
                required.append(label.group(1).strip("\"'"))
        rules[prefix.group(1)] = required
    return rules


def validate(issue: dict[str, Any], rules: dict[str, list[str]]) -> list[str]:
    """Returns template, ownership and classification violations.

    Args:
        issue: Current GitHub REST issue metadata.
        rules: Title prefixes mapped to required section headings.

    Returns:
        Actionable failures; an empty list means every issue rule passes.
    """
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    errors = []
    if has_attribution(title + "\n" + body):
        errors.append("Remove prohibited attribution from the issue text.")
    prefix = next(
        (
            prefix
            for prefix in rules
            if title.startswith(prefix) and title[len(prefix) :].strip()
        ),
        None,
    )
    if prefix is None:
        names = " or ".join(f"'{prefix}'" for prefix in rules)
        errors.append(f"Start the title with {names} and describe the issue.")
    else:
        for heading in rules[prefix]:
            if not re.search(
                rf"^#{{2,3}} {re.escape(heading)}\s*$", body, re.MULTILINE
            ):
                errors.append(
                    f"Include the issue template section: ### {heading}"
                )
    if not issue.get("assignees"):
        errors.append("Assign at least one owner.")
    labels = {label["name"] for label in issue.get("labels", [])}
    if not labels & TYPE_LABELS:
        errors.append("Add a change-type label.")
    if not issue.get("milestone"):
        errors.append("Add the issue to a milestone.")
    return errors


def main() -> None:
    """Checks fresh metadata instead of trusting an older event payload."""
    repository = os.environ["GITHUB_REPOSITORY"]
    number = int(os.environ["ISSUE_NUMBER"])
    issue = api(f"repos/{repository}/issues/{number}")
    if "pull_request" in issue or issue.get("state") != "open":
        print(f"Issue #{number}: not an open issue, skipped")
        return
    errors = validate(issue, template_rules())
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Issue #{number}: template, owner, label and milestone passed")


if __name__ == "__main__":
    main()
