"""Compatibility contract between the launcher, the plugin and the service.

Three numbers move independently. The **package version** is what was
installed. The **wire protocol** is what a hook or a served call speaks, and it
changes only when that contract changes, so several package versions normally
share one protocol. The **store schema** is what the database on disk holds, and
`store.initialize` already refuses a schema newer than the code.

Drift between the three surfaces as a tool failure in the middle of an agent's
turn, which reads as a coordination bug rather than an install that is out of
date. Each boundary therefore states its number and compares it where the call
already crosses: the launcher against the installed plugin before it starts a
lane, the hook against the launcher that wrote its command, and the server
against a served call's declared protocol. `agent-parley doctor` reports all
three without changing anything.

A hook never reaches the network to learn a version. The launcher writes its
protocol into the hook command it configures, so the hook boundary is checked
locally, and the HTTP boundary is checked where calls actually cross it.

A fourth surface moves without any number changing: the modules a detached
service already loaded against the sources now on disk. `revision` fingerprints
those sources so the service can notice that it is answering from a module set
the checkout no longer holds, rather than failing one lazily imported call at a
time.
"""

import json
import os
from pathlib import Path

CLIENTS = ("claude", "codex")
HEADER = "Agent-Parley-Protocol"
PROTOCOL = 1
SUPPORTED = (1,)
UPDATE = "agent-parley setup PATH reinstalls the plugin for this repository."
MIGRATE = "agent-parley down, then agent-parley up, migrates the store."
UPGRADE = "A newer agent-parley wrote this store; install that version."
RELAUNCH = "agent-parley up starts a service on the code in the checkout."
UNKNOWN = -1
OK = "ok"
MISMATCH = "mismatch"
STALE = "stale"
STOPPED = "not running"


def manifests(root: Path) -> dict[str, Path]:
    """Locates the shipped plugin manifest of each supported client."""
    return {
        client: root / "plugins/agent-parley" / f".{client}-plugin/plugin.json"
        for client in CLIENTS
    }


def package_root() -> Path:
    """Returns the directory the project file is read from."""
    return Path(__file__).resolve().parent.parent


def plugin_root() -> Path:
    """Returns the directory the installed plugin tree sits under.

    A wheel carries the plugin tree inside the package directory, because a
    wheel is unpacked into an environment that holds nothing else of this
    project. A checkout keeps it at the top level, where the client
    directories read it, and installs the package from the same checkout. The
    two layouts therefore disagree on where a manifest is, and a lookup fixed
    to either one reports drift that is not there on the other.

    Returns:
        The package directory when the plugin tree was installed beside the
        modules, and the directory the package sits in otherwise, which is
        the checkout for a path install.
    """
    package = Path(__file__).resolve().parent
    if (package / "plugins/agent-parley").is_dir():
        return package
    return package.parent


def launcher_version() -> str:
    """Returns the version of the code that runs, not the one installed.

    An editable install records its version once, when it was installed, so a
    checkout that has moved on keeps reporting the older number through
    installation metadata. `make install-dev` is the documented development
    path, so every contributor meets this, and a stale launcher number sends
    an operator to the compatibility contract for drift that is not there.

    The project file beside the package is therefore authoritative wherever it
    exists, because it is the file the release path raises. A package
    installed without one, which is every wheel, keeps its recorded metadata.

    The readers are imported here rather than at module load because this
    module sits on the lifecycle hook's import path, which runs once per
    native tool call, and only the launcher and the doctor ask for a version.

    Returns:
        The running package version.
    """
    import importlib.metadata
    import tomllib

    try:
        declared = tomllib.loads(
            (package_root() / "pyproject.toml").read_text()
        )["project"]["version"]
    except (OSError, ValueError, KeyError, TypeError):
        declared = None
    if isinstance(declared, str):
        return declared
    return importlib.metadata.version("agent-parley")


