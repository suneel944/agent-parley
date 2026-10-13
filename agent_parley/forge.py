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
MAX_PULL_REQUESTS = 30
PULL_REQUEST_FIELDS = (
    "number,url,headRefName,headRefOid,mergeable,statusCheckRollup,"
    "latestReviews,closingIssuesReferences,files"
)
MAX_LANDINGS = 100
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE
)
UNLINKED = re.compile(
    r"<!--.*?(?:-->|\Z)|^ {0,3}(`{3,}|~{3,}).*?(?:^ {0,3}\1|\Z)|(`+).+?\2",
    re.DOTALL | re.MULTILINE,
)
TIMELINE_PAGE = 100
TIMELINE_PAGES = 10
FAILED_CHECKS = frozenset(
    {
        "FAILURE",
        "ERROR",
        "TIMED_OUT",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
    }
)
NOT_STARTED_CHECKS = frozenset({"ACTION_REQUIRED", "STARTUP_FAILURE"})
ACTIONS_JOB = re.compile(r"/actions/runs/(?P<run>\d+)/job/(?P<job>\d+)")
PROVIDER_LABEL = "provider:"
FORGES = ("github", "beads", "null")
DEFAULT_FORGE = "github"

GITHUB_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?|ssh://git@|git@)"
    r"(?:ssh\.)?github\.com(?::443)?[:/]"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
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
    evidence = branch_evidence(repo, branch)
    return (evidence["state"], evidence["created_at"]) if evidence else None


