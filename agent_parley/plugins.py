"""Adds the Agent Parley plugin to each supported native CLI on PATH.

Two native CLIs take a published plugin, `claude` and `codex`, and one
marketplace serves both. Installing by hand is two commands per CLI; this
module runs them for every CLI it finds, so the install script and a manual
install share one code path.

Every step reads before it writes. The marketplace is added only when the
CLI's own listing does not already name it, and the plugin is installed only
when the CLI's listing does not already hold it, so a re-run never duplicates
an entry. A re-run refreshes what is already present instead, which is how an
upgraded launcher gets the plugin version it expects.

The commands are the native CLIs' own plugin commands, run as the operator
with the operator's environment. Nothing here reads or writes a credential,
edits a CLI's configuration file, or passes a flag that loosens a permission.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

MARKETPLACE = "agent-parley"
SOURCE = "suneel944/agent-parley"
PLUGIN = "agent-parley@agent-parley"
TIMEOUT = 300.0


class Client(NamedTuple):
    """One native CLI's plugin commands.

    Attributes:
        name: Executable looked up on PATH.
        marketplaces: Arguments that list configured marketplaces.
        add_marketplace: Arguments that add this project's marketplace.
        refresh_marketplace: Arguments that refresh the added marketplace.
        plugins: Arguments that list plugins, installed ones among them.
        install: Arguments that install the plugin.
        update: Arguments that update an installed plugin, or empty when
            refreshing the marketplace is the CLI's whole update.
        status_column: Whether the plugin listing also names plugins that
            are not installed, marking each one's state on its line.
    """

    name: str
    marketplaces: tuple[str, ...]
    add_marketplace: tuple[str, ...]
    refresh_marketplace: tuple[str, ...]
    plugins: tuple[str, ...]
    install: tuple[str, ...]
    update: tuple[str, ...]
    status_column: bool


CLIENTS = (
    Client(
        "claude",
        ("plugin", "marketplace", "list", "--json"),
        ("plugin", "marketplace", "add", SOURCE),
        ("plugin", "marketplace", "update", MARKETPLACE),
        ("plugin", "list", "--json"),
        ("plugin", "install", PLUGIN),
        ("plugin", "update", PLUGIN),
        False,
    ),
    Client(
        "codex",
        ("plugin", "marketplace", "list"),
        ("plugin", "marketplace", "add", SOURCE),
        ("plugin", "marketplace", "upgrade", MARKETPLACE),
        ("plugin", "list", "--marketplace", MARKETPLACE),
        ("plugin", "add", PLUGIN),
        (),
        True,
    ),
)


class Failed(Exception):
    """A native CLI's plugin command did not succeed."""


def run(executable: str, arguments: Sequence[str]) -> str:
    """Runs one native plugin command and returns its standard output.

    Args:
        executable: Resolved path of the native CLI.
        arguments: Arguments after the executable.

    Returns:
        The command's standard output.

    Raises:
        Failed: If the command cannot start, times out or exits non-zero.
    """
    command = [executable, *arguments]
    try:
        finished = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failed(f"{' '.join(command)}: {error}") from error
    if finished.returncode:
        detail = (finished.stderr or finished.stdout).strip()
        raise Failed(
            f"{' '.join(command)} exited {finished.returncode}: {detail}"
        )
    return finished.stdout


def names(text: str, wanted: str) -> bool:
    """Says whether a listing names one entry as a whole word.

    Args:
        text: Listing output, plain or JSON.
        wanted: Entry name to find.

    Returns:
        True when a whitespace-separated word, stripped of quotes and
        punctuation, equals the name, so a repository path that merely
        contains it does not count.
    """
    return any(word.strip("\"',:;[]{}()") == wanted for word in text.split())


def holds(client: Client, listing: str) -> bool:
    """Says whether a plugin listing shows the plugin installed.

    Args:
        client: CLI that produced the listing.
        listing: Output of the client's plugin listing.

    Returns:
        True when the plugin is installed. A listing that names available
        plugins too must also mark the plugin's line installed.
    """
    for line in listing.splitlines():
        if not names(line, PLUGIN):
            continue
        if not client.status_column:
            return True
        state = line.lower()
        if "installed" in state and "not installed" not in state:
            return True
    return False


