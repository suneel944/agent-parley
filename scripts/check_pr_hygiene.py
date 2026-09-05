"""Validates live pull-request metadata through read-only GitHub requests."""

import json
import os
import re
import subprocess
from typing import Any

from scripts.check_policy import has_attribution


def issue_numbers(body: str) -> set[int]:
    """Returns explicit local issue references from a pull-request body."""
    return {
        int(match)
        for match in re.findall(
            r"(?im)\b(?:refs?|fix(?:es)?|clos(?:e[sd]?)|resolv(?:e[sd]?))"
            r"\s+#([1-9][0-9]*)\b",
            body,
        )
    }


def validate(pr: dict[str, Any], issues: list[dict[str, Any]]) -> list[str]:
    """Returns ownership, classification and traceability violations.

    Args:
        pr: Current GitHub REST pull-request metadata.
        issues: REST issues explicitly referenced by the pull-request body.

    Returns:
        Actionable failures; an empty list means all metadata rules pass.
    """
    errors = []
    if has_attribution(pr.get("title", "") + "\n" + (pr.get("body") or "")):
        errors.append("Remove prohibited attribution from the PR text.")
    if not pr.get("assignees"):
        errors.append("Assign at least one owner.")
    labels = {label["name"] for label in pr.get("labels", [])}
    if not labels & {
        "bug",
        "enhancement",
        "documentation",
        "dependencies",
        "ci",
        "security",
        "performance",
        "release",
    }:
        errors.append("Add a change-type label.")
    linked = [issue for issue in issues if "pull_request" not in issue]
    if not linked:
        errors.append("Reference an existing local issue with Refs #N.")
    milestone = pr.get("milestone")
    if "release" in labels and not milestone:
        errors.append("Release PRs require a milestone.")
    for issue in linked:
        expected = issue.get("milestone")
        if expected and (
            not milestone or milestone["number"] != expected["number"]
        ):
            errors.append(
                f"Match the milestone of linked issue #{issue['number']}."
            )
    if pr.get("user", {}).get("type") != "Bot":
        if not re.fullmatch(
            r"(?:feat|fix|perf|docs|chore|ci|build|refactor|test|revert)"
            r"(?:\([\w.-]+\))?!?: \S.*",
            pr.get("title", ""),
        ):
            errors.append("Use a Conventional Commit title for release notes.")
        body = pr.get("body") or ""
        for heading in (
            "## Problem and result",
            "## Verification",
            "## Compatibility and risks",
        ):
            if heading not in body:
                errors.append(f"Include the PR template section: {heading}")
    return errors


def api(path: str, *, paginate: bool = False) -> Any:
    """Reads authenticated GitHub metadata through the native CLI.

    Raises:
        subprocess.CalledProcessError: If GitHub rejects the request.
        subprocess.TimeoutExpired: If the request exceeds thirty seconds.
    """
    result = subprocess.run(
        ["gh", "api", path]
        + (["--paginate", "--jq", ".[] | @json"] if paginate else []),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if paginate:
        return [json.loads(line) for line in result.stdout.splitlines()]
    return json.loads(result.stdout)


def main() -> None:
    """Checks fresh metadata instead of trusting an older event payload."""
    repository = os.environ["GITHUB_REPOSITORY"]
    number = int(os.environ["PR_NUMBER"])
    pr = api(f"repos/{repository}/pulls/{number}")
    issues = [
        api(f"repos/{repository}/issues/{issue}")
        for issue in sorted(issue_numbers(pr.get("body") or ""))
    ]
    errors = validate(pr, issues)
    commits = api(
        f"repos/{repository}/pulls/{number}/commits?per_page=100", paginate=True
    )
    if any(has_attribution(commit["commit"]["message"]) for commit in commits):
        errors.append("Remove prohibited attribution from commit messages.")
    for endpoint in (
        f"issues/{number}/comments",
        f"pulls/{number}/comments",
        f"pulls/{number}/reviews",
    ):
        comments = api(f"repos/{repository}/{endpoint}", paginate=True)
        if any(
            has_attribution(comment.get("body") or "") for comment in comments
        ):
            errors.append("Remove prohibited attribution from PR comments.")
    if any(has_attribution(issue.get("body") or "") for issue in issues):
        errors.append("Remove prohibited attribution from linked issues.")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"PR #{number}: owner, labels, issue and milestone policy passed")


if __name__ == "__main__":
    main()
