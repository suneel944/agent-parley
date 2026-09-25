"""Per-project settings: verification, approval, branch, tracker and budgets.

`SettingsMixin` reports and records the per-project settings commands: the
verification and initialization commands, the approval policy, the branch
naming prefix, the issue tracker, the declared resources and the lane
budgets.

Method bodies resolve the module-level names they use through `cli` when they
run, so a name bound or replaced there, including a test's patch, is the one a
moved method reads. This module never imports `cli` at import time, because
`cli` imports it to define `Bridge`.
"""

from __future__ import annotations

from pathlib import Path

from agent_parley import BridgeError
from agent_parley.core import BridgeCore


class SettingsMixin(BridgeCore):
    """Per-project verification, approval, branch, tracker and budget rules."""

    def verification(self, repo: Path, command: str | None = None) -> str:
        """Reports or records the command every merge must pass first.

        The gate belongs to the repository, not to a participant, so it lives
        beside the roster in that repository's project manifest rather than in
        a new configuration file or inside the target source tree.

        Args:
            repo: Any checkout of the target repository.
            command: Command line to require before every merge, an empty
                string to remove the gate, or None to report the current
                setting without changing it.

        Returns:
            An account of the configured gate.

        Raises:
            BridgeError: If the repository has no project yet, or the command
                is not a usable argument list.
        """
        from agent_parley.cli import lock, roster, shlex, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if command is None:
            configured = data["verify"]
        else:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                data["verify"] = roster.verify_command(command)
                write_json(directory / "project.json", data)
                configured = data["verify"]
        if not configured:
            return (
                f"{root} has no verification command; `participant merge` "
                "runs no gate."
            )
        return (
            f"{root} runs `{shlex.join(configured)}` in the base checkout "
            "before every `participant merge`."
        )

    def approval_policy(
        self, repo: Path, steps: list[str] | None = None
    ) -> str:
        """Reports or records the steps that require an operator approval.

        The requirement belongs to the repository rather than to a
        participant, so it lives beside the roster and the verification
        command in that repository's project manifest.

        Args:
            repo: Any checkout of the target repository.
            steps: Steps to require a recorded approval before, an empty list
                to require none, or None to report the current setting
                without changing it.

        Returns:
            An account of the configured requirement.

        Raises:
            BridgeError: If the repository has no project yet, or a step is
                not one the gate can stand in front of.
        """
        from agent_parley.cli import approvals, lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if steps is None:
            required = data["approval"]
        else:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                data["approval"] = roster.approval_steps(steps)
                write_json(directory / "project.json", data)
                required = data["approval"]
        if not required:
            return (
                f"{root} requires no recorded operator approval; "
                "`participant merge` and `participant pr` run unchanged."
            )
        commands = ", ".join(
            f"`{approvals.COMMANDS[step]}`" for step in required
        )
        return (
            f"{root} refuses {commands} until `agent-parley approve NAME` "
            "records a decision on the lane's current ready report."
        )

    def initialization(self, repo: Path, command: str | None = None) -> str:
        """Reports or records the command every new lane runs before starting.

        The command lives in coordination state rather than in the repository,
        so configuring it commits nothing to the target project.

        Args:
            repo: Any checkout of the target repository.
            command: Command line to run in every new lane, an empty string to
                remove it, or None to report the current setting without
                changing it.

        Returns:
            An account of the configured command.

        Raises:
            BridgeError: If the repository has no project yet, or the command
                is not a usable argument list.
        """
        from agent_parley.cli import lock, roster, shlex, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if command is None:
            configured = data["initialize"]
        else:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                data["initialize"] = roster.verify_command(
                    command, "Lane initialization command"
                )
                write_json(directory / "project.json", data)
                configured = data["initialize"]
        if not configured:
            return (
                f"{root} has no lane initialization command; a new lane "
                "starts from a bare worktree."
            )
        return (
            f"{root} runs `{shlex.join(configured)}` in every new lane "
            "before its agent starts. AGENT_PARLEY_BASE names the base "
            "checkout while it runs."
        )

    def commands(self, repo: Path) -> dict:
        """Reports the commands a repository configured, without running them.

        Args:
            repo: Any checkout of the target repository.

        Returns:
            The base checkout, the verification command every merge must pass,
            and the command every new lane runs before its agent starts. Each
            command is the stored argument list, empty when none is
            configured.

        Raises:
            BridgeError: If the repository has no project yet.
        """
        from agent_parley.cli import roster

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        return {
            "root": str(root),
            "verify": list(data["verify"]),
            "initialize": list(data["initialize"]),
        }

    def branch_naming(self, repo: Path, prefix: str | None = None) -> str:
        """Reports or records the prefix new lane branches are created under.

        The prefix belongs to the repository rather than to a participant, so
        it lives beside the roster in that repository's project manifest.
        Changing it renames nothing: lanes that already exist keep the branch
        they were created with, and the manifest records which scheme each one
        uses.

        Args:
            repo: Any checkout of the target repository.
            prefix: Prefix for new lane branches, or None to report the
                current setting without changing it.

        Returns:
            An account of the configured prefix and the names it produces.

        Raises:
            BridgeError: If the repository has no project yet, or the prefix
                is not a usable Git ref path component.
        """
        from agent_parley.cli import lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if prefix is not None:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                data["branch_prefix"] = roster.branch_prefix(prefix)
                write_json(directory / "project.json", data)
        configured = data["branch_prefix"]
        return (
            f"{root} creates lane branches as "
            f"{configured}/{directory.name}/lane-N. A lane branch carries no "
            "participant, provider or account name. Existing lanes keep the "
            "branch they were created with."
        )

    def tracker(self, repo: Path, name: str | None = None) -> str:
        """Reports or records the forge a project coordinates over.

        The forge is per project and lives in the manifest beside the roster.
        It changes nothing about ownership: every forge exchange stays best
        effort, and the ledger decides who owns an issue whichever tracker
        mirrors it. Only the GitHub forge opens pull requests.

        Args:
            repo: Any checkout of the target repository.
            name: Forge to record, or None to report the current choice.

        Returns:
            An account of the forge in use and what it can do.

        Raises:
            BridgeError: If the repository has no project yet, or the name is
                not a known forge.
        """
        from agent_parley.cli import forge, lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if name is not None:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                data["forge"] = roster.forge_choice(name)
                write_json(directory / "project.json", data)
        chosen = forge.select(root, data)
        origin = "recorded" if data.get("forge") else "detected"
        ability = (
            "opens pull requests through gh"
            if chosen == "github"
            else "opens no pull requests"
        )
        return (
            f"{root} coordinates over the {chosen} forge ({origin}), which "
            f"{ability}. Every forge exchange is best effort; the ledger "
            "decides ownership."
        )

    def resources(self, repo: Path, declared: str | None = None) -> str:
        """Reports or records the named resources a project declares.

        The declaration lives beside the roster in coordination state, so it
        commits nothing to the target repository. It narrows what a lane may
        reserve by name; it grants nothing, revokes nothing and holds no lease
        of its own.

        Args:
            repo: Any checkout of the target repository.
            declared: Space-separated resource names, an empty string to
                accept any well-formed name again, or None to report the
                current declaration without changing it.

        Returns:
            An account of the declared resources.

        Raises:
            BridgeError: If the repository has no project yet, or a name is
                not a scheme and a name such as ``port:5432``.
        """
        from agent_parley.cli import lock, roster, shlex, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if declared is not None:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                data["resources"] = roster.resources(shlex.split(declared))
                write_json(directory / "project.json", data)
        current = data["resources"]
        if not current:
            return (
                f"{root} declares no named resources, so a lane may reserve "
                "any well-formed name, such as port:5432 or db:local."
            )
        return (
            f"{root} declares {len(current)} named resources: "
            + ", ".join(current)
            + ". A lane that reserves an undeclared name is refused with this "
            "list."
        )

    def budgets(self, repo: Path, defaults: dict | None = None) -> str:
        """Reports or records the deadline and attempt defaults of a project.

        The defaults let lanes inherit a time budget without repeating a flag.
        They change nothing about ownership: an overdue claim is still owned,
        an exhausted attempt budget releases nothing, and only an explicit
        release or an accepted handoff ever moves an issue.

        Args:
            repo: Any checkout of the target repository.
            defaults: Fields to record, or None to report the current
                defaults. A field set to None is removed.

        Returns:
            An account of the recorded defaults.

        Raises:
            BridgeError: If the repository has no project yet, or a default is
                not a usable window or budget.
        """
        from agent_parley.cli import lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if defaults is not None:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                merged = {
                    key: value
                    for key, value in {
                        **data["deadlines"],
                        **defaults,
                    }.items()
                    if value is not None
                }
                data["deadlines"] = roster.deadlines(merged)
                write_json(directory / "project.json", data)
        recorded = data["deadlines"]
        if not recorded:
            return (
                f"{root} records no deadline defaults, so a claim, an offer "
                "or an acknowledgement carries a deadline only when it passes "
                "--within."
            )
        windows = ", ".join(
            f"{field} {int(recorded[field])}s"
            for field in roster.DEADLINE_FIELDS
            if field in recorded
        )
        budget = recorded.get("attempts")
        return (
            f"{root} records defaults: {windows or 'no deadlines'}"
            + (f", attempt budget {budget}" if budget else "")
            + ". An overdue claim is still owned; only an explicit release or "
            "an accepted handoff moves it."
        )

    def budget(
        self, repo: Path, scope: str, name: str, changes: dict | None = None
    ) -> str:
        """Reports or records the advisory consumption limits of one record.

        A budget is distinct from the project's deadlines: deadlines are
        windows and attempt counts on claims, offers and acknowledgements,
        while a budget is a ceiling on the tokens, served calls and session
        hours a lane consumes. A participant's limit wins over its
        provider's, which wins over the project's, field by field. Crossing
        a limit marks the lane and sends it one notice; nothing is stopped,
        revoked or refused, and a token budget counts what the lane's own
        client recorded rather than spend.

        Args:
            repo: Any checkout of the target repository.
            scope: ``participant``, ``provider`` or ``project``.
            name: Participant or provider the limits belong to; ignored for
                the project.
            changes: Flag values per field; None reports the current
                limits. A field left None is unchanged and 0 removes it.

        Returns:
            An account of the recorded limits and, for a participant, of
            its consumption against the limits that apply.

        Raises:
            BridgeError: If the record does not exist or a limit is unusable.
        """
        import sqlite3

        from agent_parley.cli import budgets, lock, roster, store, write_json

        if scope == "provider":
            if changes is not None:
                roster.provider_budget(self.home, name, changes)
            recorded = dict(
                roster.provider(self.home, name).get("budget") or {}
            )
            return f"provider {name} " + self._budget_account(recorded)
        _, directory = self.project(repo, create=False)
        if changes is not None:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                if scope == "project":
                    data["budget"] = roster.merged_budget(
                        data["budget"], changes
                    )
                else:
                    participant = data["participants"].get(name)
                    if participant is None:
                        raise BridgeError(
                            f"{name} is not a participant in this project; "
                            "run agent-parley participant list."
                        )
                    participant["budget"] = roster.merged_budget(
                        participant.get("budget") or {}, changes
                    )
                write_json(directory / "project.json", data)
        data = roster.read(directory)
        if scope == "project":
            return f"{data['root']} " + self._budget_account(data["budget"])
        if name not in data["participants"]:
            raise BridgeError(
                f"{name} is not a participant in this project; "
                "run agent-parley participant list."
            )
        try:
            usage = store.usage(self.home, data["root"])
        except sqlite3.Error:
            usage = {}
        reading = budgets.report(self.home, directory, data, name, usage)
        own = data["participants"][name].get("budget") or {}
        standing = budgets.marker(reading) or "no budget applies"
        return (
            f"{name} "
            + self._budget_account(own)
            + f" Standing: {standing}. A budget informs and does not gate."
        )

    @staticmethod
    def _budget_account(recorded: dict) -> str:
        """Words the limits one record carries of its own."""
        from agent_parley.cli import roster

        if not recorded:
            return "records no budget of its own."
        return (
            "records a budget of "
            + ", ".join(
                f"{field} {recorded[field]:,}"
                if field != "hours"
                else f"hours {recorded[field]:g}"
                for field in roster.BUDGET_FIELDS
                if field in recorded
            )
            + "."
        )
