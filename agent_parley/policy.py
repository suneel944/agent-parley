"""Detects assistant attribution in text a lane would publish.

A lane can sign its work: a co-author trailer naming an assistant, a "generated
with" line in a commit body, a vendor or model name in an authorship position on
a pull request. Nothing in a user's repository stops that, and the coordination
layer that launched the lane is the only component that sees every commit,
merge, tag and pull request the lane produces.

The rules live here rather than in this repository's own gate so that both hold
the same text to the same standard: the gate imports them, the lane hook imports
them, and integration imports them. Each rule carries an enumerated name, which
travels into the denial an agent reads and into the lane's event log.

Detection is textual and deliberately narrow. It matches authorship claims and
authorship positions, never an ordinary mention of a product, so a commit that
says "fix the codex adapter" is untouched while one that says it was written by
an assistant is refused.
"""

from __future__ import annotations

import re

ACTOR = r"(?:ai\b|claude\b|codex\b|chatgpt\b|copilot\b|openai\b|anthropic\b)"
TRAILER = (
    r"co-authored-by|author|authored-by|committer|signed-off-by|"
    r"assisted-by|generated-by|created-by|reviewed-by"
)
RULES: tuple[tuple[str, str], ...] = (
    (
        "assistant_credit",
        r"\b(?:generated|written|created|authored|assisted|powered)\s+"
        r"(?:with|by)\s+(?:(?:an?|the)\s+)?" + ACTOR,
    ),
    ("assistant_trailer", r"^co-authored-by:\s*[^\n]*" + ACTOR),
    ("generated_pull_request", r"\bthis\s+pr\s+was\s+generated\s+with\b"),
    ("robot_signature", r"\U0001f916|:robo[t]:|\bbeep\s*\*?\s*boop\b"),
    (
        "assistant_authorship",
        r"^(?:" + TRAILER + r")\s*:\s*[^\n]*" + ACTOR,
    ),
)


def matched_rule(text: str) -> str | None:
    """Names the attribution rule a text breaks, if it breaks one.

    Args:
        text: Commit message, tag message, pull-request title or body, or any
            other contribution text.

    Returns:
        The enumerated rule name, or None when the text claims no assistant
        authorship.
    """
    for name, pattern in RULES:
        if re.search(pattern, text, re.I | re.M):
            return name
    return None


def has_attribution(text: str) -> bool:
    """Reports whether a text claims assistant authorship.

    Args:
        text: File contents, commit message, or public contribution text.

    Returns:
        Whether the text contains a prohibited authorship credit.
    """
    return matched_rule(text) is not None


def refusal(subject: str, rule: str) -> str:
    """Builds the sentence a refused attribution is reported with.

    Args:
        subject: What carried the attribution, such as ``This commit message``
            or ``Commit 1a2b3c4``.
        rule: Enumerated rule name reported by :func:`matched_rule`.

    Returns:
        A refusal naming the rule and what to do about it. No flag turns this
        off, so the message asks for the text to change rather than offering a
        way around the check.
    """
    return (
        f"{subject} attributes the work to an assistant ({rule}). Agent Parley "
        "refuses to publish authorship credits, vendor names in an authorship "
        "position, or generator signatures from a lane, on any repository and "
        "for every provider. Rewrite the text without them and retry; no flag "
        "skips this check."
    )
