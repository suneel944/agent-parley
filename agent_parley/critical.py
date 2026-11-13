"""Weighs issues by what finishing them unblocks, within one milestone.

Choosing the next issue by number or by plan group ignores the shape of the
work: an issue that three others wait on frees more than one nothing waits
on. The shape is recorded in three places, all read here: ``Blocked by #N``
and ``Depends on #N`` lines in an issue body, the forge's own issue
relationships, and the ledger's ``blocked_by`` set by ``issue block``. The
forge side is read once by `forge.open_issue_catalog` and cached beside the
rest of the catalog; this module is pure and reads nothing itself.

An edge joins two open issues of the same milestone. A set of issues that
wait on each other can never start, so such a cycle is reported with its
members and every edge among them is dropped, and the ranking proceeds as if
those edges were absent. Nothing here writes a ledger or claims an issue.
"""

import re

FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
SPAN = re.compile(r"(`+).+?\1")
LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:blocked\s+by|depends\s+on)\s*:?\s*"
    r"(#\d+(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+)#\d+)*)(?!\w)",
    re.IGNORECASE,
)


def references(body: str) -> list[str]:
    """Reads the issues a body says it waits on.

    A line counts when it begins, after an optional list marker, with
    ``Blocked by`` or ``Depends on`` in any case, an optional colon, and one
    or more ``#N`` separated by commas or ``and``. Fenced blocks, indented
    blocks that follow a blank line and inline code spans are skipped, so
    an example inside code never becomes an edge, and a passing mention such
    as ``see #5`` is not a dependency.

    Args:
        body: Issue body text as the forge returns it.

    Returns:
        Distinct issue numbers as bare digit strings, ascending.
    """
    found: set[int] = set()
    fence = ""
    previous_blank = True
    code = False
    for line in (body or "").splitlines():
        marker = FENCE.match(line)
        if fence:
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
            ):
                fence = ""
            continue
        if marker:
            fence = marker.group(1)
            continue
        blank = not line.strip()
        indented = line.startswith(("    ", "\t"))
        code = indented and (previous_blank or code) and not blank
        previous_blank = blank
        if code or blank:
            continue
        match = LINE.match(SPAN.sub("", line))
        if match:
            found.update(int(n) for n in re.findall(r"#(\d+)", match.group(1)))
    return [str(number) for number in sorted(found)]


