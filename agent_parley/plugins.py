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

The commands are the native CLIs' own plugin commands, run as the operator.
Each CLI is driven once per configuration directory a lane can load the
plugin from: the CLI's default home, the one the operator's shell exports,
and every credential profile home a participant of that CLI uses. Nothing
here reads or writes a credential, edits a CLI's configuration file, or
passes a flag that loosens a permission.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from agent_parley import protocol

MARKETPLACE = "agent-parley"
SOURCE = "suneel944/agent-parley"
PLUGIN = "agent-parley@agent-parley"
TIMEOUT = 300.0
HOME_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
CODEX_CACHE = Path("plugins/cache/agent-parley/agent-parley")
SHELL = "shell"
REPAIR_RECORD = "plugin-repair.json"
REPAIRED = "repaired"
FAILED = "failed"
RETRY_FIRST = 60.0
RETRY_CEILING = 3600.0
REPAIR_ENV = "AGENT_PARLEY_PLUGIN_REPAIR"


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
        ref_flag: Flag that pins the added marketplace to a Git ref, or
            empty when the CLI accepts none.
    """

    name: str
    marketplaces: tuple[str, ...]
    add_marketplace: tuple[str, ...]
    refresh_marketplace: tuple[str, ...]
    plugins: tuple[str, ...]
    install: tuple[str, ...]
    update: tuple[str, ...]
    status_column: bool
    ref_flag: str = ""


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
        "--ref",
    ),
)


class Failed(Exception):
    """A native CLI's plugin command did not succeed."""


