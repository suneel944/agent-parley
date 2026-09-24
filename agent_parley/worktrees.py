"""Creates, restores, pauses, restarts and retires participant worktrees."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_parley import BridgeError
from agent_parley.copilot import release_copilot
from agent_parley.lazy import DeferredCallable, deferred, deferred_module

if TYPE_CHECKING:
    import contextlib
    import hashlib
    import json
    import shlex
    import shutil
    import subprocess

    from agent_parley import (
        approvals,
        checkpoints,
        forge,
        issues,
        metrics,
        process,
        reclaim,
        roster,
        state,
        store,
        supervision,
        terminal,
    )
    from agent_parley.checkpoints import (
        branch_head,
        current_branch,
        lane_branch,
    )
    from agent_parley.issues import parse_issue, snapshot
    from agent_parley.state import lock, write_json
else:
    contextlib = deferred_module("contextlib")
    hashlib = deferred_module("hashlib")
    json = deferred_module("json")
    shlex = deferred_module("shlex")
    shutil = deferred_module("shutil")
    subprocess = deferred_module("subprocess")
    approvals = deferred("approvals")
    checkpoints = deferred("checkpoints")
    forge = deferred("forge")
    issues = deferred("issues")
    metrics = deferred("metrics")
    process = deferred("process")
    reclaim = deferred("reclaim")
    roster = deferred("roster")
    state = deferred("state")
    store = deferred("store")
    supervision = deferred("supervision")
    terminal = deferred("terminal")
    branch_head = DeferredCallable(checkpoints, "branch_head")
    current_branch = DeferredCallable(checkpoints, "current_branch")
    lane_branch = DeferredCallable(checkpoints, "lane_branch")
    parse_issue = DeferredCallable(issues, "parse_issue")
    snapshot = DeferredCallable(issues, "snapshot")
    lock = DeferredCallable(state, "lock")
    write_json = DeferredCallable(state, "write_json")

VERIFY_TIMEOUT = 1800
INIT_OUTPUT_LINES = 20
GIT_SECONDS = 30


def git(repo: Path, *args: str) -> str:
    """Runs Git in a repository and returns stripped stdout.

    Args:
        repo: Working directory for Git.
        *args: Individual Git arguments, never shell-expanded.

    Returns:
        Command output with surrounding whitespace removed.

    Raises:
        BridgeError: If Git exits unsuccessfully.
        subprocess.TimeoutExpired: If Git exceeds the command timeout.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=(
            None
            if args and args[0] in {"push", "pull", "fetch"}
            else GIT_SECONDS
        ),
        check=False,
    )
    if result.returncode:
        raise BridgeError(result.stderr.strip() or "Git command failed.")
    return result.stdout.strip()


def has_branch(repo: Path, branch: str) -> bool:
    """Reports whether a branch still exists in a repository."""
    return bool(
        git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/{branch}",
        )
    )


def preserve_pending(root: Path) -> str | None:
    """Stashes pending base-checkout work so lanes can start from HEAD.

    Registration reads committed HEAD, so pending changes would otherwise
    never reach a lane. The changes are stashed rather than discarded. The
    stash stack is shared by every worktree of the repository, so the entry
    carries a unique message and the returned account restores it by name
    rather than by position. The name is the full object name: Git reads a
    bare decimal argument to ``git stash apply`` as a reflog position, so an
    abbreviation made only of digits would restore some other entry or fail
    outright.

    Args:
        root: Common repository root, which is always the base checkout.

    Returns:
        An account of the preserved entry, or None if nothing was pending.

    Raises:
        BridgeError: If Git leaves changes in the checkout after stashing.
        subprocess.TimeoutExpired: If Git exceeds the command timeout.
    """
    if not git(root, "status", "--porcelain"):
        return None
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    git(
        root,
        "stash",
        "push",
        "--include-untracked",
        "--message",
        f"agent-parley pending work {stamp}",
    )
    if git(root, "status", "--porcelain"):
        raise BridgeError(
            f"The checkout at {root} still holds changes that Git cannot "
            "stash. Commit or preserve them first; worktrees start at HEAD."
        )
    entry = git(root, "rev-parse", "refs/stash")
    return (
        f"Preserved your pending changes as stash entry {entry}; worktrees "
        f"start at HEAD. Restore them with `git -C "
        f"{shlex.quote(str(root))} stash apply {entry}`, which names this "
        "entry rather than whichever one is on top of the shared stack."
    )


