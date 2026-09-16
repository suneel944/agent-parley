"""Optional best-effort exchanges with the repository's issue tracker.

Every call here is optional context, never authority. The local ledger decides
ownership and is written first; a forge lookup or mirror runs afterwards
through the operator's own client, adds no flag that bypasses a repository
rule, and reports failure instead of raising. Coordination must keep working
with no network, no client and no remote.

The forge is selected per project. ``github`` speaks through ``gh`` and is the
shipped default. ``beads`` speaks through the ``bd`` client when the
repository carries a Beads ledger in ``.beads/``. ``null`` keeps issue numbers
bare, calls nothing and never fails. :func:`select` names the implementation
from the project manifest, falling back to detection, and every public
function routes through that choice.
"""

import getpass
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

MAX_TITLE = 200
MAX_PATHS = 200
MAX_OPEN_ISSUES = 100
PROVIDER_LABEL = "provider:"
FORGES = ("github", "beads", "null")
DEFAULT_FORGE = "github"

GITHUB_REMOTE = re.compile(
    r"^(?:https://|ssh://git@|git@)github\.com[:/]"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)

_selected: dict[str, str] = {}


def select(repo: Path, manifest: dict | None = None) -> str:
    """Names the forge implementation that applies to a checkout.

    A value recorded in the project manifest wins. Without one, a checkout
    carrying a Beads ledger selects ``beads`` and any other selects
    ``github``. The choice is remembered for the checkout path, because the
    lookups and mirrors below receive only that path and must route through
    the same implementation the caller resolved.

    Args:
        repo: Repository or assigned worktree the forge is asked about.
        manifest: Project manifest, or None to rely on detection alone.

    Returns:
        One of ``github``, ``beads`` or ``null``.
    """
    recorded = (manifest or {}).get("forge")
    if recorded in FORGES:
        name = str(recorded)
    else:
        root = Path((manifest or {}).get("root") or repo)
        name = (
            "beads"
            if (repo / ".beads").is_dir() or (root / ".beads").is_dir()
            else DEFAULT_FORGE
        )
    _selected[str(repo)] = name
    return name


def _implementation(repo: Path) -> str:
    """Returns the forge remembered for a checkout, detecting it if needed."""
    return _selected.get(str(repo)) or select(repo)


def slug(repo: Path) -> str | None:
    """Returns the GitHub owner and name behind a repository's origin remote.

    The forge is optional context, never authority. A checkout without an
    origin remote, a remote on another host, and a failing Git invocation are
    all ordinary outcomes that leave coordination unchanged, so this reports
    absence instead of raising.

    Args:
        repo: Repository or assigned worktree whose origin remote is read.

    Returns:
        The ``owner/name`` slug, or None when origin is missing, is not a
        GitHub remote, or cannot be read.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode:
        return None
    match = GITHUB_REMOTE.match(result.stdout.strip())
    return f"{match['owner']}/{match['name']}" if match else None


def _reachable(repo: Path) -> str | None:
    """Returns the forge project when a usable ``gh`` client is installed."""
    project = slug(repo)
    return project if project and shutil.which("gh") else None


def _beads_reachable() -> bool:
    """Reports whether the ``bd`` client is installed."""
    return shutil.which("bd") is not None


def branch_completion(repo: Path, branch: str) -> tuple[str, float] | None:
    """Reports the newest pull request opened from a lane branch.

    A lane branch outlives the work it was first used for, so the branch name
    alone cannot say whether the current work ended. Only the newest pull
    request describes the current use of the branch; an older merged or closed
    one belongs to a finished generation and must not speak for it. The caller
    correlates the reported creation time with the claim it is asking about.
    Only the GitHub forge opens pull requests, so every other forge reports
    None.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        branch: Lane branch whose pull requests are read.

    Returns:
        The newest pull request's state and creation time in Unix seconds, or
        None when the forge is unavailable, the branch has no pull request, or
        the response cannot be read.
    """
    if _implementation(repo) != "github":
        return None
    project = _reachable(repo)
    if not project:
        return None
    output = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            project,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "state,createdAt",
        ],
        5,
    )
    try:
        records = json.loads(output or "[]")
        newest = max(
            (
                (str(record["state"]), _epoch(record["createdAt"]))
                for record in records
                if record.get("state") and record.get("createdAt")
            ),
            key=lambda entry: entry[1],
            default=None,
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    return newest


def _epoch(value: str) -> float:
    """Converts a forge timestamp to Unix seconds, or raises ValueError."""
    return datetime.fromisoformat(value).timestamp()


def _run(args: list[str], timeout: int) -> str | None:
    """Runs a forge client, reporting absence instead of raising."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return None if result.returncode else result.stdout


