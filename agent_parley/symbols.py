"""Python definitions a file edit changes, for symbol reservations.

A reservation may name one function or class of a Python file as
``path::Qualified.name``. The lane hook compares the definitions an edit
changes with those keys, so two lanes holding different functions of one
module do not contend. Only the standard library ``ast`` and ``difflib``
modules are used. Reservations stay advisory; this module only names what
an edit touches.
"""

from __future__ import annotations

import ast
import difflib

DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
BLOCKS = (ast.stmt, ast.excepthandler, ast.match_case)


def owners(source: str) -> list[str]:
    """Maps each line of a module to the innermost definition holding it.

    Only statements are searched, since a definition is always one, so the
    expressions of a long module are never walked.

    Args:
        source: Module text.

    Returns:
        One qualified name per line, indexed from 1, such as
        ``Store.claim``; an empty string for a line at module level.

    Raises:
        SyntaxError: If the text does not parse.
        ValueError: If the text carries a null byte.
    """
    tree = ast.parse(source)
    spans: list[tuple[int, int, str]] = []
    pending: list[tuple[ast.AST, str]] = [(tree, "")]
    while pending:
        node, prefix = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFINITIONS):
                name = prefix + child.name
                decorators = [item.lineno for item in child.decorator_list]
                start = min([child.lineno, *decorators])
                end = child.end_lineno or child.lineno
                spans.append((start, -end, name))
                pending.append((child, name + "."))
            elif isinstance(child, BLOCKS):
                pending.append((child, prefix))
    found = [""] * (source.count("\n") + 2)
    for start, negative, name in sorted(spans):
        found[start : 1 - negative] = [name] * (1 - negative - start)
    return found


def touched(before: str, after: str) -> list[str] | None:
    """Names the definitions an edit changes, read before and after it.

    Changed lines are read in the text before the edit, which names a
    definition renamed or removed, and in the text after it, which names
    one added. Each line is charged to the innermost function or class
    holding it, so editing one method touches that method, not its class.

    A class span covers the blank lines between its methods, so a blank or
    comment-only changed line is not charged while any other line changed;
    adding a method beside a reserved one then touches only the new
    method. An edit that changes nothing but such lines is charged by them.

    The lines both texts share at the start and the end are trimmed before
    the texts are compared, so a small edit of a long module compares only
    the span it changes.

    Args:
        before: Module text before the edit, empty for a new file.
        after: Module text the edit would leave.

    Returns:
        Sorted qualified names, with an empty string when a changed line is
        at module level, or None when either text does not parse.
    """
    try:
        old = owners(before)
        new = owners(after)
    except (SyntaxError, ValueError):
        return None
    first = before.split("\n")
    second = after.split("\n")
    shorter = min(len(first), len(second))
    head = 0
    while head < shorter and first[head] == second[head]:
        head += 1
    tail = 0
    while tail < shorter - head and first[-1 - tail] == second[-1 - tail]:
        tail += 1
    matcher = difflib.SequenceMatcher(
        None,
        first[head : len(first) - tail],
        second[head : len(second) - tail],
        autojunk=False,
    )
    changed: list[tuple[str, str]] = []
    for tag, start, end, begin, finish in matcher.get_opcodes():
        if tag != "equal":
            changed.extend(
                (first[line], old[line + 1])
                for line in range(head + start, head + end)
            )
            changed.extend(
                (second[line], new[line + 1])
                for line in range(head + begin, head + finish)
            )
    code = {name for text, name in changed if not ignorable(text)}
    return sorted(code or {name for _, name in changed})


def ignorable(line: str) -> bool:
    """Reports whether a line is blank or holds only a comment.

    Args:
        line: One line of module text.

    Returns:
        Whether the line carries no code.
    """
    text = line.strip()
    return not text or text.startswith("#")