def branch_evidence(repo: Path, branch: str) -> dict | None:
    """Reports the newest pull request from a lane branch and how it ended.

    This is the same observation :func:`branch_completion` correlates with a
    claim, kept whole so a caller that must justify a decision can record what
    it saw rather than the verdict alone. The merge commit is present only for
    a merged pull request; a closed one carries an empty commit, because
    nothing was integrated and naming a commit would overstate the evidence.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        branch: Lane branch whose pull requests are read.

    Returns:
        The branch, the pull request's state, number and URL, its creation
        time in Unix seconds, and the merge commit where one exists. None when
        the forge is unavailable, the branch has no pull request, or the
        response cannot be read.
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
            "state,createdAt,mergeCommit,number,url",
        ],
        5,
    )
    try:
        records = json.loads(output or "[]")
        newest = max(
            (
                record
                for record in records
                if record.get("state") and record.get("createdAt")
            ),
            key=lambda record: _epoch(record["createdAt"]),
            default=None,
        )
        if not newest:
            return None
        return {
            "branch": branch,
            "state": str(newest["state"]),
            "created_at": _epoch(newest["createdAt"]),
            "commit": str((newest.get("mergeCommit") or {}).get("oid") or ""),
            "pull_request": int(newest.get("number") or 0),
            "url": str(newest.get("url") or ""),
        }
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def issue_completion(repo: Path, number: str) -> dict | None:
    """Reports whether one issue is closed and which pull request closed it.

    A lane may land a claim through a pull request from any branch, so the
    lane branch cannot say whether a claimed issue ended. The issue itself
    can: its state, when it closed, and the pull requests the forge links to
    it as closing it. The newest linked pull request supplies the head
    branch and merge commit that identify who landed the work. Only the
    GitHub forge links pull requests to issues, so every other forge reports
    None and the caller falls back to the lane branch.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        None when the forge cannot say, including when a linked closing
        pull request cannot be read, since a closed issue whose pull request
        is unknown must not read as closed without merging. Otherwise the
        issue state, `OPEN` or `CLOSED`, and for a closed issue the instant
        it closed in Unix seconds, together with the closing pull request's
        state, number, URL, head branch and merge commit, each empty when no
        pull request is linked. A closing pull request that merged reports
        the state `MERGED`.
    """
    if _implementation(repo) != "github":
        return None
    project = _reachable(repo)
    if not project:
        return None
    output = _run(
        [
            "gh",
            "issue",
            "view",
            number,
            "--repo",
            project,
            "--json",
            "state,closedAt,closedByPullRequestsReferences",
        ],
        5,
    )
    try:
        record = json.loads(output or "null")
        state = str(record["state"]).upper()
        if state != "CLOSED":
            return {"state": state}
        closed_at = _epoch(record["closedAt"])
        linked = [
            int(entry["number"])
            for entry in record.get("closedByPullRequestsReferences") or []
            if entry.get("number")
        ]
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    reading = {
        "state": "CLOSED",
        "closed_at": closed_at,
        "pull_request": 0,
        "url": "",
        "branch": "",
        "commit": "",
    }
    if not linked:
        linked = _closing_references(project, number, closed_at)
    if not linked:
        return reading
    pull = _run(
        [
            "gh",
            "pr",
            "view",
            str(max(linked)),
            "--repo",
            project,
            "--json",
            "state,number,url,headRefName,mergeCommit",
        ],
        5,
    )
    try:
        request = json.loads(pull or "null")
        state = str(request["state"]).upper()
        reading.update(
            state="MERGED" if state == "MERGED" else "CLOSED",
            pull_request=int(request.get("number") or 0),
            url=str(request.get("url") or ""),
            branch=str(request.get("headRefName") or ""),
            commit=str((request.get("mergeCommit") or {}).get("oid") or ""),
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    return reading


def _closing_references(project: str, number: str, closed_at: float) -> list:
    """Finds merged pull requests that name an issue with a closing keyword.

    GitHub links a closing pull request to an issue only when the pull
    request targets the default branch. Work that lands on an integration
    branch is closed by hand, so the link is empty. The issue timeline
    still records each pull request that referenced the issue. A pull
    request counts only when it merged in the same project no later than
    the issue closed and its body closes this issue by keyword, so a pull
    request that merely mentions the issue is never taken as its closer.

    Args:
        project: Forge project in `owner/name` form.
        number: Bare repository issue number.
        closed_at: Instant the issue closed, in Unix seconds.

    An issue worked by several lanes collects labels, assignments and
    progress comments, so its closing reference often lies past the first
    page. The timeline is read one page of `TIMELINE_PAGE` events at a time
    until a short page ends it, for at most `TIMELINE_PAGES` pages, so one
    reading stays bounded however long the timeline grows.

    Returns:
        The matching pull request numbers, empty when none match or the
        timeline cannot be read.
    """
    try:
        events = []
        for page in range(1, TIMELINE_PAGES + 1):
            output = _run(
                [
                    "gh",
                    "api",
                    f"repos/{project}/issues/{number}/timeline"
                    f"?per_page={TIMELINE_PAGE}&page={page}",
                ],
                5,
            )
            batch = json.loads(output or "[]")
            events.extend(batch)
            if len(batch) < TIMELINE_PAGE:
                break
        found = []
        for event in events:
            if event.get("event") != "cross-referenced":
                continue
            source = (event.get("source") or {}).get("issue") or {}
            merged = (source.get("pull_request") or {}).get("merged_at")
            home = (source.get("repository") or {}).get("full_name")
            if (
                merged
                and home == project
                and _epoch(merged) <= closed_at
                and number in closing_numbers(source.get("body") or "")
            ):
                found.append(int(source["number"]))
        return found
    except (ValueError, TypeError, AttributeError, KeyError):
        return []


def closing_numbers(body: str) -> list[str]:
    """Lists the issues a pull request body closes by keyword.

    GitHub links a closing keyword only in rendered prose, so a keyword
    inside an HTML comment, a fenced code block or an inline code span
    closes nothing and is removed before the keywords are read.

    Args:
        body: Markdown body of one pull request.

    Returns:
        Bare issue numbers in the order the prose names them.
    """
    return CLOSING.findall(UNLINKED.sub(" ", body))


def integration_landings(repo: Path, base: str) -> dict[str, dict] | None:
    """Reports which issues merged pull requests into a branch close.

    GitHub closes an issue from a closing keyword only when the pull request
    merges into the default branch, and links it to the issue only then, so
    work merged into a milestone's integration branch leaves its issue open
    and unlinked. One bounded request reads the newest `MAX_LANDINGS` pull
    requests merged into that branch, and each one's body names the issues
    it closes by keyword, whichever branch it came from. A pull request that
    merely mentions an issue is never taken as landing it. Only the GitHub
    forge opens pull requests, so every other forge reports None.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        base: Integration branch the pull requests merged into.

    Returns:
        Bare issue number to the newest merged pull request closing it: the
        state `MERGED`, the instant it merged in Unix seconds as
        `closed_at`, its number, URL, head branch, merge commit and the
        base branch. None when the forge is unavailable or the response
        cannot be read.
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
            "--base",
            base,
            "--state",
            "merged",
            "--limit",
            str(MAX_LANDINGS),
            "--json",
            "number,url,headRefName,mergeCommit,mergedAt,body",
        ],
        15,
    )
    try:
        records = json.loads(output or "null")
        if not isinstance(records, list):
            return None
        landed: dict[str, dict] = {}
        for record in records:
            if not record.get("mergedAt"):
                continue
            reading = {
                "state": "MERGED",
                "closed_at": _epoch(record["mergedAt"]),
                "pull_request": int(record["number"]),
                "url": str(record.get("url") or ""),
                "branch": str(record.get("headRefName") or ""),
                "commit": str(
                    (record.get("mergeCommit") or {}).get("oid") or ""
                ),
                "base": base,
            }
            for number in closing_numbers(record.get("body") or ""):
                kept = landed.get(number)
                if not kept or kept["closed_at"] < reading["closed_at"]:
                    landed[number] = reading
        return landed
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def open_pull_requests(repo: Path) -> list[dict] | None:
    """Reports the check, review and merge state of open pull requests.

    One bounded request reads at most `MAX_PULL_REQUESTS` open pull requests,
    newest first, with their head commit, the checks reported on it, the
    latest review of each reviewer, whether the forge can merge it, the
    issues it closes and the files it changes. A reading of exactly
    `MAX_PULL_REQUESTS` may be truncated, so the caller must not treat a pull
    request missing from it as closed; `pull_request` reads one by number.
    The caller decides which lane a pull request belongs to and what changed
    since its last reading. Only the GitHub forge opens pull requests, so
    every other forge reports None.

    Args:
        repo: Repository or assigned worktree that selects the forge project.

    Returns:
        One reading per open pull request: its number, URL, head branch and
        head commit, the checks verdict (`pending`, `green`, `red`, or
        `none` when nothing reported), the sorted names of failing checks,
        each unfinished check with its state and start time, each failing
        check with the forge's conclusion and whether it never started, the
        latest reviews as author, state and submission time, the merge state
        the forge reports, the bare numbers of the issues it closes and the
        sorted repository paths it changes. None when the forge is
        unavailable or the response cannot be read.
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
            "--state",
            "open",
            "--limit",
            str(MAX_PULL_REQUESTS),
            "--json",
            PULL_REQUEST_FIELDS,
        ],
        15,
    )
    try:
        records = json.loads(output or "null")
        if not isinstance(records, list):
            return None
        return [_pull_request_reading(record) for record in records]
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def pull_request(repo: Path, number: int) -> dict | None:
    """Reads one pull request by number in the shape of `open_pull_requests`.

    A truncated `open_pull_requests` reading omits the oldest open pull
    requests, so the caller re-reads one it already tracks here instead of
    treating its absence as closed.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Pull request number.

    Returns:
        The reading `open_pull_requests` would give while it is open, an
        empty mapping once the forge reports it merged or closed, and None
        when the forge is unavailable or the response cannot be read.
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
            "view",
            str(number),
            "--repo",
            project,
            "--json",
            f"state,{PULL_REQUEST_FIELDS}",
        ],
        15,
    )
    try:
        record = json.loads(output or "null")
        if not isinstance(record, dict):
            return None
        if record.get("state") != "OPEN":
            return {} if record.get("state") else None
        return _pull_request_reading(record)
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def _pull_request_reading(record: dict) -> dict:
    """Shapes one forge pull request record into an open reading.

    Args:
        record: One pull request object the GitHub client printed.

    Returns:
        The reading described by `open_pull_requests`.
    """
    checks, failing = _checks(record.get("statusCheckRollup"))
    return {
        "number": int(record["number"]),
        "url": str(record.get("url") or ""),
        "branch": str(record.get("headRefName") or ""),
        "sha": str(record.get("headRefOid") or ""),
        "checks": checks,
        "failing": failing,
        "pending": _pending(record.get("statusCheckRollup")),
        "failed": _failed(record.get("statusCheckRollup")),
        "reviews": [
            {
                "author": str((review.get("author") or {}).get("login") or ""),
                "state": str(review.get("state") or ""),
                "at": str(review.get("submittedAt") or ""),
            }
            for review in record.get("latestReviews") or []
        ],
        "mergeable": str(record.get("mergeable") or "UNKNOWN"),
        "issues": [
            str(entry["number"])
            for entry in record.get("closingIssuesReferences") or []
            if entry.get("number")
        ],
        "files": sorted(
            {
                str(entry["path"])
                for entry in record.get("files") or []
                if entry.get("path")
            }
        ),
    }


