"""Ranks the unclaimed issues a lane could take next, and says why.

Choosing work today means reading the ledger, the plan and the reservations
peers hold, then guessing. The reading is the same every time, so it is done
here once: unclaimed and unblocked issues first, ordered by the plan group
already under way, by how many owned issues each one unblocks, and away from
the issues whose likely paths a peer already reserves or whose history
forecasts a collision with one.

Nothing here claims anything. The result is a shortlist with the reason for
each entry, and taking one of them stays an explicit, separate call, so a
recommendation never moves ownership and two lanes reading the same ledger
still race for the claim rather than for the advice. To keep that race rare,
an issue the supervisor already named as another lane's next work ranks
below the issues nobody was pointed at.
"""

import re
from collections.abc import Callable
from pathlib import Path

from agent_parley import forecast, forge, issues

MAX_SHORTLIST = 5
MAX_LOOKUPS = 3
MAX_CLAIM_LOOKUPS = 6
MAX_REASONS = 6
MINIMUM_TERM = 3
COMMON_TERMS = frozenset(
    {
        "add",
        "and",
        "are",
        "can",
        "code",
        "file",
        "files",
        "fix",
        "for",
        "from",
        "issue",
        "make",
        "not",
        "repo",
        "should",
        "that",
        "the",
        "this",
        "use",
        "want",
        "when",
        "with",
        "work",
    }
)


def conflicts(
    paths: list[str],
    held: dict[str, list[str]],
    commits: list[list[str]],
    overlap: Callable[[str, str], bool],
) -> dict:
    """Names the peer reservations one issue's likely paths run into.

    Args:
        paths: Repository-relative paths the issue's earlier pull requests
            touched, which stand in for the reservation nobody has filed yet.
        held: Active reservation keys per peer identity, the reading lane's
            own identity already excluded.
        commits: Files per recent commit of the base checkout, as
            ``forecast.history`` reports them.
        overlap: The store's reservation overlap rule.

    Returns:
        The peer-held keys those paths overlap directly, each carrying
        ``path`` and ``peer``, and the forecast collision records the
        co-change history predicts for the same paths.
    """
    direct = [
        {"path": path, "peer": peer}
        for path in paths
        for peer, patterns in sorted(held.items())
        if any(overlap(path, pattern) for pattern in patterns)
    ]
    direct.sort(key=lambda entry: (entry["path"], entry["peer"]))
    return {
        "overlaps": direct[:MAX_REASONS],
        "collisions": forecast.collisions(
            forecast.cochanges(commits, paths, overlap), held, overlap
        ),
    }


def candidate_paths(repo: Path, free: list[str]) -> dict[str, list[str]]:
    """Reads the paths earlier pull requests touched for the first candidates.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        free: Unclaimed issues, best first.

    Returns:
        Paths per issue for the first ``MAX_LOOKUPS`` issues, leaving out an
        issue the forge named no path for.
    """
    found = {
        number: forge.issue_pull_request_paths(repo, number)
        for number in free[:MAX_LOOKUPS]
    }
    return {number: paths for number, paths in found.items() if paths}