def revision() -> str:
    """Fingerprints the package sources a process loads its modules from.

    A long-lived process imports lazily, so a merge that adds a module and a
    lazy import of it leaves the process able to answer some calls and unable
    to answer others. The fingerprint is what turns that into one detectable
    fact: it is read once at start and compared later, and any difference
    means the modules on disk are no longer the ones the process began with.

    The reading is one directory scan of a flat package, without opening a
    file, because it sits behind a latency-sensitive path and is only ever
    compared against itself.

    Returns:
        Text that changes when a module beside this one is added, removed,
        or written, and stays equal otherwise. An unreadable package
        directory yields its own value, which compares unequal to any
        reading taken while the directory was readable.
    """
    newest = 0
    modules = 0
    try:
        with os.scandir(Path(__file__).resolve().parent) as entries:
            for entry in entries:
                if entry.name.endswith(".py"):
                    modules += 1
                    newest = max(newest, entry.stat().st_mtime_ns)
    except OSError:
        return "unreadable"
    return f"{modules}:{newest}"


def installed(path: Path) -> int:
    """Reads the protocol one plugin manifest declares.

    Args:
        path: Plugin manifest written by the client the plugin targets.

    Returns:
        The declared protocol, or `UNKNOWN` when the manifest is missing,
        unreadable, or declares no protocol. An unreadable manifest is
        reported rather than guessed, because refusing a lane on a guess is
        worse than saying the number could not be read.
    """
    try:
        declared = json.loads(path.read_text()).get("protocol")
    except (OSError, ValueError):
        return UNKNOWN
    return declared if isinstance(declared, int) else UNKNOWN


def compatible(other: int) -> bool:
    """Reports whether this build accepts a component speaking `other`."""
    return other in SUPPORTED


def render(reported: dict) -> str:
    """Formats the doctor report as aligned lines for a terminal.

    Args:
        reported: Component report produced by the launcher.

    Returns:
        One line per component carrying its version, the protocol it speaks
        and the state this build puts it in, then one verdict line naming
        every command the reported drift needs. A state this build does not
        accept is printed in upper case, so an operator scanning the report
        sees which line to read. No path inside a credential profile and no
        credential is printed. One platform line names the kernel release,
        the WSL generation and whether ``pidfd_open`` is available.
    """
    lines = []
    for component in reported["components"]:
        number = component["protocol"]
        speaks = "no protocol" if number == UNKNOWN else f"protocol {number}"
        state = component["state"]
        lines.append(
            f"{component['component']:<16}"
            f"{component['version'] or '-':<12}"
            f"{speaks:<14}"
            f"{state if component['compatible'] else state.upper()}"
        )
    host = reported["platform"]
    pidfd = "available" if host["pidfd_open"] else "unavailable"
    lines.append(
        f"{'platform':<16}kernel {host['kernel'] or '-'}, wsl {host['wsl']}, "
        f"pidfd_open {pidfd}"
    )
    lines.append(verdict(reported["components"]))
    return "\n".join(lines)


def verdict(components: list[dict]) -> str:
    """Names the drift and every command that resolves it.

    A store behind this build and an installed plugin speaking another
    protocol need different commands, and reporting one remedy for both sends
    an operator to reinstall a plugin that is already correct.

    Each component carries the command its own state needs, because the
    launcher that classified it is the only place that knows which one
    applies. A component that names none falls back to reinstalling the
    plugin, which is the drift this report was first written for.

    Args:
        components: Component records the launcher reported.

    Returns:
        A single sentence for a consistent set, or a refusal naming each
        distinct command once, in the order the components are reported.
    """
    remedies = [
        component.get("remedy") or UPDATE
        for component in components
        if not component["compatible"]
    ]
    if not remedies:
        return "Consistent."
    return "Mismatch. " + " ".join(dict.fromkeys(remedies))


def mismatch(component: str, other: int) -> str:
    """Builds the plain sentence a refused boundary reports.

    Args:
        component: Boundary that declared the other number, named as an
            operator would name it.
        other: Protocol that component declared, or `UNKNOWN`.

    Returns:
        One sentence carrying both numbers and the single command that
        resolves the drift.
    """
    speaks = "no protocol" if other == UNKNOWN else f"protocol {other}"
    return (
        f"This {component} speaks {speaks}; agent-parley speaks protocol "
        f"{PROTOCOL}. {UPDATE}"
    )