def issue_title(repo: Path, number: str) -> str | None:
    """Returns the forge's title for an issue, when one is reachable.

    The lookup is best effort and read only. It is skipped entirely without a
    reachable client, and any failure of the client, its authentication, or
    its output reports absence. A resolved title is peer-supplied display
    context; it never decides ownership, so coordination must keep working
    with no network, no client, and no remote. The null forge always reports
    absence, so the ledger shows bare numbers.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        The issue title, clipped to 200 characters, or None when the forge is
        unavailable, refuses the request, or reports no usable title.
    """
    chosen = _implementation(repo)
    if chosen == "beads":
        return _beads_title(number)
    if chosen != "github":
        return None
    project = _reachable(repo)
    if project is None:
        return None
    output = _run(
        ["gh", "issue", "view", number, "--repo", project, "--json", "title"],
        15,
    )
    return _title_of(output)


def _title_of(output: str | None) -> str | None:
    """Reads a clipped title from a client's JSON reply, or None."""
    if output is None:
        return None
    try:
        record = json.loads(output)
        if isinstance(record, list):
            record = record[0]
        title = record["title"]
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    return title[:MAX_TITLE] if isinstance(title, str) else None


def _beads_title(number: str) -> str | None:
    """Reads one issue's title from the Beads ledger through ``bd``."""
    if not _beads_reachable():
        return None
    return _title_of(_run(["bd", "show", number, "--json"], 15))


def issue_pull_request_paths(repo: Path, number: str) -> list[str]:
    """Lists the files the pull requests that closed an issue touched.

    A claimed issue that already had pull requests names, through those pull
    requests, the paths the work tends to touch. The reading is best effort
    and read only: it is skipped without a GitHub origin or the ``gh`` client,
    and a slow or refusing forge reports nothing rather than raising. Only
    the GitHub forge has pull requests, so every other forge reports nothing.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        Sorted unique repository-relative paths, at most ``MAX_PATHS``, or an
        empty list when the forge is unavailable or no pull request refers to
        the issue.
    """
    if _implementation(repo) != "github":
        return []
    project = _reachable(repo)
    if project is None:
        return []
    output = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            project,
            "--search",
            f"closes #{number}",
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "files",
        ],
        15,
    )
    if output is None:
        return []
    paths: set[str] = set()
    try:
        for record in json.loads(output):
            for entry in record.get("files") or []:
                if isinstance(entry.get("path"), str):
                    paths.add(entry["path"])
    except (ValueError, TypeError, AttributeError):
        return []
    return sorted(paths)[:MAX_PATHS]


def open_issues(repo: Path, limit: int = MAX_OPEN_ISSUES) -> dict[str, dict]:
    """Reads the open issues the forge records, with their titles and labels.

    A lane about to open an issue needs to know whether the work is already
    tracked, and only the forge knows what is open. The whole list is read in
    one bounded call for the same reason ``issue_providers`` reads it that
    way. A forge that is missing, slow or unwilling reports nothing, and the
    caller states that it found nothing rather than that nothing exists.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        limit: Most open issues to read in the one call.

    Returns:
        Bare issue number to its recorded title and label names.
    """
    if _implementation(repo) != "github":
        return {}
    project = _reachable(repo)
    if project is None:
        return {}
    output = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            project,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,labels",
        ],
        15,
    )
    if output is None:
        return {}
    catalog: dict[str, dict] = {}
    try:
        for record in json.loads(output):
            catalog[str(int(record["number"]))] = {
                "title": str(record.get("title") or ""),
                "labels": sorted(
                    str(label["name"])
                    for label in record.get("labels") or []
                    if label.get("name")
                ),
            }
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}
    return catalog