def run(
    executable: str,
    arguments: Sequence[str],
    env: dict[str, str] | None = None,
) -> str:
    """Runs one native plugin command and returns its standard output.

    Args:
        executable: Resolved path of the native CLI.
        arguments: Arguments after the executable.
        env: Environment selecting the configuration directory, or None for
            the operator's own.

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
            env=env,
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


def installed_manifest(name: str, config_dir: Path) -> Path | None:
    """Finds the plugin manifest a native CLI loads from one config home.

    Claude Code records each install in ``plugins/installed_plugins.json``;
    the user-scope entry's ``installPath``, or the first entry when none is
    user-scoped, holds ``.claude-plugin/plugin.json``. Codex marks the plugin
    enabled in ``config.toml`` and keeps one directory per version under
    ``plugins/cache/agent-parley/agent-parley``; the highest version is the
    one read. Those files belong to the CLIs and nothing is written.

    Args:
        name: Supported CLI's executable name, ``claude`` or ``codex``.
        config_dir: Configuration directory the CLI runs with.

    Returns:
        The manifest path, or None when the record is missing, unreadable
        or names no manifest on disk.
    """
    try:
        if name == "claude":
            record = config_dir / "plugins" / "installed_plugins.json"
            entries = json.loads(record.read_text())["plugins"][PLUGIN]
            installs = [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("installPath")
            ]
            if not installs:
                return None
            chosen = next(
                (entry for entry in installs if entry.get("scope") == "user"),
                installs[0],
            )
            path = Path(chosen["installPath"]) / ".claude-plugin/plugin.json"
        elif name == "codex":
            with (config_dir / "config.toml").open("rb") as file:
                entry = tomllib.load(file).get("plugins", {}).get(PLUGIN)
            if not isinstance(entry, dict) or entry.get("enabled") is False:
                return None
            found = sorted(
                (config_dir / CODEX_CACHE).glob("*/.codex-plugin/plugin.json"),
                key=lambda path: [
                    int(part) if part.isdigit() else -1
                    for part in path.parent.parent.name.split(".")
                ],
            )
            if not found:
                return None
            path = found[-1]
        else:
            return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return path if path.is_file() else None


def assessed(manifest: Path) -> dict:
    """Classifies one plugin manifest against the running launcher.

    Args:
        manifest: Plugin manifest to read.

    Returns:
        The manifest's ``version`` and ``protocol`` with the doctor
        ``state``, ``remedy`` and ``compatible`` fields: a protocol this
        build does not accept is a mismatch, and a version other than the
        launcher's is stale, so each names ``agent-parley plugins install``.
    """
    declared = protocol.installed(manifest)
    try:
        version = json.loads(manifest.read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        version = ""
    version = version if isinstance(version, str) else ""
    if not protocol.compatible(declared):
        state = protocol.MISMATCH
    elif version != protocol.launcher_version():
        state = protocol.STALE
    else:
        state = protocol.OK
    return {
        "version": version,
        "protocol": declared,
        "state": state,
        "remedy": "" if state == protocol.OK else protocol.UPDATE,
        "compatible": state == protocol.OK,
    }


def config_dirs(home: Path | None, name: str) -> list[tuple[str, Path]]:
    """Lists every configuration directory one CLI can load the plugin from.

    A default-account lane runs on the CLI's default home, the operator's
    own sessions on the directory the shell exports, and a lane on a
    credential profile on that profile's home. Reading the participants is
    read-only, and a state root that does not exist adds nothing.

    Args:
        home: Private bridge state root, or None to skip the participants.
        name: Supported CLI's executable name, ``claude`` or ``codex``.

    Returns:
        Each distinct directory with its label: empty for the CLI default,
        ``shell`` for an exported directory, and the credential profile name
        otherwise.
    """
    from agent_parley import records, roster
    from agent_parley.state import BridgeError

    found = {Path(records.DEFAULT_HOMES[name]).expanduser(): ""}
    exported = os.environ.get(HOME_ENV[name])
    if exported:
        found.setdefault(Path(exported).expanduser(), SHELL)
    if home is not None:
        for path in sorted((home / "projects").glob("*/project.json")):
            try:
                participants = roster.read(path.parent)["participants"]
            except (BridgeError, OSError, ValueError, KeyError):
                continue
            for entry in participants.values():
                profile = entry.get("credential")
                if not profile or roster.retired(entry):
                    continue
                try:
                    provider = roster.provider(home, entry["provider"])
                    selected = roster.config_home(home, provider, profile)
                except (BridgeError, KeyError):
                    continue
                if provider.get("adapter") == name and selected:
                    found.setdefault(Path(selected), profile)
    return [(label, directory) for directory, label in found.items()]


def environment(name: str, label: str, directory: Path) -> dict[str, str]:
    """Builds the environment that points one CLI at one config home.

    The CLI default is reached by removing the variable rather than setting
    it to the default path, because Claude Code moves other files when the
    variable is set at all.

    Args:
        name: Supported CLI's executable name.
        label: Directory label from `config_dirs`.
        directory: Configuration directory to select.

    Returns:
        A copy of the operator's environment selecting that directory.
    """
    env = dict(os.environ)
    if not label:
        env.pop(HOME_ENV[name], None)
    elif label != SHELL:
        env[HOME_ENV[name]] = str(directory)
    return env


def status(home: Path | None = None) -> list[str]:
    """Reports, per supported CLI and config home, the plugin in place.

    Args:
        home: Private bridge state root whose participants name credential
            profile homes, or None for the CLI and shell homes only.

    Returns:
        One line per supported CLI that is not on PATH, and one per config
        home of each CLI that is, naming the installed version when the
        CLI's record shows one; nothing is written.
    """
    lines = []
    found = {client.name: path for client, path in detected()}
    launcher = protocol.launcher_version()
    for client in CLIENTS:
        executable = found.get(client.name)
        if executable is None:
            lines.append(f"{client.name}: not on PATH")
            continue
        for label, directory in config_dirs(home, client.name):
            prefix = f"{client.name} ({label})" if label else client.name
            env = environment(client.name, label, directory)
            try:
                if not names(
                    run(executable, client.marketplaces, env), MARKETPLACE
                ):
                    lines.append(f"{prefix}: marketplace not added")
                    continue
                if not holds(client, run(executable, client.plugins, env)):
                    lines.append(f"{prefix}: plugin {PLUGIN} not installed")
                    continue
            except Failed as error:
                lines.append(f"{prefix}: unknown ({error})")
                continue
            line = f"{prefix}: plugin {PLUGIN} installed"
            manifest = installed_manifest(client.name, directory)
            version = assessed(manifest)["version"] if manifest else ""
            if version:
                line += f" at {version}"
                if version != launcher:
                    line += f", launcher is {launcher}"
            lines.append(line)
    return lines


def install_one(
    client: Client,
    executable: str,
    env: dict[str, str] | None = None,
    label: str = "",
) -> list[str]:
    """Adds the marketplace and plugin to one CLI, or refreshes them.

    A marketplace added by a CLI that accepts a ref is pinned to the
    launcher's release tag, so a package install never pulls plugin text
    that has not been released.

    Args:
        client: CLI to install into.
        executable: Resolved path of that CLI.
        env: Environment selecting the config home, or None for the
            operator's own.
        label: Config home label from `config_dirs`, printed with each line.

    Returns:
        One line per step taken.

    Raises:
        Failed: If a native plugin command fails.
    """
    prefix = f"{client.name} ({label})" if label else client.name
    done = []
    if names(run(executable, client.marketplaces, env), MARKETPLACE):
        run(executable, client.refresh_marketplace, env)
        done.append(f"{prefix}: marketplace {MARKETPLACE} refreshed")
    elif client.ref_flag:
        ref = f"v{protocol.launcher_version()}"
        run(executable, (*client.add_marketplace, client.ref_flag, ref), env)
        done.append(f"{prefix}: marketplace {SOURCE} added at {ref}")
    else:
        run(executable, client.add_marketplace, env)
        done.append(f"{prefix}: marketplace {SOURCE} added")
    if not holds(client, run(executable, client.plugins, env)):
        run(executable, client.install, env)
        done.append(f"{prefix}: plugin {PLUGIN} installed")
    elif client.update:
        run(executable, client.update, env)
        done.append(f"{prefix}: plugin {PLUGIN} updated")
    else:
        done.append(f"{prefix}: plugin {PLUGIN} already installed")
    return done


def install(home: Path | None = None) -> tuple[list[str], bool]:
    """Adds the plugin to every supported CLI found on PATH.

    Each CLI is driven once per config home from `config_dirs`. A failure
    in one CLI or home does not stop the next one, so one broken CLI leaves
    the others installed.

    Args:
        home: Private bridge state root whose participants name credential
            profile homes, or None for the CLI and shell homes only.

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
        for label, directory in config_dirs(home, client.name):
            env = environment(client.name, label, directory)
            try:
                lines.extend(install_one(client, executable, env, label))
            except Failed as error:
                prefix = f"{client.name} ({label})" if label else client.name
                lines.append(f"{prefix}: failed: {error}")
                succeeded = False
    return lines, succeeded