def drift(name: str, participant: dict, actual: str) -> str:
    """Builds an actionable message for a lane that left its branch.

    Args:
        name: Participant that owns the lane.
        participant: Manifest entry holding the lane and assigned branch.
        actual: Branch the lane currently has.

    Returns:
        A message naming both branches and the repair commands.
    """
    return (
        f"{name} lane is on {actual!r}, expected "
        f"{participant['branch']!r}. Run "
        f"`agent-parley participant restore {name}` to return it, or "
        f"`agent-parley participant retire {name}` to drop the lane. "
        "Both preserve committed and uncommitted work; neither discards."
    )


def session_busy(name: str) -> str:
    """Builds the refusal used while a participant still holds a session.

    Args:
        name: Participant that owns the lane.

    Returns:
        The message reported when that participant is still working.
    """
    return f"{name} has a running session; stop that terminal first."


def held_claim(directory: Path, name: str) -> dict:
    """Names the claim a lane's integration record belongs to.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.

    Returns:
        The issue and claim identifier the lane currently holds, or empty
        fields when it holds none. A record with no claim is reported as
        unknown by history rather than being attached to a guessed one.
    """
    owned = sorted(
        (
            (number, record)
            for number, record in snapshot(directory)["issues"].items()
            if record["owner"] == name
        ),
        key=lambda item: int(item[0]),
    )
    if not owned:
        return {"issue": None, "claim_id": None}
    number, record = owned[0]
    return {"issue": int(number), "claim_id": record.get("claim_id")}


def exact_claim(directory: Path, name: str, issue: str = "") -> dict:
    """Returns one exact owned claim or refuses an ambiguous selection.

    Args:
        directory: Private state directory for the common repository.
        name: Participant that owns the lane.
        issue: Explicit issue selection, or empty to infer a sole claim.

    Returns:
        Issue number and claim identifier, or empty fields when no claim is
        held and none was requested.

    Raises:
        BridgeError: If the selection is not currently owned or ownership is
            ambiguous.
    """
    owned = {
        number: record
        for number, record in snapshot(directory)["issues"].items()
        if record["owner"] == name
    }
    if issue:
        number = parse_issue(issue)
        if number not in owned:
            raise BridgeError(f"Issue #{number} is not owned by {name}.")
    elif len(owned) > 1:
        raise BridgeError(
            f"{name} owns multiple issues; name one with --issue."
        )
    elif not owned:
        return {"issue": None, "claim_id": None}
    else:
        number = next(iter(owned))
    return {"issue": int(number), "claim_id": owned[number].get("claim_id")}


def verify_base(
    root: Path, command: list[str], integrated: bool = False
) -> None:
    """Runs a repository's verification command in the base checkout.

    Executing a configured command is a different trust decision from reading
    Git state, so the gate is a separate step that never rewrites, resets or
    stages anything itself. Run before a merge it reports the checkout as it
    stands, which is not a claim about the merged result; run after one it
    reports the integrated result itself. The command is run as an argument
    list without a shell, and no flag skips it: a repository that configures
    a gate always pays it.

    Args:
        root: Common repository root, which is always the base checkout.
        command: Argument tokens recorded in the project manifest.
        integrated: Whether the run follows a merge, which decides whether a
            failure reports that nothing was merged or that the merge stands
            and is unverified. Nothing is ever reset or reverted either way.

    Raises:
        BridgeError: If the command cannot run, or if it exits non-zero.
        subprocess.TimeoutExpired: If verification exceeds its timeout.
    """
    quoted = shlex.join(command)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            text=True,
            timeout=VERIFY_TIMEOUT,
            check=False,
        )
    except OSError as exc:
        raise BridgeError(
            f"The verification command for the base checkout at {root} could "
            f"not run: {exc}. Correct it with `agent-parley verify set`, then "
            "rerun; merge never skips verification."
        ) from None
    if not result.returncode:
        return
    outcome = (
        "The merge commits already recorded stand and are unverified; "
        "nothing was reset or reverted."
        if integrated
        else "Nothing was merged."
    )
    raise BridgeError(
        f"Verification failed in the base checkout at {root}: `{quoted}` "
        f"exited {result.returncode}. Fix it and rerun; merge never skips "
        f"verification. {outcome} See the command output above."
    )