def detected() -> list[tuple[Client, str]]:
    """Finds the supported native CLIs on PATH.

    Returns:
        Each supported client found, with its resolved executable path.
    """
    found = []
    for client in CLIENTS:
        executable = shutil.which(client.name)
        if executable:
            found.append((client, executable))
    return found


def recorded(name: str) -> bool:
    """Says whether a CLI's own plugin record lists the plugin, cheaply.

    Running a native CLI's plugin listing costs a few hundred milliseconds,
    too slow for a screen printed on every bare invocation. This reads the
    record each CLI keeps of its installed plugins instead: Claude Code's
    ``plugins/installed_plugins.json`` under ``CLAUDE_CONFIG_DIR`` (default
    ``~/.claude``) and the ``plugins`` table of Codex's ``config.toml``
    under ``CODEX_HOME`` (default ``~/.codex``). Those files belong to the
    CLIs and may change shape, so `status` stays the authoritative reading;
    this one only chooses the next command to suggest. Nothing is written.

    Args:
        name: Supported CLI's executable name, ``claude`` or ``codex``.

    Returns:
        True when the record lists the plugin and does not mark it disabled;
        False when it does not, or the record is missing or unreadable.
    """
    try:
        if name == "claude":
            base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
            path = Path(base).expanduser() / "plugins"
            entries = json.loads(
                (path / "installed_plugins.json").read_text()
            ).get("plugins", {})
            return isinstance(entries, dict) and PLUGIN in entries
        if name == "codex":
            base = os.environ.get("CODEX_HOME") or "~/.codex"
            with (Path(base).expanduser() / "config.toml").open("rb") as file:
                entry = tomllib.load(file).get("plugins", {}).get(PLUGIN)
            return isinstance(entry, dict) and entry.get("enabled") is not False
    except (OSError, ValueError, AttributeError):
        return False
    return False


def status() -> list[str]:
    """Reports, per supported CLI, whether the plugin is in place.

    Returns:
        One line per supported CLI; nothing is written.
    """
    lines = []
    found = {client.name: path for client, path in detected()}
    for client in CLIENTS:
        executable = found.get(client.name)
        if executable is None:
            lines.append(f"{client.name}: not on PATH")
            continue
        try:
            if not names(run(executable, client.marketplaces), MARKETPLACE):
                lines.append(f"{client.name}: marketplace not added")
            elif holds(client, run(executable, client.plugins)):
                lines.append(f"{client.name}: plugin {PLUGIN} installed")
            else:
                lines.append(f"{client.name}: plugin {PLUGIN} not installed")
        except Failed as error:
            lines.append(f"{client.name}: unknown ({error})")
    return lines


def install_one(client: Client, executable: str) -> list[str]:
    """Adds the marketplace and plugin to one CLI, or refreshes them.

    Args:
        client: CLI to install into.
        executable: Resolved path of that CLI.

    Returns:
        One line per step taken.

    Raises:
        Failed: If a native plugin command fails.
    """
    done = []
    if names(run(executable, client.marketplaces), MARKETPLACE):
        run(executable, client.refresh_marketplace)
        done.append(f"{client.name}: marketplace {MARKETPLACE} refreshed")
    else:
        run(executable, client.add_marketplace)
        done.append(f"{client.name}: marketplace {SOURCE} added")
    if not holds(client, run(executable, client.plugins)):
        run(executable, client.install)
        done.append(f"{client.name}: plugin {PLUGIN} installed")
    elif client.update:
        run(executable, client.update)
        done.append(f"{client.name}: plugin {PLUGIN} updated")
    else:
        done.append(f"{client.name}: plugin {PLUGIN} already installed")
    return done


def install() -> tuple[list[str], bool]:
    """Adds the plugin to every supported CLI found on PATH.

    A failure in one CLI does not stop the next one, so one broken CLI
    leaves the others installed.

    Returns:
        The report lines, and whether every detected CLI succeeded.
    """
    found = detected()
    if not found:
        supported = ", ".join(client.name for client in CLIENTS)
        return (
            [
                f"No supported CLI on PATH ({supported}). Install one, then "
                "run: agent-parley plugins install"
            ],
            True,
        )
    lines = []
    succeeded = True
    for client, executable in found:
        try:
            lines.extend(install_one(client, executable))
        except Failed as error:
            lines.append(f"{client.name}: failed: {error}")
            succeeded = False
    return lines, succeeded