def repairs(home: Path) -> dict[str, dict]:
    """Reads the service's record of plugin repairs, one entry per home.

    Args:
        home: Private bridge state root holding `REPAIR_RECORD`.

    Returns:
        Entries keyed by CLI name and config home, empty when the record is
        missing or unreadable.
    """
    try:
        recorded = json.loads((home / REPAIR_RECORD).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(recorded, dict):
        return {}
    return {
        key: entry for key, entry in recorded.items() if isinstance(entry, dict)
    }


def repair(home: Path, now: float = 0.0) -> list[str]:
    """Refreshes every installed plugin the running launcher has outgrown.

    `uv tool upgrade` replaces the launcher and leaves the plugin each lane
    loads in the native CLI's own cache, so six releases once ran with a
    0.13.0 plugin in every lane. The coordination service calls this from
    its supervision thread, never from a hook, so the repair runs inside a
    process that is already running after every upgrade instead of
    depending on an operator remembering `agent-parley plugins install`.

    A config home is repaired only when the CLI's record already names an
    installed manifest whose version differs from the launcher's, so a home
    the operator never installed into is left alone. Each CLI and home is
    installed at most once per launcher version. An install that fails, or
    that leaves the manifest at another version, is recorded with the CLI's
    error and retried on later calls after a delay doubling from
    `RETRY_FIRST` to `RETRY_CEILING`. The install is `install_one`, so the
    CLI's own plugin commands run in the operator's environment and nothing
    reads a credential or loosens a permission. An operator who manages the
    plugin another way sets `REPAIR_ENV` to ``off`` and nothing is run.

    Args:
        home: Private bridge state root, read for credential profile homes
            and holding `REPAIR_RECORD`.
        now: Unix time; the clock when zero.

    Returns:
        One line per install attempted, empty when nothing needed one.
    """
    from agent_parley.state import write_json

    if os.environ.get(REPAIR_ENV, "").strip().lower() == "off":
        return []
    stamp = now or time.time()
    launcher = protocol.launcher_version()
    recorded = repairs(home)
    done = []
    for client, executable in detected():
        for label, directory in config_dirs(home, client.name):
            manifest = installed_manifest(client.name, directory)
            if manifest is None:
                continue
            before = assessed(manifest)["version"]
            key = f"{client.name}:{directory}"
            prior = recorded.get(key) or {}
            same = prior.get("launcher") == launcher
            if before == launcher or (
                same
                and (
                    prior.get("state") == REPAIRED
                    or stamp < float(prior.get("next_at") or 0)
                )
            ):
                continue
            prefix = f"{client.name} ({label})" if label else client.name
            error = ""
            try:
                install_one(
                    client,
                    executable,
                    environment(client.name, label, directory),
                    label,
                )
            except Failed as failure:
                error = str(failure)
            found = installed_manifest(client.name, directory)
            after = assessed(found)["version"] if found else ""
            if not error and after != launcher:
                error = f"plugin still at {after or 'unknown'} after install"
            attempts = int(prior.get("attempts") or 0) + 1 if same else 1
            delay = min(RETRY_FIRST * 2 ** (attempts - 1), RETRY_CEILING)
            recorded[key] = {
                "client": client.name,
                "label": label,
                "directory": str(directory),
                "launcher": launcher,
                "from": before,
                "to": after,
                "state": FAILED if error else REPAIRED,
                "at": stamp,
                "attempts": attempts,
                "next_at": stamp + delay if error else 0,
                "error": error,
            }
            done.append(
                f"{prefix}: plugin {before} {FAILED}: {error}"
                if error
                else f"{prefix}: plugin {before} {REPAIRED} to {after}"
            )
    if done:
        write_json(home / REPAIR_RECORD, recorded)
    return done