def issue_providers(repo: Path, limit: int = MAX_OPEN_ISSUES) -> dict[str, str]:
    """Reads the provider each open issue declares through a label.

    An issue that must be worked by one assistant says so on the forge with a
    ``provider:NAME`` label, which is the operator's own labelling rather than
    anything this project writes. The whole open list is read in one bounded
    call, because a per-issue lookup would cost one client call per candidate.
    Absence is the normal answer: a forge that is missing, slow or unwilling
    declares no provider for anything and the reading lane ranks without it.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        limit: Most open issues to read in the one call.

    Returns:
        Bare issue number to the lowercased provider name it declares. An
        issue carrying no such label, or several, is absent.
    """
    if _implementation(repo) != "github":
        return {}
    project = _reachable(repo)
    if project is None:
        return {}
    output = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            project,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,labels",
        ],
        15,
    )
    if output is None:
        return {}
    declared: dict[str, str] = {}
    try:
        for record in json.loads(output):
            names = [
                str(label["name"])[len(PROVIDER_LABEL) :].strip().lower()
                for label in record.get("labels") or []
                if str(label.get("name", "")).startswith(PROVIDER_LABEL)
            ]
            if len(names) == 1 and names[0]:
                declared[str(int(record["number"]))] = names[0]
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}
    return declared


def assign(repo: Path, number: str) -> bool:
    """Records the operator's forge account as an issue's assignee.

    A claim is recorded in the local ledger first; this mirrors it onto the
    forge so a reader outside Agent Parley can see that the issue is being
    worked. The mirror is best effort and carries no authority: the ledger
    stays correct with no network, no client and no remote, and a rejected
    write changes no coordination state. The null forge accepts nothing.

    The forge sees one assignee, the operator's own account, because every
    lane runs under that account. A handoff between participants therefore
    changes the ledger owner without changing the forge assignee.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        True when the forge accepted the assignment, False when the forge is
        unavailable or refused it.
    """
    chosen = _implementation(repo)
    if chosen == "beads":
        return _beads_update(number, _operator())
    if chosen != "github":
        return False
    project = _reachable(repo)
    if project is None:
        return False
    return (
        _run(
            [
                "gh",
                "issue",
                "edit",
                number,
                "--repo",
                project,
                "--add-assignee",
                "@me",
            ],
            20,
        )
        is not None
    )


def unassign(repo: Path, number: str) -> bool:
    """Removes the operator's forge account from a released issue.

    This is the counterpart of :func:`assign` and carries the same best-effort
    contract. It removes only the account Agent Parley added, so an assignee
    a person set by hand is left in place.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        True when the forge accepted the removal, False when the forge is
        unavailable or refused it.
    """
    chosen = _implementation(repo)
    if chosen == "beads":
        return _beads_update(number, "")
    if chosen != "github":
        return False
    project = _reachable(repo)
    if project is None:
        return False
    return (
        _run(
            [
                "gh",
                "issue",
                "edit",
                number,
                "--repo",
                project,
                "--remove-assignee",
                "@me",
            ],
            20,
        )
        is not None
    )


def _operator() -> str:
    """Names the operator account a Beads assignment records."""
    try:
        return getpass.getuser()
    except OSError:
        return "agent-parley"


def _beads_update(number: str, assignee: str) -> bool:
    """Sets one Beads issue's assignee through ``bd``, best effort."""
    if not _beads_reachable():
        return False
    return (
        _run(["bd", "update", number, "--assignee", assignee], 20) is not None
    )


def comment(repo: Path, number: str, body: str) -> bool:
    """Adds one comment to an issue on the forge.

    The comment reproduces what a participant reported and states that a
    reported state is the participant's own account rather than review. It is
    best effort: an unreachable forge leaves the report recorded locally and
    unchanged, and the null forge records nothing.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.
        body: Comment text, already shaped by the caller.

    Returns:
        True when the forge accepted the comment, False when the forge is
        unavailable or refused it.
    """
    chosen = _implementation(repo)
    if chosen == "beads":
        if not _beads_reachable():
            return False
        return _run(["bd", "comment", number, body], 20) is not None
    if chosen != "github":
        return False
    project = _reachable(repo)
    if project is None:
        return False
    return (
        _run(
            [
                "gh",
                "issue",
                "comment",
                number,
                "--repo",
                project,
                "--body",
                body,
            ],
            20,
        )
        is not None
    )
