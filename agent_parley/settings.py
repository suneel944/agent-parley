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
            BridgeError: If the repository has no project yet, the command
                is not a usable argument list, or a change comes from a lane
                or a process holding a lane's token.
        """
        from agent_parley import unattended
        from agent_parley.cli import lock, roster, shlex, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if command is None:
            configured = data["verify"]
        else:
            unattended.operator_only(
                repo, root, data, "The verification command is set"
            )
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
            BridgeError: If the repository has no project yet, a step is not
                one the gate can stand in front of, or a change comes from a
                lane or a process holding a lane's token.
        """
        from agent_parley import unattended
        from agent_parley.cli import approvals, lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if steps is None:
            required = data["approval"]
        else:
            unattended.operator_only(
                repo, root, data, "The approval requirement is set"
            )
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

    def resume_opt_in(
        self,
        repo: Path,
        participant: str = "",
        bridge_tools: bool | None = None,
        auto_mode: bool | None = None,
    ) -> str:
        """Reports or records the opt-ins a service resume of a lane needs.

        A `claude` lane the service resumes has nobody at the keyboard, so
        the service withholds the resume until the operator records
        `approve_bridge_tools` or `auto_mode`. Both stay explicit operator
        choices that default to off; this writes the same manifest fields
        `roster.approval_opt_in` validates, for the project under
        `supervision` or for one lane, so no one edits private state by hand.

        Args:
            repo: Any checkout of the target repository.
            participant: Lane to record the opt-in on, or empty for the
                project default a lane entry overrides.
            bridge_tools: New `approve_bridge_tools` value, or None to keep it.
            auto_mode: New `auto_mode` value, or None to keep it.

        Returns:
            One line per affected lane with both effective opt-ins.

        Raises:
            BridgeError: If the repository has no project yet, the named
                participant is not in it, or a change comes from a lane or a
                process holding a lane's token.
        """
        from agent_parley import dialogs, unattended
        from agent_parley.cli import lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        changes = {
            key: value
            for key, value in (
                (dialogs.PRE_APPROVE, bridge_tools),
                (dialogs.AUTO_MODE, auto_mode),
            )
            if value is not None
        }
        if participant and participant not in data["participants"]:
            raise BridgeError(
                f"{participant} is not a participant in this project."
            )
        if changes:
            unattended.operator_only(
                repo, root, data, "The service resume opt-in is set"
            )
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                target = (
                    data["participants"][participant]
                    if participant
                    else data.setdefault("supervision", {})
                )
                target.update(changes)
                write_json(directory / "project.json", data)
        names = [participant] if participant else sorted(data["participants"])
        if not names:
            return f"{root} has no participants yet."
        state = {True: "on", False: "off"}
        return "\n".join(
            f"{name}: {dialogs.PRE_APPROVE} "
            f"{state[dialogs.pre_approved(data, name)]}, "
            f"{dialogs.AUTO_MODE} {state[dialogs.auto_mode(data, name)]}"
            for name in names
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
            BridgeError: If the repository has no project yet, the command
                is not a usable argument list, or a change comes from a lane
                or a process holding a lane's token.
        """
        from agent_parley import unattended
        from agent_parley.cli import lock, roster, shlex, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if command is None:
            configured = data["initialize"]
        else:
            unattended.operator_only(
                repo, root, data, "The lane initialization command is set"
            )
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

    def pool(self, repo: Path, size: int | None = None) -> str:
        """Reports or records how many prepared spare worktrees to keep.

        Recording a size starts a detached fill, so the command returns at
        once and `status` shows the spares as they become ready. Size zero
        keeps none; the spares already prepared are removed by the next
        `gc` sweep.

        Args:
            repo: Any checkout of the target repository.
            size: Spare worktrees to keep, or None to report the current
                setting without changing it.

        Returns:
            An account of the configured pool and its spares.

        Raises:
            BridgeError: If the repository has no project yet, the size is
                out of range, or a change comes from a lane or a process
                holding a lane's token.
        """
        from agent_parley import pool, unattended
        from agent_parley.cli import lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        if size is not None:
            unattended.operator_only(
                repo, root, data, "The spare worktree pool is set"
            )
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                data["pool"] = roster.pool_size(size)
                write_json(directory / "project.json", data)
            pool.replenish(self.home, root, directory)
        configured = int(data.get("pool") or 0)
        line = pool.summary_line(pool.reading(directory, data))
        if not configured:
            return f"{root} keeps no spare worktrees." + (
                f" {line}." if line else ""
            )
        return (
            f"{root} keeps {configured} spare worktrees prepared from the "
            f"current base for new lanes. {line}."
        )

    def fill_pool(self, repo: Path) -> str:
        """Brings the spare worktree pool to its configured size now.

        Args:
            repo: Any checkout of the target repository.

        Returns:
            The pool's status line once the fill finished.
        """
        from agent_parley import pool
        from agent_parley.cli import roster

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        pool.fill(root, directory, data)
        return pool.summary_line(pool.reading(directory, data)) or (
            f"{root} keeps no spare worktrees."
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
        base = data.get("integration_base")
        return (
            f"{root} creates lane branches as "
            f"{configured}/{directory.name}/lane-N. A lane branch carries no "
            "participant, provider or account name. Existing lanes keep the "
            "branch they were created with. "
            + (
                f"Pull requests merged into {base} land the claims they close."
                if base
                else "No integration base is recorded."
            )
        )

    def integration_branch(self, repo: Path, branch: str) -> str:
        """Records the branch a milestone's pull requests merge into.

        The forge closes an issue only when its pull request merges into the
        default branch, so a milestone integrated on another branch leaves
        every claim open until it crosses. With this branch recorded, the
        supervisor counts a pull request merged into it that closes a claimed
        issue by keyword as that claim's landed work, and asks the operator
        once to open the crossing pull request when every claim has landed.
        Only the operator records it, because it ends claims.

        Args:
            repo: Any checkout of the target repository.
            branch: Integration branch, or an empty string to remove it.

        Returns:
            An account of the recorded integration base.

        Raises:
            BridgeError: If the repository has no project yet, the branch is
                not one Git branch name, or the change comes from a lane or
                a process holding a lane's token.
        """
        from agent_parley import unattended
        from agent_parley.cli import lock, roster, write_json

        root, directory = self.project(repo, create=False)
        data = roster.read(directory)
        unattended.operator_only(
            repo, root, data, "The integration base is set"
        )
        accepted = roster.integration_base(branch)
        with lock(directory / "setup.lock"):
            data = roster.read(directory)
            data.pop("integration_base", None)
            if accepted:
                data["integration_base"] = accepted
            write_json(directory / "project.json", data)
        if not accepted:
            return (
                f"{root} records no integration base; only issues the forge "
                "closes end claims."
            )
        return (
            f"{root} integrates into {accepted}: a pull request merged into "
            "it that closes a claimed issue by keyword lands that claim."
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
                f"{root} records no deadline defaults, so a claim, an offer, "
                "a takeover request or an acknowledgement carries a deadline "
                "only when it passes --within."
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

    def enforce(self, repo: Path, changes: dict | None = None) -> str:
        """Reports or records the run budget a project opted into enforcing.

        A run budget sums every lane's tokens, served calls and session
        hours and, once any enforced sum reaches its limit, refuses new
        wakes, dispatches, retries and launches until an operator resumes
        the run. It is recorded apart from the advisory budgets, which stay
        advisory and never gate. Recording a limit never clears a recorded
        exhaustion; only ``budget resume`` does.

        Args:
            repo: Any checkout of the target repository.
            changes: Flag values per field; None reports the current
                standing. A field left None is unchanged and 0 removes it;
                removing every field returns the project to advisory only.

        Returns:
            An account of the enforced limits and the run's standing.

        Raises:
            BridgeError: If the repository has no project or a limit is
                unusable.
        """
        from agent_parley.cli import budgets, lock, roster, write_json

        _, directory = self.project(repo, create=False)
        if changes is not None:
            with lock(directory / "setup.lock"):
                data = roster.read(directory)
                before = dict(data.get("run_budget") or {})
                data["run_budget"] = roster.merged_budget(before, changes)
                if data["run_budget"]:
                    budgets.start(self.home, directory, data, before)
                else:
                    data.pop("run_budget")
                write_json(directory / "project.json", data)
        return budgets.run_account(self.home, directory, roster.read(directory))

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
