"""Sends operator mail and decisions, and reads lane mail and pending items."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_parley import BridgeError
from agent_parley.lazy import DeferredCallable, deferred, deferred_module
from agent_parley.worktrees import Worktrees, git

if TYPE_CHECKING:
    import hashlib

    from agent_parley import attachments, issues, roster, store
    from agent_parley.issues import parse_issue
else:
    hashlib = deferred_module("hashlib")
    attachments = deferred("attachments")
    issues = deferred("issues")
    roster = deferred("roster")
    store = deferred("store")
    parse_issue = DeferredCallable(issues, "parse_issue")


def repeat_plan(
    after: float | None,
    at: float | None,
    every: float | None,
    until: float | None,
) -> tuple[float | None, int]:
    """Resolves the first delivery instant and how many deliveries follow.

    Args:
        after: Delay in seconds before the first delivery, or None.
        at: Absolute instant of the first delivery, or None.
        every: Repeat interval in seconds, or None for a single delivery.
        until: Absolute instant after which the repeat stops, or None.

    Returns:
        The first delivery instant, or None when nothing waits on a clock, and
        the number of deliveries the repeat is bounded to.

    Raises:
        BridgeError: If the repeat is unbounded or ends before it starts.
    """
    first = (
        at
        if at is not None
        else (time.time() + after if after is not None else None)
    )
    if every is None:
        if until is not None:
            raise BridgeError("--until bounds a repeat; add --every.")
        return first, 1
    if until is None:
        raise BridgeError(
            "A repeat must be bounded; add --until, such as --until 18:00."
        )
    first = first if first is not None else time.time() + every
    if until <= first:
        raise BridgeError("--until must fall after the first delivery.")
    return first, min(int((until - first) // every) + 1, store.MAX_REPEATS)


def operator_key(name: str, subject: str, body: str) -> str:
    """Derives a stable idempotency key from an operator message itself.

    Args:
        name: Participant the message addresses.
        subject: Subject line of the message.
        body: Message body.

    Returns:
        A key that repeats only for an identical message, so retyping the same
        steer redelivers nothing while a changed one is a new message.
    """
    digest = hashlib.sha256("\x00".join((name, subject, body)).encode())
    return f"operator-{digest.hexdigest()[:48]}"


class Mail(Worktrees):
    """Writes operator mail and decisions and reads lane mail."""

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
        """Writes one operator message into a participant's lane inbox.

        The operator supervises several lanes and steers one without typing
        into its terminal. It writes from this command line only: no
        coordination tool sends as the operator, and the operator identity
        holds no credential, so no served session can write in its name.

        Args:
            repo: Any checkout of the target repository.
            name: Participant whose inbox receives the message.
            text: Message body the participant reads.
            subject: Subject line; a plain default is used when empty.
            key: Idempotency key; derived from the message when empty.
            ack: Whether the participant must acknowledge the message.
            within: Seconds the acknowledgement is expected to take, recorded
                as a deadline. None takes the project default, and a message
                that requires no acknowledgement records none.
            after: Seconds to wait before the message becomes deliverable.
            at: Absolute instant the message becomes deliverable.
            when_released: Issue whose explicit release or completion the
                message waits on.
            unless_reported: Whether a delayed message is dropped once the
                lane files a report of its own.
            every: Repeat interval in seconds, which requires ``until``.
            until: Absolute instant after which the bounded repeat stops.

        Returns:
            The delivered message identifier, carrying ``duplicate`` when this
            key already named exactly this message. A message carrying a time
            or a condition is recorded instead, and the mapping names the
            pending item the supervision poll will deliver.

        Raises:
            BridgeError: If the repository has no project, the participant is
                not in its roster or not registered, the delivery condition is
                unbounded or contradictory, or the message fails validation.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        participant = data["participants"].get(name)
        if participant is None:
            raise BridgeError(
                f"{name} is not a participant in this project; "
                "run agent-parley participant list."
            )
        subject = subject or "Operator message"
        identity = participant["display"]
        expected = (
            within if within is not None else data["deadlines"].get("ack")
        )
        dedup = key or operator_key(identity, subject, text)
        condition = (
            f"released:{parse_issue(when_released)}" if when_released else ""
        )
        not_before, repeats = repeat_plan(after, at, every, until)
        if unless_reported and not_before is None:
            raise BridgeError(
                "--unless-reported drops a delayed message; add --after or "
                "--at."
            )
        if not_before is None and not condition:
            return store.speak(
                self.home,
                data["root"],
                identity,
                subject,
                text,
                dedup,
                ack=ack,
                within=expected if ack else None,
            )
        return store.schedule(
            self.home,
            data["root"],
            {
                "kind": "message",
                "recipient": name,
                "subject": subject,
                "body_md": text,
                "dedup_key": dedup,
                "ack_required": ack,
                "ack_within": expected if ack else None,
                "not_before": not_before,
                "condition": condition,
                "unless_reported": unless_reported,
                "every_seconds": every,
                "repeats_left": repeats,
            },
        )

    def acknowledge(self, repo: Path, identifier: int) -> dict:
        """Records the operator's acknowledgement of one awaited message.

        The lane that holds the message answers it in the ordinary course.
        Where that lane cannot, the condition stays on the problem list with
        no control to clear it, so the operator records the acknowledgement
        from any checkout instead. Nothing else moves: no ownership changes,
        no reservation is released and no lane is woken.

        Args:
            repo: Any checkout of the target repository.
            identifier: Message awaiting an acknowledgement.

        Returns:
            The message identifier and the registered identities the
            acknowledgement was recorded for.

        Raises:
            BridgeError: If the project has no store, or that message awaits
                no acknowledgement in it.
        """
        _, directory = self.project(repo, create=False)
        data = roster.read(directory)
        return store.acknowledge(self.home, data["root"], identifier)

    def mail(
        self,
        repo: Path,
        action: str,
        *,
        thread: str = "",
        query: str = "",
        after: int = 0,
        limit: int = store.MAX_SEARCH_HITS,
        identifier: int = 0,
        full: bool = False,
        participant: str = "",
    ) -> dict:
        """Reads a mail thread, searches mail, or handles pending items.

        The worktree selects the reader for a thread or a search, exactly as it
        does for reports and issue transitions, so an operator reads a
        participant's own mail rather than the whole project's. Naming a
        participant selects that reader instead, so an operator opens a message
        the problems report cites from the main checkout without changing
        directory into a lane. It stays a read: naming a participant sends
        nothing, acknowledges nothing and marks nothing read on their behalf.
        Pending operator items belong to the project rather than to one lane,
        so listing and cancelling them need no lane.

        Listing pending items delivers nothing: an item leaves the list only
        when the supervision poll delivers it or the operator cancels it.

        Args:
            repo: Assigned agent worktree, any checkout of the repository for
                pending items, and any checkout when a participant is named.
            action: Thread, search, list, show, pending or cancel.
            thread: Thread identifier for a thread read.
            query: Text to search subjects and bodies for.
            after: Last thread message already read.
            limit: Maximum search hits reported.
            identifier: Pending item to cancel, or message to show.
            full: Whether a shown message's attachment is read whole.
            participant: Lane whose mail is read, for an operator reading from
                the main checkout. Without one the worktree selects the reader.

        Returns:
            One thread page, the matching messages, the most recent messages,
            one message, the pending items, or the outcome of a cancellation.

        Raises:
            BridgeError: If the lane, the named participant or its registered
                identity is unknown.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        if action == "pending":
            return {"pending": store.schedules(self.home, data["root"])}
        if action == "cancel":
            return store.cancel_schedule(self.home, data["root"], identifier)
        if participant:
            if participant not in data["participants"]:
                raise BridgeError(
                    f"{participant} is not a participant in this project; "
                    "run agent-parley participant list."
                )
            agent = participant
        else:
            lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
            agent = roster.resolve(data, lane)
        name = data["participants"][agent]["display"]
        if action == "thread":
            return store.read_thread(
                self.home, data["root"], name, thread, after
            )
        if action == "list":
            return store.list_messages(self.home, data["root"], name, limit)
        if action == "show":
            message = store.read_message(
                self.home, data["root"], name, identifier
            )
            found = attachments.find(message["body_md"])
            if found:
                message["attachment"], message["attachment_bytes"] = found
            if full and found:
                message["attachment_body"] = attachments.body(
                    directory, found[0], name
                )
            return message
        return store.search_messages(
            self.home, data["root"], name, query, limit
        )

    def decide(
        self, repo: Path, text: str, subject: str = "", key: str = ""
    ) -> dict:
        """Records one decision every registered lane of the project can read.

        The decision is recorded against the project rather than sent to an
        inbox, so a lane that joins later, or that was never party to the
        discussion, still finds it by searching the log. Ordinary mail keeps
        the scope it always had.

        Args:
            repo: Any checkout of the repository the project covers.
            text: Decision text every participant can read.
            subject: Subject line the decision is found under.
            key: Idempotency key. Without one the key follows the text, so
                recording the same decision twice records it once.

        Returns:
            The recorded decision identifier, carrying ``duplicate`` when this
            key already named exactly this decision.

        Raises:
            BridgeError: If the repository has no project or the decision
                fails validation.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        heading = subject or "Operator decision"
        return store.decide(
            self.home,
            data["root"],
            heading,
            text,
            key or operator_key("decision", heading, text),
        )

    def decisions(
        self,
        repo: Path,
        query: str = "",
        limit: int = store.MAX_SEARCH_HITS,
        window: float = 0.0,
    ) -> dict:
        """Lists or searches the decisions recorded for this project.

        The worktree selects the reading participant exactly as a mail search
        does, but the log it reads belongs to the project, so the reader sees
        decisions it neither sent nor received.

        Args:
            repo: Assigned agent worktree.
            query: Text to match, or empty to list the newest decisions.
            limit: Maximum decisions reported.
            window: Seconds back the page may reach, or zero for the whole
                log.

        Returns:
            Matching decisions newest first, naming the index that answered.

        Raises:
            BridgeError: If the lane or its registered identity is unknown.
        """
        _, directory = self.project(repo)
        data = roster.read(directory)
        lane = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
        agent = roster.resolve(data, lane)
        return store.search_decisions(
            self.home,
            data["root"],
            data["participants"][agent]["display"],
            query,
            limit,
            int(window),
        )