def sharing(
    state: dict,
    looked: dict[str, list[str]],
    commits: list[list[str]],
    repo: Path,
    overlap: Callable[[str, str], bool],
    lane: str = "",
) -> dict[str, list[dict]]:
    """Names the live claims each candidate's forecast paths run into.

    A candidate's forecast paths are the paths its earlier pull requests
    touched together with the files ``forecast.cochanges`` says change with
    them. A live claim's paths are the paths its own earlier pull requests
    touched, read for at most ``MAX_CLAIM_LOOKUPS`` claims, the reading
    lane's first. A claim the ledger records complete, or one released,
    holds nothing, so a candidate it held back returns on the next reading.
    Nothing is read from the forge when no candidate has paths, and each
    path reading is kept for ``forge.PATHS_TTL`` seconds, so a poll that
    repeats the ranking does not ask the forge's search again.

    Args:
        state: Published issue ledger.
        looked: Paths per candidate, as ``candidate_paths`` reports them.
        commits: Files per recent commit of the base checkout, as
            ``forecast.history`` reports them.
        repo: Repository or assigned worktree that selects the forge project.
        overlap: The store's reservation overlap rule.
        lane: Lane doing the reading, whose own claims are read first.

    Returns:
        Per candidate sharing paths with a live claim, one record per such
        claim carrying ``issue`` and ``owner``, in issue number order.
    """
    if not looked:
        return {}
    owners = issues.holders(state)
    order = [
        *owners.get(lane, []),
        *(
            number
            for owner in sorted(owners)
            if owner != lane
            for number in owners[owner]
        ),
    ]
    claimed = {
        number: forge.issue_pull_request_paths(repo, number)
        for number in order[:MAX_CLAIM_LOOKUPS]
    }
    found: dict[str, list[dict]] = {}
    for number, paths in looked.items():
        predicted = [*paths, *forecast.cochanges(commits, paths, overlap)]
        entries = [
            {"issue": held, "owner": str(state["issues"][held]["owner"])}
            for held in sorted(claimed, key=int)
            if any(
                overlap(path, key)
                for path in predicted
                for key in claimed[held]
            )
        ]
        if entries:
            found[number] = entries
    return found


def for_lane(
    free: list[str], shared: dict[str, list[dict]], lane: str
) -> list[str]:
    """Orders free issues for one lane by the live claims they share paths with.

    An issue sharing paths with a claim this lane holds moves to the front,
    one sharing paths with another lane's claim moves to the back, and the
    order is otherwise kept.

    Args:
        free: Unclaimed issues, best first.
        shared: Live claims per candidate, as ``sharing`` reports them.
        lane: Lane the order is for.

    Returns:
        The same issues in this lane's order.
    """
    return sorted(
        free,
        key=lambda number: (
            any(e["owner"] != lane for e in shared.get(number, [])),
            not any(e["owner"] == lane for e in shared.get(number, [])),
        ),
    )


def _grouped(groups: dict[str, list[str]]) -> dict[str, str]:
    """Maps each planned issue to the first group alphabetically holding it."""
    return {
        number: name
        for name in sorted(groups, reverse=True)
        for number in groups[name]
    }


def _underway(groups: dict[str, list[str]], state: dict) -> set[str]:
    """Names the groups a lane already owns at least one member of."""
    return {
        name
        for name, members in groups.items()
        if any(
            state.get("issues", {}).get(number, {}).get("owner")
            for number in members
        )
    }


def _reasons(record: dict, known: bool, provider: str) -> list[str]:
    """States, in ranking order, why an issue sits where it sits."""
    said = ["no recorded dependency blocks it"]
    if record["unblocks"]:
        owned = [n for n in record["unblocks"] if n not in record["planned"]]
        listed = [f"#{n}" for n in owned]
        if record["planned"]:
            listed.append(
                ", ".join(f"#{n}" for n in record["planned"]) + " (planned)"
            )
        said.append("unblocks " + ", ".join(listed))
    if record["group"]:
        said.append(
            f"plan group {record['group']} is already under way"
            if record["group_underway"]
            else f"plan group {record['group']} is not started"
        )
    for entry in record["overlaps"]:
        said.append(f"{entry['peer']} reserves {entry['path']}")
    for entry in record["collisions"]:
        said.append(
            f"likely to collide with {entry['peer']} on {entry['path']}"
        )
    if known and not record["overlaps"] and not record["collisions"]:
        said.append("no peer reservation or forecast collision on its paths")
    if record["provider"]:
        said.append(
            f"declares provider {record['provider']}, which this lane runs"
            if record["provider"] == provider
            else f"declares provider {record['provider']}, "
            f"not this lane's {provider or 'own'}"
        )
    if record.get("named_to"):
        said.insert(1, f"already named as {record['named_to']}'s next work")
    if record.get("own"):
        said.insert(1, "named as this lane's next work")
    if record.get("same_files"):
        held = ", ".join(f"#{n}" for n in record["same_files"])
        said.insert(1, f"same files as {held} you hold")
    for entry in reversed(record.get("held_back") or []):
        said.insert(
            1,
            f"held back: same files as #{entry['issue']} "
            f"{entry['owner']} holds",
        )
    return said[:MAX_REASONS]