def _checks(rollup: list | None) -> tuple[str, list[str]]:
    """Reduces a pull request's reported checks to one verdict.

    A check run is pending until its status reads completed, and a commit
    status is pending while its state reads pending or expected. The verdict
    is `red` only once every check finished, so a lane is told once about a
    finished run rather than about each job as it fails.

    Args:
        rollup: The forge's `statusCheckRollup` entries, check runs and
            commit statuses mixed.

    Returns:
        The verdict and the sorted names of the failing checks, empty
        unless the verdict is `red`.
    """
    failing: set[str] = set()
    pending = False
    for entry in rollup or []:
        name = str(entry.get("name") or entry.get("context") or "unnamed")
        if "status" in entry:
            if str(entry.get("status") or "").upper() != "COMPLETED":
                pending = True
                continue
            outcome = str(entry.get("conclusion") or "").upper()
        else:
            outcome = str(entry.get("state") or "").upper()
            if outcome in {"", "PENDING", "EXPECTED"}:
                pending = True
                continue
        if outcome in FAILED_CHECKS:
            failing.add(name)
    if pending:
        return "pending", []
    if failing:
        return "red", sorted(failing)
    return ("green" if rollup else "none"), []


def _pending(rollup: list | None) -> list[dict]:
    """Names each unfinished check on a pull request and when it began.

    Args:
        rollup: The forge's `statusCheckRollup` entries, check runs and
            commit statuses mixed.

    Returns:
        One entry per check `_checks` counts as pending: its name, its state
        in lower case (`queued`, `in_progress`, `pending` or `expected`) and
        its start in Unix seconds, or None when the forge reports no usable
        start time.
    """
    pending: list[dict] = []
    for entry in rollup or []:
        if "status" in entry:
            state = str(entry.get("status") or "").upper()
            if state == "COMPLETED":
                continue
        else:
            state = str(entry.get("state") or "").upper()
            if state not in {"", "PENDING", "EXPECTED"}:
                continue
        try:
            started: float | None = _epoch(str(entry.get("startedAt") or ""))
        except (ValueError, OverflowError, OSError):
            started = None
        pending.append(
            {
                "name": str(
                    entry.get("name") or entry.get("context") or "unnamed"
                ),
                "state": state.lower() or "pending",
                "started": started if started and started > 0 else None,
            }
        )
    return pending


