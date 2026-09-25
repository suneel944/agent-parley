"""State root, service lifecycle, projects and participants of the bridge.

`BridgeCore` is the base every command group of `cli.Bridge` builds on: the
private state root and its configuration, the mail server's start, stop and
readiness, the project manifest, and the participant lanes recorded in it.

Method bodies resolve the module-level names they use through `cli` when they
run, so a name bound or replaced there, including a test's patch, is the one a
moved method reads. This module never imports `cli` at import time, because
`cli` imports it to define `Bridge`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError

if TYPE_CHECKING:
    from agent_parley import process


class BridgeCore:
    """Private state root, mail server and project lanes of the bridge.

    Attributes:
        home: Resolved private state directory.
        config: Local HTTP port and bearer credential.
        url: Loopback HTTP origin of the mail server.
    """

    def __init__(self, home: Path) -> None:
        """Loads or initializes private configuration under home."""
        from agent_parley.cli import json, lock, secrets, write_json

        self.home = home.expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.home.stat().st_mode & 0o077:
            raise BridgeError(
                f"State directory must be private: chmod 700 {self.home}"
            )
        path = self.home / "config.json"
        if not path.exists():
            with lock(self.home / "config.lock"):
                if not path.exists():
                    port = int(os.environ.get("AGENT_PARLEY_PORT", "8876"))
                    if not 1024 <= port <= 65535:
                        raise BridgeError(
                            "AGENT_PARLEY_PORT must be between 1024 and 65535."
                        )
                    write_json(
                        path,
                        {"port": port, "token": secrets.token_urlsafe(32)},
                    )
        self.config = json.loads(path.read_text())
        self.url = f"http://127.0.0.1:{self.config['port']}"

    def server_process(self) -> process.ServerProcess | None:
        """Returns the recorded server only if its process identity matches."""
        from agent_parley.cli import json, process

        record = self.home / "server.json"
        if not record.exists():
            return None
        data = json.loads(record.read_text())
        return process.identify(data, self.home)

    def health(self) -> dict:
        """Reads the service's own account of itself, without a proxy.

        The service answers what code it started on and whether the checkout
        has moved past it, so a reading here reports drift the launcher
        cannot see from its own process.

        Returns:
            The readiness document, or an empty mapping when nothing answers
            on the configured port.
        """
        from agent_parley.cli import MAX_HEALTH_BYTES, json, socket

        port = int(self.config["port"])
        authority = f"127.0.0.1:{port}"
        request = (
            "GET /health/readiness HTTP/1.0\r\n"
            f"Host: {authority}\r\n"
            f"Authorization: Bearer {self.config['token']}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        response = bytearray()
        try:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=1
            ) as connection:
                connection.sendall(request)
                while len(response) <= MAX_HEALTH_BYTES:
                    chunk = connection.recv(
                        min(8192, MAX_HEALTH_BYTES + 1 - len(response))
                    )
                    if not chunk:
                        break
                    response.extend(chunk)
        except OSError:
            return {}
        headers, separator, body = bytes(response).partition(b"\r\n\r\n")
        status = headers.split(b"\r\n", 1)[0].split()[1:2]
        if (
            len(response) > MAX_HEALTH_BYTES
            or not separator
            or status != [b"200"]
        ):
            return {}
        try:
            document = json.loads(body)
        except ValueError:
            return {}
        return document if isinstance(document, dict) else {}

    def ready(self) -> bool:
        """Checks authenticated readiness without routing through proxies."""
        return self.health().get("status") == "ready"

    def up(self) -> None:
        """Starts the mail server with bounded readiness checking.

        The published record names a service that answered. A record found
        naming a process that is gone, as a reboot or a drift exit leaves
        behind, is removed before the new service is started. A start that
        fails, on an occupied port or a process never ready within
        `START_SECONDS`, publishes a record with ``state: failed``, the time
        and the count of consecutive failures instead, and names no process.
        Every reader of the record therefore learns of a service only once it
        serves, and the hooks' relaunch still finds a record to retry from,
        with a backoff the failure count sets.

        A service already serving is left running and the store is migrated
        in place to this build's schema, so repairing a store an upgrade left
        behind interrupts no lane.

        Raises:
            BridgeError: If the port is occupied or startup fails.
        """
        from agent_parley.cli import (
            START_SECONDS,
            json,
            lock,
            process,
            socket,
            store,
            subprocess,
            write_json,
        )

        with lock(self.home / "server.lock"):
            published = self.home / "server.json"
            failures = 0
            if published.exists():
                record = json.loads(published.read_text())
                if record.get("state") == "failed":
                    failures = int(record.get("failures", 0))
                legacy = "pid" in record and "start_ticks" not in record
                if legacy and process.running(record["pid"]):
                    raise BridgeError(
                        "A service from an older installation is running. "
                        "Stop it with the command that started it before "
                        "upgrading; existing sessions are preserved."
                    )
            running = self.server_process()
            if running:
                if not self.ready():
                    raise BridgeError(
                        "Server is running but unhealthy. "
                        f"Inspect {self.home}/server.log"
                    )
                store.initialize(self.home)
                return
            failed = {
                "state": "failed",
                "failed_at": time.time(),
                "failures": failures + 1,
            }
            published.unlink(missing_ok=True)
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", self.config["port"]))
                except OSError:
                    write_json(published, failed)
                    raise BridgeError(
                        f"Port {self.config['port']} "
                        "is occupied by another service."
                    ) from None
            with (self.home / "server.log").open("ab") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "agent_parley.server",
                        "--home",
                        str(self.home),
                    ],
                    cwd=self.home,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            identity = {
                "pid": child.pid,
                "start_ticks": process.start_ticks(child.pid),
            }
            deadline = time.monotonic() + START_SECONDS
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                if self.ready():
                    write_json(published, identity)
                    return
                time.sleep(0.2)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            write_json(published, failed)
            raise BridgeError(
                "Coordination server failed to start. "
                f"Inspect {self.home}/server.log"
            )

    def down(self) -> None:
        """Stops the identified server while retaining all persistent state.

        A start holds the same lock until the new service answers, which is
        bounded at thirty seconds including the wind-down of a service that
        never became ready. Refusing the moment that lock is held reported
        contention for a stop that was only queued behind a start, so the
        stop waits for that span before it reports the lock busy.

        Raises:
            BridgeError: If locking fails or the server does not stop in time.
        """
        from agent_parley.cli import lock

        with lock(self.home / "server.lock", timeout=30):
            running = self.server_process()
            if running:
                running.stop()
            (self.home / "server.json").unlink(missing_ok=True)

    def project(self, repo: Path, *, create: bool = True) -> tuple[Path, Path]:
        """Returns repository paths, optionally creating its state directory.

        Args:
            repo: Main checkout or linked worktree.
            create: Whether to create the private project directory.

        Returns:
            Main worktree and shared state directory paths.
        """
        from agent_parley.cli import git, hashlib

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
        from agent_parley.cli import lock, roster

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
        from agent_parley.cli import (
            drift,
            forge,
            git,
            json,
            lane_branch,
            preserve_pending,
            roster,
            write_json,
        )

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
        from agent_parley.cli import (
            git,
            initialize_lane,
            lock,
            roster,
            write_json,
        )

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
        from agent_parley.cli import (
            git,
            has_branch,
            initialize_lane,
            write_json,
        )

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
        from agent_parley.cli import roster

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
        from agent_parley.cli import approvals, branch_head

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
        from agent_parley.cli import approvals

        if step not in data["approval"]:
            return
        refusal = approvals.refusal(
            name, step, self._reviewed(directory, data, name)
        )
        if refusal:
            raise BridgeError(refusal)