def initialize_lane(lane: Path, command: list[str], base: Path) -> None:
    """Prepares a newly created lane before its native client starts.

    Every real repository needs more than a bare checkout before an agent can
    work in it: dependencies installed, an untracked environment file copied,
    a database migrated. Doing that once here costs the same setup once per
    lane instead of spending the first turns of every session on it, and makes
    every lane start from the same state.

    The command runs as an argument list without a shell, exactly as the
    verification gate does, and no flag skips it. It runs only when a lane is
    created, never on a resume. The base checkout is offered through
    AGENT_PARLEY_BASE so a command can copy a file Git does not track. A
    non-zero exit refuses the launch and leaves the worktree in place, because
    an operator needs to look at what the command did before it failed.

    Args:
        lane: Freshly created worktree the command runs in.
        command: Argument tokens recorded in the project manifest.
        base: Common repository root the lane was created from.

    Raises:
        BridgeError: If the command cannot run, or if it exits non-zero.
        subprocess.TimeoutExpired: If initialization exceeds its timeout.
    """
    quoted = shlex.join(command)
    try:
        result = subprocess.run(
            command,
            cwd=lane,
            env={**os.environ, "AGENT_PARLEY_BASE": str(base)},
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
            check=False,
        )
    except OSError as exc:
        raise BridgeError(
            f"The lane initialization command could not run in {lane}: {exc}. "
            "Correct it with `agent-parley init set`, then rerun. The "
            "worktree is left in place for inspection."
        ) from None
    if not result.returncode:
        return
    tail = "\n".join(
        (result.stdout + result.stderr).splitlines()[-INIT_OUTPUT_LINES:]
    )
    raise BridgeError(
        f"Lane initialization failed in {lane}: `{quoted}` exited "
        f"{result.returncode}, so the lane was not started. The worktree is "
        "left in place for inspection. Last output:\n" + tail
    )