def rank(
    state: dict,
    groups: dict[str, list[str]],
    risks: dict[str, dict],
    declared: dict[str, str],
    provider: str,
    limit: int = MAX_SHORTLIST,
    named: dict[str, str] | None = None,
    own: str = "",
    shared: dict[str, list[dict]] | None = None,
    lane: str = "",
) -> list[dict]:
    """Orders the issues a lane could take next, worst obstacle last.

    The order is a total one over the ledger's own contents, so the same
    store state always produces the same shortlist. An issue whose forecast
    paths run into another lane's live claim is held back below every other
    issue until that claim completes or is released, and one whose forecast
    paths run into a claim this lane holds ranks first, so work on the same
    files gathers on one lane instead of colliding at integration. The issue
    the supervisor named as this lane's own next work ranks next, so the
    lane claims what it was pointed at rather than a lower-numbered free
    issue. An issue the
    supervisor already named as another lane's next work sinks below every
    issue no lane was pointed at, so lanes reading the same ledger spread
    out instead of racing for one claim. An issue a peer already
    reserves the paths of sinks below one nobody contends, a declared
    provider this lane does not run sinks below a matching or silent one, a
    forecast collision sinks below a clean path, and what remains is decided
    by a plan group already under way, then by how much authorized,
    unfinished work, planned or owned, waits on the issue, then by how many
    owned issues wait on it, then by the issue number.

    Args:
        state: Published issue ledger.
        groups: Groups the applied plan names, mapped to their issues.
        risks: Reservation overlaps and forecast collisions per issue, as
            ``conflicts`` reports them. An issue absent from the mapping was
            not looked up, which is reported rather than read as clean.
        declared: Provider each issue declares, where the forge says so.
        provider: Provider driving the reading lane.
        limit: Most entries to return.
        named: Issue number to the other lane the supervisor's published
            work names it to, as its next lead or first pull pick.
        own: Issue the supervisor's published work names as the reading
            lane's own next lead, or empty when it names none.
        shared: Live claims per candidate, as ``sharing`` reports them.
        lane: Lane doing the reading, which owns the claims it shares
            paths with rather than being held back by them.

    Returns:
        One record per candidate carrying its number, recorded title, plan
        group, the issues it unblocks and which of those are planned rather
        than owned, the provider it declares, the peer reservations and
        forecast collisions found for it, the lane it is named to, whether
        it is this lane's own lead, the claims of this lane it shares paths
        with, the other lanes' claims holding it back, and the reasons for
        its position.
    """
    named = named or {}
    shared = shared or {}
    waiting = issues.waiters(state, owned_only=False)
    owned = issues.waiters(state)
    placed = _grouped(groups)
    started = _underway(groups, state)
    ranked = []
    for number in issues.unclaimed(state):
        risk = risks.get(number) or {}
        group = placed.get(number, "")
        unblocks = waiting.get(number, [])
        record = {
            "issue": number,
            "title": state["issues"][number].get("title") or "",
            "group": group,
            "group_underway": group in started,
            "unblocks": unblocks,
            "planned": [n for n in unblocks if n not in owned.get(number, [])],
            "provider": declared.get(number, ""),
            "overlaps": risk.get("overlaps", []),
            "collisions": risk.get("collisions", []),
            "named_to": "" if number == own else named.get(number, ""),
            "own": number == own,
            "same_files": [
                entry["issue"]
                for entry in shared.get(number, [])
                if entry["owner"] == lane
            ],
            "held_back": [
                entry
                for entry in shared.get(number, [])
                if entry["owner"] != lane
            ],
        }
        record["reasons"] = _reasons(record, number in risks, provider)
        ranked.append(record)
    ranked.sort(
        key=lambda record: (
            bool(record["held_back"]),
            not record["same_files"],
            not record["own"],
            bool(record["named_to"]),
            bool(record["overlaps"]),
            bool(record["provider"]) and record["provider"] != provider,
            bool(record["collisions"]),
            not record["group_underway"],
            -len(record["unblocks"]),
            len(record["planned"]),
            int(record["issue"]),
        )
    )
    return ranked[: max(limit, 1)]