def _failed(rollup: list | None) -> list[dict]:
    """Names each check `_checks` counts as failing, with the forge's outcome.

    Args:
        rollup: The forge's `statusCheckRollup` entries, check runs and
            commit statuses mixed.

    Returns:
        One entry per check whose outcome `_checks` counts toward a red
        verdict: its name, the forge's conclusion in lower case, and whether
        the forge reports the job never started (`action_required` or
        `startup_failure`) rather than having run and failed. A check run
        whose details link names an Actions run and job also carries both
        identifiers as `run` and `job`, so one job can be re-run.
    """
    failed: list[dict] = []
    for entry in rollup or []:
        if "status" in entry:
            if str(entry.get("status") or "").upper() != "COMPLETED":
                continue
            outcome = str(entry.get("conclusion") or "").upper()
        else:
            outcome = str(entry.get("state") or "").upper()
        if outcome not in FAILED_CHECKS:
            continue
        check = {
            "name": str(entry.get("name") or entry.get("context") or "unnamed"),
            "conclusion": outcome.lower(),
            "not_started": outcome in NOT_STARTED_CHECKS,
        }
        job = ACTIONS_JOB.search(str(entry.get("detailsUrl") or ""))
        if job:
            check["run"], check["job"] = job.group("run"), job.group("job")
        failed.append(check)
    return failed


def rerun_job(repo: Path, run: str, job: str) -> tuple[str, bool]:
    """Asks the forge to re-run one finished Actions job.

    The request goes through the operator's own `gh` authentication, the
    same client every forge read uses, and adds no flag that bypasses a
    repository rule. Only the GitHub forge runs Actions jobs.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        run: Actions run identifier.
        job: Actions job identifier within that run.

    Returns:
        The exact command an operator can run to repeat the request, and
        whether the forge accepted it. The forge is not called when the
        identifiers are not numeric or no GitHub project is reachable.
    """
    project = slug(repo) if _implementation(repo) == "github" else None
    command = ["gh", "run", "rerun", run, "--job", job]
    if project:
        command += ["--repo", project]
    shown = " ".join(command)
    if not (project and run.isdigit() and job.isdigit() and shutil.which("gh")):
        return shown, False
    return shown, _run(command, 15) is not None