class Worktrees:
    """Owns each participant's worktree, branch and session lifecycle.

    The launcher class combines this with its other command groups, so every
    group reaches the shared state root and the project paths through here.

    Attributes:
        home: Resolved private state directory.
        config: Local HTTP port and bearer credential.
        url: Loopback HTTP origin of the mail server.
    """

    home: Path
    config: dict[str, Any]
    url: str

    if TYPE_CHECKING:

        def server_process(self) -> process.ServerProcess | None:
            """Returns the recorded server only if its identity matches."""

        def health(self) -> dict:
            """Reads the service's own account of itself."""

        def up(self) -> None:
            """Starts the mail server with bounded readiness checking."""

        def say(
            self,
            repo: Path,
            name: str,
            text: str,
            subject: str = "",
            key: str = "",
            ack: bool = False,
            within: float | None = None,
            *,
            after: float | None = None,
            at: float | None = None,
            when_released: str = "",
            unless_reported: bool = False,
            every: float | None = None,
            until: float | None = None,
        ) -> dict:
            """Writes one operator message into a participant's lane inbox."""

        def history(
            self,
            repo: Path,
            subject: str = "",
            value: str = "",
            *,
            kinds: tuple[str, ...] = (),
            participant: str = "",
            provider: str = "",
            issue: str = "",
            window: float = 0.0,
        ) -> dict:
            """Reads ownership history for one issue, lane or claim."""

        def launch(
            self,
            agent: str,
            repo: Path,
            task: str,
            provider: str | None = None,
            credential: str | None = None,
            *,
            resume: bool = False,
        ) -> int:
            """Runs one participant's native CLI in its persistent lane."""

    def project(self, repo: Path, *, create: bool = True) -> tuple[Path, Path]:
        """Returns repository paths, optionally creating its state directory.

        Args:
            repo: Main checkout or linked worktree.
            create: Whether to create the private project directory.

        Returns:
            Main worktree and shared state directory paths.
        """
        common = Path(
            git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        if git(repo, "rev-parse", "--is-bare-repository") == "true":
            raise BridgeError(
                "Use a non-bare repository with an initial commit."
            )
        root = Path(
            git(repo, "worktree", "list", "--porcelain", "-z").split("\0")[0][
                9:
            ]
        )
        key = hashlib.sha256(str(common.resolve()).encode()).hexdigest()[:16]
        directory = self.home / "projects" / key
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return root, directory

    def setup(self, repo: Path) -> dict:
        """Creates or verifies the project manifest for a repository.

        Args:
            repo: Main checkout or linked worktree of the target repository.

        Returns:
            Manifest containing the common root, base, and participants.

        Raises:
            BridgeError: If the checkout or an existing lane is unusable.
        """
        root, directory = self.project(repo)
        with lock(directory / "setup.lock"):
            return roster.expand(self._project(root, directory, preserve=True))

    def _project(
        self,
        root: Path,
        directory: Path,
        verify: set[str] | None = None,
        preserve: bool = False,
    ) -> dict:
        """Reads or creates the manifest while the setup lock is held.

        Args:
            root: Common repository root.
            directory: Private state directory for the repository.
            verify: Participants whose branch must match, or None for all.
            preserve: Whether creation is allowed, preserving pending work
                in a stash first. False requires an existing manifest.

        Returns:
            Manifest using the participant roster layout.

        A retired participant is never checked for branch drift. It kept no
        worktree to be on the wrong branch of, and reading its pruned lane
        would report an unavailable worktree as a drifted one.

        Raises:
            BridgeError: If a checked lane left its assigned branch, or no
                manifest exists and creation is not allowed.
        """
        path = directory / "project.json"
        if path.exists():
            data = roster.normalize(json.loads(path.read_text()))
            participants = data["participants"]
            names = (
                set(participants)
                if verify is None
                else verify & set(participants)
            )
            for name in sorted(names):
                participant = participants[name]
                if roster.retired(participant):
                    continue
                actual = lane_branch(Path(participant["lane"]))
                if actual != participant["branch"]:
                    raise BridgeError(drift(name, participant, actual))
            return data
        if not preserve:
            return roster.read(directory)
        preserved = preserve_pending(root)
        if preserved:
            print(preserved, file=sys.stderr, flush=True)
        data = {
            "version": roster.MANIFEST_VERSION,
            "root": str(root),
            "base": git(root, "rev-parse", "--verify", "HEAD"),
            "verify": [],
            "initialize": [],
            "forge": forge.select(root),
            "participants": {},
        }
        write_json(path, data)
        return data

    def add_participant(
        self,
        repo: Path,
        name: str,
        provider: str | None = None,
        credential: str | None = None,
    ) -> dict:
        """Adds one lane for a participant without touching existing lanes.

        This is also how a retired participant is re-admitted: naming it again
        with the provider and account it already had clears the retirement and
        restores its worktree, so a lane that retired itself comes back the
        same way it was first admitted.

        Args:
            repo: Main checkout or linked worktree of the target repository.
            name: Participant name, unique within this project.
            provider: Provider definition; defaults to the registered one, or
                to a provider named exactly like the participant.
            credential: Credential profile selecting one account; defaults to
                the profile already registered for this participant.

        Returns:
            Manifest containing the common root, base, and participants.

        Raises:
            BridgeError: If the name, provider, lane, or branch is unusable.
        """
        roster.identifier(name, "Participant name")
        root, directory = self.project(repo)
        with lock(directory / "setup.lock"):
            data = self._project(root, directory, verify={name}, preserve=True)
            participants = data["participants"]
            existing = participants.get(name)
            provider = provider or (existing or {}).get("provider") or name
            if credential is None:
                credential = (existing or {}).get("credential")
            roster.provider(self.home, provider)
            if credential is not None:
                roster.credential(self.home, credential)
            if existing:
                if (
                    existing["provider"] != provider
                    or existing["credential"] != credential
                ):
                    raise BridgeError(
                        f"Participant {name} already uses provider "
                        f"{existing['provider']} with "
                        f"{existing['credential'] or 'the default account'}."
                    )
                if roster.retired(existing):
                    self._readmit(root, directory, data, name)
                return roster.expand(data)
            if len(participants) >= roster.MAX_PARTICIPANTS:
                raise BridgeError(
                    "This project already has "
                    f"{roster.MAX_PARTICIPANTS} participants."
                )
            if any(
                participant["display"] == name
                for participant in participants.values()
            ):
                raise BridgeError(f"Identity {name} is already registered.")
            lane = directory / name
            refs = set(
                git(
                    root,
                    "for-each-ref",
                    "--format=%(refname:short)",
                    "refs/heads",
                ).splitlines()
            )
            branch = roster.next_lane_branch(data, directory.name, refs)
            if lane.exists():
                raise BridgeError(
                    f"Existing lane directory for {name}; preserve or remove "
                    f"the worktree at {lane} before adding this participant."
                )
            git(root, "worktree", "add", "-b", branch, str(lane), data["base"])
            if data.get("initialize"):
                initialize_lane(lane, data["initialize"], root)
            participants[name] = {
                "provider": provider,
                "display": name,
                "lane": str(lane),
                "branch": branch,
                "credential": credential,
                "scheme": "lane",
            }
            try:
                write_json(directory / "project.json", data)
            except OSError:
                print(
                    f"Lane preserved without registration: {lane}",
                    file=sys.stderr,
                )
                raise
            return roster.expand(data)

    def _readmit(
        self, root: Path, directory: Path, data: dict, name: str
    ) -> None:
        """Returns a retired participant to service under its own lane.

        Retirement pruned the worktree when it was clean and left the branch
        alone, so re-admission adds the worktree back on that same branch and
        any commits it carried are exactly where the lane left them. A branch
        that no longer exists is created again from the project base. The
        credential is not reissued here; the next launch registers one, which
        is the only path that has ever issued a lane's credential.

        Args:
            root: Common repository root.
            directory: Private state directory for the repository.
            data: Manifest being updated, under the held setup lock.
            name: Retired participant being re-admitted.
        """
        participant = data["participants"][name]
        lane = Path(participant["lane"])
        branch = participant["branch"]
        if not lane.exists():
            git(root, "worktree", "prune")
            if has_branch(root, branch):
                git(root, "worktree", "add", str(lane), branch)
            else:
                git(
                    root,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(lane),
                    data["base"],
                )
            if data.get("initialize"):
                initialize_lane(lane, data["initialize"], root)
        participant.pop("retired", None)
        write_json(directory / "project.json", data)

    def _lane(self, repo: Path, name: str) -> tuple[Path, dict, dict]:
        """Resolves one participant's state directory and manifest entry."""
        _, directory = self.project(repo)
        data = roster.read(directory)
        participant = data["participants"].get(name)
        if participant is None:
            raise BridgeError(
                f"{name} is not a participant in this project; "
                "run agent-parley participant list."
            )
        return directory, data, participant

    def _reviewed(self, directory: Path, data: dict, name: str) -> dict:
        """Reads the operator decision standing against a lane's work now."""
        branch = data["participants"][name]["branch"]
        head = branch_head(Path(data["root"]), branch)
        return approvals.review(directory, data, name, head)

    def _require_approval(
        self, directory: Path, data: dict, name: str, step: str
    ) -> None:
        """Refuses an integration step the operator has not approved.

        The decision is read again here, under the lane's session exclusion
        and immediately before the branch is merged or pushed, so an approval
        recorded for earlier commits cannot carry a later head into the base
        repository or the forge.

        Args:
            directory: Private state directory for the common repository.
            data: Project manifest holding this participant.
            name: Participant whose work would be integrated.
            step: Step being attempted, ``merge`` or ``pr``.

        Raises:
            BridgeError: If the project requires approval for this step and no
                matching decision is recorded, if the recorded decision is a
                rejection, or if the lane's log cannot be read.
        """
        if step not in data["approval"]:
            return
        refusal = approvals.refusal(
            name, step, self._reviewed(directory, data, name)
        )
        if refusal:
            raise BridgeError(refusal)

    def restore(self, repo: Path, name: str) -> str:
        """Returns a drifted lane to its branch without discarding work.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane must return to its bridge branch.

        Returns:
            An account of what changed.

        Raises:
            BridgeError: If the lane is busy, dirty, missing, or holds commits
                the assigned branch does not.
        """
        directory, _, participant = self._lane(repo, name)
        lane = Path(participant["lane"])
        branch = participant["branch"]
        with lock(
            directory / f"{name}.session.lock",
            f"{name} has a running session; stop that terminal first.",
        ):
            if not lane.exists():
                raise BridgeError(
                    f"{name} has no worktree at {lane}. Retire the "
                    "participant, then add it again."
                )
            actual = lane_branch(lane)
            if actual == branch:
                return f"{name} is already on {branch}."
            if not has_branch(lane, branch):
                raise BridgeError(
                    f"Branch {branch} no longer exists. Recover it from the "
                    f"reflog, or retire {name}, follow any kept-branch "
                    "recovery instructions, then add it again."
                )
            if git(lane, "status", "--porcelain"):
                raise BridgeError(
                    f"{name} has uncommitted changes on {actual}. Commit or "
                    "preserve them first; restore never discards work."
                )
            unmerged = git(lane, "log", "--oneline", f"{branch}..HEAD")
            if unmerged:
                head = git(lane, "rev-parse", "HEAD")
                raise BridgeError(
                    f"{name} holds commits that {branch} does not:\n"
                    f"{unmerged}\nKeep them first with `git -C "
                    f"{shlex.quote(str(lane))} branch KEEP_NAME {head}`, then "
                    "rerun; restore never discards work."
                )
            git(lane, "switch", branch)
            return f"{name} restored to {branch} from {actual}."

    def retire(self, repo: Path, name: str) -> str:
        """Removes a participant's lane while preserving any work it holds.

        Deliveries still unread or unacknowledged by the lane are marked
        superseded, because a lane that left answers nothing; the messages
        themselves stay readable.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane is retired.

        Returns:
            An account of what was removed and what was kept.

        Raises:
            BridgeError: If the lane is busy or holds uncommitted changes.
        """
        root, directory = self.project(repo, create=False)
        roster.read(directory)
        with lock(directory / "setup.lock"):
            data = self._project(root, directory, verify=set())
            participant = data["participants"].get(name)
            if participant is None:
                raise BridgeError(
                    f"{name} is not a participant in this project; "
                    "run agent-parley participant list."
                )
            lane = Path(participant["lane"])
            branch = participant["branch"]
            with lock(
                directory / f"{name}.session.lock",
                f"{name} has a running session; stop that terminal first.",
            ):
                if lane.exists():
                    if git(lane, "status", "--porcelain"):
                        raise BridgeError(
                            f"{name} has uncommitted changes. Commit or "
                            "preserve them first; retire never discards work."
                        )
                    git(root, "worktree", "remove", str(lane))
                git(root, "worktree", "prune")
                note = f"Branch {branch} was already gone."
                if has_branch(root, branch):
                    if git(
                        root, "log", "--oneline", f"{data['base']}..{branch}"
                    ):
                        note = (
                            f"Branch {branch} kept; it holds commits the "
                            "project base does not. Nothing renames or "
                            "deletes it, and a new lane takes the next free "
                            "branch name, so adding this participant again "
                            "leaves those commits exactly where they are."
                        )
                    else:
                        git(root, "branch", "-d", branch)
                        note = f"Branch {branch} deleted; it added no commits."
                store.revoke(self.home, data["root"], participant["display"])
                store.supersede_recipient(
                    self.home,
                    data["root"],
                    participant["display"],
                    f"{name} retired",
                )
                metrics.record_report(
                    directory,
                    name,
                    {
                        "kind": "integration",
                        "action": "retire",
                        **held_claim(directory, name),
                    },
                )
                with lock(directory / f"{name}-checkpoint.lock", timeout=1):
                    for suffix in (
                        "identity.json",
                        "activity.json",
                        "mcp.json",
                        "events.jsonl",
                        "events.1.jsonl",
                        "events.jsonl.tmp",
                        "events.1.jsonl.tmp",
                        "gemini-settings.json",
                        "amp-settings.json",
                    ):
                        (directory / f"{name}-{suffix}").unlink(missing_ok=True)
                    shutil.rmtree(
                        directory / f"{name}-opencode", ignore_errors=True
                    )
                    release_copilot(self.home, participant, name)
                del data["participants"][name]
                write_json(directory / "project.json", data)
            (directory / f"{name}-checkpoint.lock").unlink(missing_ok=True)
            (directory / f"{name}-integration.lock").unlink(missing_ok=True)
            (directory / f"{name}.session.lock").unlink(missing_ok=True)
            return f"Retired {name}. {note} Messages are preserved."

    def _record_operator(
        self, directory: Path, name: str, reason: checkpoints.Reason, note: str
    ) -> None:
        """Writes one operator lifecycle decision into the lane's event log."""
        checkpoints.record(
            directory,
            name,
            {"hook_event_name": "OperatorCommand"},
            reason,
            None,
            note,
        )

    def pause(self, repo: Path, name: str, *, resume: bool = False) -> str:
        """Refuses or restores a lane's coordination without ending it.

        A paused lane keeps its session, its claims and its reservations. It
        is refused the ability to act: every served coordination call and
        every native tool use comes back denied, naming the operator as the
        cause. Nothing is released on the lane's behalf, because pausing is
        not a handoff.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane is paused or restored.
            resume: Whether to clear the pause instead of setting it.

        Returns:
            An account of the lane's new state and what it still holds.

        Raises:
            BridgeError: If the participant does not exist.
        """
        directory, _, _ = self._lane(repo, name)
        with lock(directory / "setup.lock"):
            data = roster.read(directory)
            participant = data["participants"][name]
            if participant.get("paused", False) is not resume:
                state = "paused" if not resume else "not paused"
                return f"{name} is already {state}; nothing changed."
            participant["paused"] = not resume
            write_json(directory / "project.json", data)
        self._record_operator(
            directory,
            name,
            (
                checkpoints.Reason.OPERATOR_RESUMED
                if resume
                else checkpoints.Reason.OPERATOR_PAUSED
            ),
            "paused" if not resume else "",
        )
        if resume:
            return f"{name} is resumed and serves coordination calls again."
        held = self._holdings(directory, name)
        return (
            f"{name} is paused. Its session, claims and reservations are "
            f"retained and nothing was released. {held}"
        )

    def _holdings(self, directory: Path, name: str) -> str:
        """Describes what one lane still owns, for an operator to act on."""
        owned = sorted(
            (
                number
                for number, record in snapshot(directory)["issues"].items()
                if record["owner"] == name
            ),
            key=int,
        )
        claims = (
            "It still owns " + ", ".join(f"#{number}" for number in owned)
            if owned
            else "It owns no issue"
        )
        return (
            f"{claims}. Ownership moves only through an explicit release or "
            "an accepted handoff."
        )

    def stop(self, repo: Path, name: str) -> str:
        """Ends one lane's native session from the base checkout.

        The lane is told once that the operator is ending its session, then
        the recorded session process is signalled exactly as a normal exit
        signals it and given a bounded time to leave; a process that ignores
        that signal is sent `SIGKILL`, and the session record is cleared only
        once the process is verified gone. Identity is the recorded
        process ID together with its kernel creation time, checked here and
        again inside the platform's terminate step, so a recycled process ID
        is never signalled. The command-line check used to recognize the
        coordination server does not apply: a lane runs a native client, not
        this package.

        Claims and reservations stay owned. Ending a session is not a handoff,
        so what the lane still holds is reported for the operator to move
        deliberately. The command is recorded either way, including when it
        finds no session to end, so the ledger shows every operator action
        rather than only the ones that changed something.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose session is ended.

        Returns:
            An account of what was stopped and what the lane still holds.

        Raises:
            BridgeError: If the participant does not exist, or the recorded
                process did not exit even after `SIGKILL`.
        """
        directory, _, _ = self._lane(repo, name)
        path = directory / f"{name}-activity.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        pid = state.get("session_pid")
        ticks = str(state.get("session_ticks") or "")
        if (
            type(pid) is not int
            or not process.alive(pid, ticks)
            or supervision.rebooted(state)
        ):
            self._record_operator(
                directory,
                name,
                checkpoints.Reason.OPERATOR_STOPPED,
                "no verified session",
            )
            return (
                f"{name} has no verified running session to stop. "
                f"{self._holdings(directory, name)}"
            )
        with contextlib.suppress(BridgeError, OSError):
            self.say(repo, name, "The operator is ending this session.")
        process.ServerProcess(pid, ticks).stop()
        if process.alive(pid, ticks):
            raise BridgeError(
                f"{name}'s session process {pid} is still running after "
                "SIGTERM and SIGKILL; its session record is kept."
            )
        with lock(directory / f"{name}-checkpoint.lock", timeout=1):
            state = json.loads(path.read_text()) if path.exists() else {}
            state.update(activity="stopped", updated=time.time())
            state.pop("session_pid", None)
            state.pop("session_ticks", None)
            state.pop("launcher_pid", None)
            state.pop("launcher_ticks", None)
            write_json(path, state)
        self._record_operator(
            directory, name, checkpoints.Reason.OPERATOR_STOPPED, "stopped"
        )
        return (
            f"{name}'s session was ended from the base checkout. "
            f"{self._holdings(directory, name)}"
        )

    def restart(self, repo: Path, name: str, task: str = "") -> int:
        """Starts one lane again in its own worktree on its own branch.

        A restart is refused while a session is alive and current, because two
        clients in one worktree would fight over it. A session whose process
        is alive but whose evidence is stale past the project's
        `inactive_after` is a wedged client, such as one left in a native
        dialog or behind a system hang, so it is ended first through the same
        escalating stop the operator command uses.

        The lane must be on its assigned branch. Uncommitted work on that
        branch is the previous session's own and is exactly what a crash
        leaves behind, so it is not a reason to refuse: every claim the lane
        owns is captured into a recovery checkpoint first, the worktree is
        left as it is, and the new session is told where the checkpoint is.
        Nothing here resets, cleans, stashes or force-switches. Any recorded
        lane initialization command runs again, because a restart recreates
        the starting state.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose lane is started again.
            task: Opening instruction for the new session.

        Returns:
            The native client's exit status.

        Raises:
            BridgeError: If a current session is alive, a stale one cannot be
                ended, the uncommitted work cannot be captured, or the lane
                is not on its assigned branch.
        """
        from agent_parley import recovery

        directory, data, participant = self._lane(repo, name)
        state = checkpoints.activity(directory, name)
        if process.alive(
            state.get("session_pid"), state.get("session_ticks")
        ) and not supervision.rebooted(state):
            window = supervision.configuration(self.home, data)
            if not supervision.lane_state(state, window["inactive_after"])[
                "stale"
            ]:
                raise BridgeError(
                    f"{name} still has a live session. Run `agent-parley "
                    f"participant stop {name}` first; a restart never runs "
                    "two clients in one worktree."
                )
            self.stop(repo, name)
        lane = Path(participant["lane"])
        actual = current_branch(lane)
        if actual != participant["branch"]:
            raise BridgeError(drift(name, participant, actual))
        opening = task or terminal.PROMPT
        if git(lane, "status", "--porcelain"):
            saved = recovery.capture(directory, data, name)
            opening += (
                "\nThe previous session in this worktree ended with "
                "uncommitted changes; they were left in place, not reset. "
            )
            if saved:
                opening += "Recovery checkpoints: " + "; ".join(
                    f"#{item['issue']} {item['id']} at "
                    f"{directory / recovery.RECOVERY_FOLDER}"
                    f"/{item['artifact']['reference']}"
                    for item in saved
                )
        if data.get("initialize"):
            initialize_lane(lane, data["initialize"], Path(data["root"]))
        self._record_operator(
            directory, name, checkpoints.Reason.OPERATOR_RESTARTED, "starting"
        )
        return self.launch(
            name,
            repo,
            opening,
            participant["provider"],
            participant["credential"],
        )

    def reclaim(self, repo: Path, *, apply: bool = False) -> list[dict]:
        """Reports, and optionally removes, the lanes whose work has landed.

        A lane whose pull request merged holds nothing the operator still
        needs, and a project that never reclaims one accumulates a worktree
        and a branch for every claim it ever ran. The removal itself is the
        ordinary retirement, so a reclaimed lane leaves exactly the state a
        retired lane leaves and takes the same refusals: a lane that is
        busy, dirty or ahead of the base checkout is kept and reported
        instead of being removed.

        Retirement keeps a branch that carries commits the recorded project
        base does not, which is every branch that ever did work. The sweep
        has already established that the base checkout and the branch's own
        upstream carry those commits, so it then asks Git to delete the
        branch under Git's own merged-branch rule. A branch Git refuses
        leaves the lane reclaimed and the branch in place.

        Args:
            repo: Any checkout of the target repository.
            apply: Whether the assessed lanes are removed; the default only
                reports what a sweep would do.

        Returns:
            One row per lane, naming the lane, its branch, whether it was
            removed, whether its branch was deleted, the condition that
            decided it and any paths that condition names.
        """
        root, directory = self.project(repo, create=False)
        manifest = roster.read(directory)
        inactive = supervision.configuration(self.home, manifest)[
            "inactive_after"
        ]
        idle = set()
        for name in manifest["participants"]:
            observed = supervision.presence(directory, name, inactive)
            if observed["process_alive"] is True and observed["stale"]:
                idle.add(name)
        rows = reclaim.plan(directory, manifest, frozenset(idle))
        if not apply:
            return rows
        for row in rows:
            if not row["reclaim"]:
                continue
            try:
                self.retire(repo, row["participant"])
            except BridgeError as exc:
                row.update(reclaim=False, removed=False, reason=str(exc))
                continue
            with contextlib.suppress(BridgeError):
                git(root, "branch", "-d", row["branch"])
            row.update(
                removed=True, branch_removed=not has_branch(root, row["branch"])
            )
        return rows

    def reclaim_worktrees(
        self,
        repo: Path,
        *,
        apply: bool = False,
        sizes: bool = False,
        force: bool = False,
    ) -> list[dict]:
        """Reports, and optionally removes, worktrees lanes made themselves.

        Lanes add worktrees for pull requests and sub-tasks that no lane
        root accounts for. Each one the project repository registers is
        assessed by `reclaim.strays`, and a worktree no lane made is never
        removed. Git's own removal refuses a dirty or locked worktree, so
        nothing with uncommitted work is lost unless the operator forces
        it, and a forced removal writes a recovery checkpoint first.

        Args:
            repo: Any checkout of the target repository.
            apply: Whether the reclaimable worktrees are removed.
            sizes: Whether each worktree's size on disk is measured.
            force: Whether a worktree kept only for uncommitted changes,
                unpushed commits or a recent change is removed as well,
                after its checkpoint. Applies only with `apply`.

        Returns:
            One row per worktree, as `reclaim.strays` shapes it, carrying
            whether it was removed when applied and any checkpoint written.
        """
        root, directory = self.project(repo, create=False)
        rows = reclaim.strays(directory, roster.read(directory), sizes=sizes)
        if not apply:
            return rows
        return [
            reclaim.remove(str(root), directory, row, force=force)
            for row in rows
        ]