def _components(edges: dict[str, list[str]]) -> list[list[str]]:
    """Finds the strongly connected components, iteratively.

    Args:
        edges: Node to the nodes it points at; every target is also a key.

    Returns:
        Every component, each as a list of its nodes.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    held: list[str] = []
    live: set[str] = set()
    found: list[list[str]] = []
    for root in sorted(edges, key=int):
        if root in index:
            continue
        work = [(root, iter(edges[root]))]
        index[root] = low[root] = len(index)
        held.append(root)
        live.add(root)
        while work:
            node, targets = work[-1]
            advanced = False
            for target in targets:
                if target not in index:
                    index[target] = low[target] = len(index)
                    held.append(target)
                    live.add(target)
                    work.append((target, iter(edges[target])))
                    advanced = True
                    break
                if target in live:
                    low[node] = min(low[node], index[target])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    member = held.pop()
                    live.discard(member)
                    members.append(member)
                    if member == node:
                        break
                found.append(members)
    return found


def graph(blockers: dict[str, list[str]], open_issues: set[str]) -> dict:
    """Builds the dependency graph of open issues and finds its cycles.

    Args:
        blockers: Issue number to the issue numbers it waits on.
        open_issues: Numbers of the issues still open; an edge with either
            end outside the set is left out.

    Returns:
        ``unblocks``, each blocker mapped to the open issues waiting
        directly on it, ascending, and ``cycles``, every set of issues that
        wait on each other, members ascending, ordered by first member. The
        edges among a cycle's members, and a self-reference, are absent from
        ``unblocks``. ``blocked`` maps each issue to the open issues it
        still waits on once those cycle edges are dropped, ascending; an
        issue with no such blocker is absent.
    """
    edges: dict[str, list[str]] = {number: [] for number in open_issues}
    for waiter, waiting_on in blockers.items():
        if waiter not in open_issues:
            continue
        for blocker in waiting_on:
            if blocker in open_issues:
                edges[blocker].append(waiter)
    cycles = []
    member_of: dict[str, int] = {}
    for component in _components(edges):
        looped = len(component) == 1 and component[0] in edges[component[0]]
        if len(component) > 1 or looped:
            cycles.append(sorted(component, key=int))
    cycles.sort(key=lambda members: int(members[0]))
    for position, members in enumerate(cycles):
        for member in members:
            member_of[member] = position
    unblocks: dict[str, list[str]] = {}
    waiting: dict[str, list[str]] = {}
    for blocker, waiters in edges.items():
        kept = sorted(
            {
                waiter
                for waiter in waiters
                if not (
                    blocker in member_of
                    and member_of[blocker] == member_of.get(waiter)
                )
            },
            key=int,
        )
        if kept:
            unblocks[blocker] = kept
        for waiter in kept:
            waiting.setdefault(waiter, []).append(blocker)
    blocked = {
        waiter: sorted(blockers, key=int)
        for waiter, blockers in waiting.items()
    }
    return {"unblocks": unblocks, "cycles": cycles, "blocked": blocked}


def _depths(unblocks: dict[str, list[str]], roots: list[str]) -> dict[str, int]:
    """Measures the longest chain below each issue reachable from roots.

    Args:
        unblocks: Acyclic blocker to direct waiters mapping.
        roots: Issues to start from.

    Returns:
        Each reached issue to the number of issues in the longest chain it
        starts, itself not counted.
    """
    depth: dict[str, int] = {}
    for root in roots:
        stack = [root]
        while stack:
            node = stack[-1]
            if node in depth:
                stack.pop()
                continue
            pending = [w for w in unblocks.get(node, []) if w not in depth]
            if pending:
                stack.extend(pending)
                continue
            depth[node] = max(
                (depth[w] + 1 for w in unblocks.get(node, [])), default=0
            )
            stack.pop()
    return depth


def reach(found: dict, number: str) -> tuple[int, int]:
    """Counts what finishing one issue unblocks.

    Args:
        found: The result of `graph`.
        number: Issue number to measure.

    Returns:
        The count of distinct open issues that wait on it directly or
        through others, and the number of issues in the longest chain it
        starts, itself not counted. Both are zero for an issue with no edge.
    """
    unblocks = found["unblocks"]
    seen: set[str] = set()
    stack = list(unblocks.get(number, []))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(unblocks.get(node, []))
    return len(seen), _depths(unblocks, [number])[number] if seen else 0


def path(found: dict) -> list[str]:
    """Finds the longest chain of waiting issues in the whole graph.

    Args:
        found: The result of `graph`.

    Returns:
        Issue numbers from the blocker that starts the chain to the last
        issue in it; ties go to the lowest issue numbers. Empty when the
        graph has no edge.
    """
    unblocks = found["unblocks"]
    if not unblocks:
        return []
    depth = _depths(unblocks, sorted(unblocks, key=int))
    node = min(unblocks, key=lambda n: (-depth[n], int(n)))
    chain = [node]
    while unblocks.get(node):
        node = min(unblocks[node], key=lambda n: (-depth[n], int(n)))
        chain.append(node)
    return chain


def milestone_edges(
    catalog: dict[str, dict], ledger: dict, milestone: str | None
) -> tuple[dict[str, list[str]], set[str]]:
    """Gathers the open issues of a milestone and what each waits on.

    Edges come from the cached forge catalog's ``blocked_by`` and from the
    ledger's, joined. An edge counts only when both ends carry the same
    milestone, an issue the catalog does not name one for belonging to the
    unnamed milestone. An issue the ledger records complete, closed on the
    forge or ended there is not open, and with a non-empty catalog an issue
    the catalog does not list is not open either.

    Args:
        catalog: The forge cache's issue mapping, possibly empty.
        ledger: Published issue ledger, with forge-only records included or
            not.
        milestone: Milestone title to restrict to, or None for every issue.

    Returns:
        Issue number to the numbers it waits on, and the open issues
        considered.
    """
    from agent_parley import issues

    recorded = ledger.get("issues", {})
    done = {
        number
        for number, record in recorded.items()
        if issues.closed(record) or record.get("ended_on_forge")
    }

    def entry(number: str) -> dict:
        """Returns the cached record of an issue, or an empty one."""
        item = catalog.get(number)
        return item if isinstance(item, dict) else {}

    def titled(number: str) -> str:
        """Names the milestone of an issue, empty when it has none."""
        return str(entry(number).get("milestone") or "")

    open_issues = {
        number
        for number in {*catalog, *recorded}
        if str(number).isdigit()
        and number not in done
        and (not catalog or number in catalog)
        and (milestone is None or titled(number) == milestone)
    }
    blockers: dict[str, list[str]] = {}
    for number in open_issues:
        joined = {
            str(other)
            for other in [
                *(entry(number).get("blocked_by") or []),
                *(recorded.get(number, {}).get("blocked_by") or []),
            ]
        }
        kept = sorted(
            (other for other in joined if titled(other) == titled(number)),
            key=int,
        )
        if kept:
            blockers[number] = kept
    return blockers, open_issues