def review_feedback(repo: Path, number: int) -> tuple[str, list[dict] | None]:
    """Reads the bodies and inline comments of a pull request's reviews.

    Two bounded requests read at most one page each of the reviews and of
    the inline review comments, through the operator's own `gh`
    authentication. The text is the reviewers' own and is returned as data;
    the caller decides how much of it to quote. Only the GitHub forge has
    pull request reviews.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Pull request number.

    Returns:
        The exact commands that return the same reviews and comments, and
        one entry per review with its author, state, submission time, body
        and inline comments, each comment as path, line and body. None in
        place of the reviews when the forge is unavailable or the response
        cannot be read.
    """
    project = _reachable(repo) if _implementation(repo) == "github" else None
    base = f"repos/{project or 'OWNER/NAME'}/pulls/{number}"
    shown = f"gh api {base}/reviews; gh api {base}/comments"
    if not project:
        return shown, None
    reviews = _run(["gh", "api", f"{base}/reviews?per_page=100"], 15)
    comments = _run(["gh", "api", f"{base}/comments?per_page=100"], 15)
    try:
        records = json.loads(reviews or "null")
        notes = json.loads(comments or "null")
        if not isinstance(records, list) or not isinstance(notes, list):
            return shown, None
        inline: dict[int, list[dict]] = {}
        for note in notes:
            owner = int(note.get("pull_request_review_id") or 0)
            inline.setdefault(owner, []).append(
                {
                    "path": str(note.get("path") or ""),
                    "line": note.get("line") or note.get("original_line"),
                    "body": str(note.get("body") or ""),
                }
            )
        return shown, [
            {
                "author": str((record.get("user") or {}).get("login") or ""),
                "state": str(record.get("state") or ""),
                "at": str(record.get("submitted_at") or ""),
                "body": str(record.get("body") or ""),
                "comments": inline.get(int(record.get("id") or 0), []),
            }
            for record in records
        ]
    except (ValueError, TypeError, AttributeError):
        return shown, None


def failed_log(repo: Path, run: str, job: str) -> tuple[str, str | None]:
    """Reads the log of one Actions job's failed steps.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        run: Actions run identifier.
        job: Actions job identifier within that run.

    Returns:
        The exact command that returns the same log, and the log text, or
        None in its place when the identifiers are not numeric, no GitHub
        project is reachable or the forge returns nothing.
    """
    project = _reachable(repo) if _implementation(repo) == "github" else None
    command = ["gh", "run", "view", run, "--job", job, "--log-failed"]
    if project:
        command += ["--repo", project]
    shown = " ".join(command)
    if not (project and run.isdigit() and job.isdigit()):
        return shown, None
    return shown, _run(command, 30) or None


JOB_TIMEOUT = re.compile(r"^\s*timeout-minutes:\s*(\d+)\s*$", re.MULTILINE)


def checks_timeout(repo: Path) -> float | None:
    """Reads the longest job timeout the repository's workflows declare.

    Only literal `timeout-minutes` values in `.github/workflows` count; an
    expression or a job without one is not read, so a repository that sets
    none reports None rather than the forge's own six-hour default.

    Args:
        repo: Repository checkout whose workflows are read.

    Returns:
        The longest declared job timeout in seconds, or None when no
        workflow declares one or none can be read.
    """
    minutes: list[int] = []
    for path in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        minutes.extend(int(value) for value in JOB_TIMEOUT.findall(text))
    return 60.0 * max(minutes) if minutes else None


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


MAX_LINKED = 5
MENTION = re.compile(r"(?<![\w/&])#(\d+)\b")