def shortlist(
    directory: Path,
    root: str,
    repo: Path,
    provider: str,
    held: dict[str, list[str]],
    overlap: Callable[[str, str], bool],
    limit: int = MAX_SHORTLIST,
    named: dict[str, str] | None = None,
    own: str = "",
    lane: str = "",
) -> dict:
    """Reads the ledger, the plan and the forge, and ranks what is free.

    Open forge issues the ledger has never recorded are candidates too,
    read from the supervisor's cache through `issues.with_forge`, so a
    freshly opened issue is offered before anybody claims it.

    The forge reading is best effort and bounded: the paths an issue's
    earlier pull requests touched are read for the first ``MAX_LOOKUPS``
    candidates only, and a forge that is absent, slow or unwilling yields a
    shortlist ordered by the ledger and the plan alone rather than a failure.
    When a candidate has paths, the live claims' paths are read through
    `sharing`, bounded by ``MAX_CLAIM_LOOKUPS``, and the co-change history
    comes from the same cached `forecast.history` reading the collision
    forecast uses. Nothing is claimed, offered or written.

    The recorded plan is read through a deferred import, because the hook path
    imports this module through the store and must not pay for a TOML parser
    it never uses.

    Args:
        directory: Private project state directory holding the ledger, the
            recorded plan and the co-change cache.
        root: Canonical project key, which is the base checkout's path.
        repo: Repository or assigned worktree that selects the forge project.
        provider: Provider driving the reading lane.
        held: Active reservation keys per peer identity, the reading lane's
            own identity already excluded.
        overlap: The store's reservation overlap rule.
        limit: Most entries to return.
        named: Issue number to the other lane whose published work names
            it, which ranks it below every issue no lane was pointed at.
        own: Issue the reading lane's own published work names as its
            lead, which ranks first after work on its own claims' files.
        lane: Ledger name of the reading lane, which `sharing` uses to tell
            its own claims from the claims that hold a candidate back.

    Returns:
        The provider considered, whether the forge answered with any paths,
        and the ranked candidates.
    """
    from agent_parley import plan

    state = issues.with_forge(directory, issues.snapshot(directory))
    free = issues.unclaimed(state)
    looked = candidate_paths(repo, free)
    commits = forecast.history(root, directory) if looked else []
    risks = {
        number: conflicts(found, held, commits, overlap)
        for number, found in looked.items()
    }
    return {
        "provider": provider,
        "forge_paths": bool(looked),
        "candidates": rank(
            state,
            plan.groups(directory),
            risks,
            forge.issue_providers(repo) if free else {},
            provider,
            limit,
            named,
            own,
            sharing(state, looked, commits, repo, overlap, lane),
            lane,
        ),
    }


def terms(text: str) -> set[str]:
    """Reduces free text to the words worth matching an issue against.

    Args:
        text: A stated goal, an issue title or a reservation pattern.

    Returns:
        Lowercased words of at least ``MINIMUM_TERM`` characters, with the
        words that appear in almost every goal removed, so a match means
        shared subject matter rather than shared English.
    """
    found = set()
    for word in re.split(r"[^0-9A-Za-z_]+", text.lower()):
        stripped = word.strip("_")
        if len(stripped) >= MINIMUM_TERM and stripped not in COMMON_TERMS:
            found.add(stripped)
    return found


