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


def owners(source: str) -> list[str]:
    """Maps each line of a module to the innermost definition holding it.

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
            else:
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
    matcher = difflib.SequenceMatcher(
        None, before.split("\n"), after.split("\n"), autojunk=False
    )
    found: set[str] = set()
    for tag, first, last, start, end in matcher.get_opcodes():
        if tag != "equal":
            found.update(old[first + 1 : last + 1])
            found.update(new[start + 1 : end + 1])
    return sorted(found)