def issue_details(repo: Path, number: str) -> dict | None:
    """Reads one issue's text for a launch to quote, when a forge answers.

    The read is best effort and read only, like `issue_title`: a missing
    client, a refusal or unusable output reports absence. On GitHub it
    returns the title, body and comments from one `gh issue view`, plus the
    titles of up to `MAX_LINKED` issues the body mentions by number; on
    Beads only the title. Callers bound the text before using it.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        A dict with ``title``, ``body``, ``comments`` (dicts with ``author``
        and ``body``, oldest first), ``linked`` (number to title) and
        ``command``, the client command that prints the full issue, or None
        when no forge returns a usable reading.
    """
    chosen = _implementation(repo)
    if chosen == "beads":
        title = _beads_title(number)
        command = f"bd show {number}"
        return (
            None
            if title is None
            else {
                "title": title,
                "body": "",
                "comments": [],
                "linked": {},
                "command": command,
            }
        )
    project = _reachable(repo) if chosen == "github" else None
    if project is None or not number.isdigit():
        return None
    output = _run(
        [
            "gh",
            "issue",
            "view",
            number,
            "--repo",
            project,
            "--json",
            "title,body,comments",
        ],
        15,
    )
    try:
        record = json.loads(output or "")
        title, body = record["title"], record.get("body") or ""
        comments = [
            {
                "author": str((entry.get("author") or {}).get("login") or ""),
                "body": str(entry.get("body") or ""),
            }
            for entry in record.get("comments") or []
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    linked: dict[str, str] = {}
    for mention in MENTION.findall(body):
        if mention == number or mention in linked:
            continue
        if len(linked) == MAX_LINKED:
            break
        linked[mention] = issue_title(repo, mention) or ""
    return {
        "title": title[:MAX_TITLE],
        "body": body,
        "comments": comments,
        "linked": linked,
        "command": f"gh issue view {number} --repo {project} --comments",
    }


def issue_labels(repo: Path, number: str) -> list[str] | None:
    """Reads the label names one GitHub issue carries, best effort.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        The sorted label names, or None when no GitHub project answers.
    """
    project = _reachable(repo) if _implementation(repo) == "github" else None
    if project is None or not number.isdigit():
        return None
    output = _run(
        ["gh", "issue", "view", number, "--repo", project, "--json", "labels"],
        15,
    )
    try:
        labels = json.loads(output or "")["labels"] or []
        return sorted({str(label["name"]) for label in labels})
    except (ValueError, TypeError, KeyError):
        return None


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
    return open_issue_catalog(repo, limit) or {}


def open_issue_catalog(
    repo: Path, limit: int = MAX_OPEN_ISSUES, timeout: int = 15
) -> dict[str, dict] | None:
    """Reads the forge's open issues, telling absence apart from none open.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        limit: Most open issues to read in the one call.
        timeout: Seconds the one client call may take.

    Returns:
        Bare issue number to its recorded title and label names, an empty
        mapping when the forge reports no open issue, or None when the
        project has no GitHub forge, no client, or the call failed.
    """
    if _implementation(repo) != "github":
        return None
    project = _reachable(repo)
    if project is None:
        return None
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
        timeout,
    )
    if output is None:
        return None
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
        return None
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


def assigned(repo: Path, number: str) -> bool:
    """Reports whether the operator's account already holds an issue.

    Every lane runs under the operator's own account, so an assignment a
    person set by hand and one a claim added name the same account. The
    claim reads this before it assigns, so a release removes only an
    assignment the claim added. The lookup is best effort and read only.

    Args:
        repo: Repository or assigned worktree that selects the forge project.
        number: Bare repository issue number.

    Returns:
        True only when the GitHub forge reports the signed-in account among
        the issue's assignees; False for any other forge or any failure.
    """
    if _implementation(repo) != "github":
        return False
    project = _reachable(repo)
    if project is None:
        return False
    login = _run(["gh", "api", "user", "--jq", ".login"], 15)
    output = _run(
        [
            "gh",
            "issue",
            "view",
            number,
            "--repo",
            project,
            "--json",
            "assignees",
        ],
        15,
    )
    if not login or output is None:
        return False
    try:
        names = {
            str(person["login"]) for person in json.loads(output)["assignees"]
        }
    except (ValueError, TypeError, KeyError):
        return False
    return login.strip() in names


def unassign(repo: Path, number: str) -> bool:
    """Removes the operator's forge account from a released issue.

    This is the counterpart of :func:`assign` and carries the same best-effort
    contract. The claim path calls it only for an assignment its own claim
    added, as `assigned` read it, so an assignee a person set by hand is left
    in place.

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


def report_comment(summary: str, evidence: str) -> str:
    """Shapes one lane's ready report for the issue it claims.

    The comment reproduces the lane's own summary and evidence and adds no
    assessment of its own, so a reader on the forge sees what was reported and
    what that report is worth. It names neither the participant nor the
    provider that produced the work: which assistant wrote a change belongs in
    coordination state, where `top` and `status` read it, and never on the
    user's forge.

    Args:
        summary: The lane's account of its result.
        evidence: The verification evidence the lane recorded.

    Returns:
        Markdown for the issue comment.
    """
    return (
        "Reported ready for review.\n\n"
        f"{summary.strip()}\n\n"
        "Verification recorded by the lane:\n\n"
        f"{evidence.strip()}\n\n"
        "A reported state is the participant's own account of its lane. It is "
        "neither review nor independent verification."
    )