def _match_reasons(
    wanted: set[str],
    title: str,
    labels: list[str],
    owner: str,
    reserved: list[dict],
) -> list[str]:
    """States what a goal and one recorded issue actually share."""
    shared = sorted(wanted & terms(title))
    said = []
    if shared:
        said.append("title shares " + ", ".join(shared))
    for label in labels:
        if wanted & terms(label):
            said.append(f"label {label} matches")
    for entry in reserved:
        said.append(f"{entry['peer']} reserves {entry['path']}")
    if owner:
        said.append(f"held by {owner}; negotiate a handoff rather than claim")
    return said[:MAX_REASONS]


def match(
    goal: str,
    catalog: dict[str, dict],
    state: dict,
    held: dict[str, list[str]],
    limit: int = MAX_SHORTLIST,
) -> dict:
    """Finds the recorded open issues a stated goal already describes.

    Opening a second issue for work the ledger already tracks splits the
    history of one task across two numbers, and a lane cannot see that from
    its own worktree. The reading here is deliberately shallow: shared words
    between the goal and an issue's recorded title or forge labels, plus the
    peer reservations the goal's own words run into. It ranks, it never
    decides, and a match is a prompt to read the issue rather than proof that
    it is the same work.

    Args:
        goal: What the lane intends to do, in the operator's words.
        catalog: Open issues the forge reports, each carrying its recorded
            title and its labels. An empty catalog matches nothing, which is
            reported as no match rather than as a clean slate.
        state: Published issue ledger, read for current ownership only.
        held: Active reservation keys per peer identity, the reading lane's
            own identity already excluded.
        limit: Most matches to return.

    Returns:
        The goal read, the words it was matched on, and the matching issues
        best first, each carrying its number, recorded title, current owner,
        labels and the reasons it matched.
    """
    wanted = terms(goal)
    reserved = [
        {"peer": peer, "path": pattern}
        for peer, patterns in sorted(held.items())
        for pattern in patterns
        if wanted & terms(pattern)
    ]
    ledger = state.get("issues", {})
    found = []
    for number, record in catalog.items():
        title = record.get("title") or ledger.get(number, {}).get("title", "")
        names = record.get("labels", [])
        overlap = len(wanted & terms(title)) + sum(
            1 for name in names if wanted & terms(name)
        )
        if not overlap:
            continue
        owner = ledger.get(number, {}).get("owner") or ""
        found.append(
            {
                "issue": number,
                "title": title,
                "owner": owner,
                "labels": names,
                "matched": overlap,
                "reasons": _match_reasons(
                    wanted, title, names, owner, reserved[:MAX_REASONS]
                ),
            }
        )
    found.sort(key=lambda record: (-record["matched"], int(record["issue"])))
    return {
        "goal": goal,
        "terms": sorted(wanted),
        "reservations": reserved[:MAX_REASONS],
        "matches": found[: max(limit, 1)],
    }


def render_match(result: dict) -> str:
    """Formats goal matches as one line per issue, closest first.

    Returns:
        The matching issues with the reason for each, or a single line
        stating that the ledger records no open issue for that goal.
    """
    if not result["matches"]:
        return (
            "No recorded open issue matches that goal; "
            "opening a new one is the recorded next step."
        )
    lines = [
        f"#{record['issue']}"
        + (f" {record['title']}" if record["title"] else "")
        + ": "
        + "; ".join(record["reasons"])
        for record in result["matches"]
    ]
    lines.append("Claim one of these before opening a new issue.")
    return "\n".join(lines)


def render(result: dict) -> str:
    """Formats a shortlist as one line per candidate, best first.

    Returns:
        The ranked issues with their reasons and the instruction to claim
        before exploring, or a single line stating that the ledger records
        nothing free to take.
    """
    if not result["candidates"]:
        return "No unclaimed, unblocked issue is recorded."
    lines = [
        f"#{record['issue']}"
        + (f" {record['title']}" if record["title"] else "")
        + ": "
        + "; ".join(record["reasons"])
        for record in result["candidates"]
    ]
    lines.append(
        "Claim one with agent-parley issue claim before exploring or "
        "editing; a peer may be reading the same list."
    )
    return "\n".join(lines)
