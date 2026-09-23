"""Defines providers, credential profiles, and project participants."""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
from pathlib import Path

from agent_parley.forge import FORGES
from agent_parley.state import BridgeError, lock, write_json

ADAPTERS = ("claude", "codex", "copilot", "gemini", "opencode", "amp")
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "SessionEnd",
)
UNAVAILABLE_HOOKS: dict[str, tuple[str, ...]] = {
    "claude": (),
    "codex": (),
    "copilot": (),
    "gemini": ("PermissionRequest",),
    "opencode": ("SessionEnd",),
    "amp": (
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "Stop",
        "SessionEnd",
    ),
}
REQUIRED_HOOKS = ("SessionStart", "PreToolUse", "Stop")
DELIVERY_HOOKS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop")
HOOK_DELIVERY = "hooks"
POLLED_DELIVERY = "polled"
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,38}")
BRANCH_PREFIX = re.compile(r"[a-z0-9][a-z0-9/_-]{0,38}")
DEFAULT_PREFIX = "parley"
SCHEMES = ("participant", "lane")
VARIABLE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL")
OPERATOR = "operator"
RESERVED = frozenset(
    {
        "bridge",
        "config",
        "issues",
        OPERATOR,
        "project",
        "server",
        "setup",
        "store",
    }
)
LEGACY_DISPLAY = {"claude": "GreenCastle", "codex": "BlueLake"}
MAX_PARTICIPANTS = 32
MAX_VERIFY_ARGUMENTS = 64
MAX_STANDING_REPLY = 500
MANIFEST_VERSION = 2
APPROVAL_STEPS = ("merge", "pr")
PROVIDERS = "providers.json"
CREDENTIALS = "credentials.json"

PRESETS: dict[str, dict] = {
    "claude": {
        "adapter": "claude",
        "command": "claude",
        "home_env": "CLAUDE_CONFIG_DIR",
        "env": {},
        "require_env": [],
    },
    "codex": {
        "adapter": "codex",
        "command": "codex",
        "home_env": "CODEX_HOME",
        "env": {},
        "require_env": [],
    },
    "deepseek": {
        "adapter": "claude",
        "command": "claude",
        "home_env": "CLAUDE_CONFIG_DIR",
        "env": {},
        "require_env": ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"],
    },
    "kimi": {
        "adapter": "claude",
        "command": "claude",
        "home_env": "CLAUDE_CONFIG_DIR",
        "env": {},
        "require_env": ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"],
    },
    "grok": {
        "adapter": "codex",
        "command": "codex",
        "home_env": "CODEX_HOME",
        "env": {},
        "require_env": ["OPENAI_BASE_URL", "OPENAI_API_KEY"],
    },
    "copilot": {
        "adapter": "copilot",
        "command": "copilot",
        "home_env": "COPILOT_HOME",
        "env": {},
        "require_env": [],
    },
    "gemini": {
        "adapter": "gemini",
        "command": "gemini",
        "home_env": "GEMINI_CLI_HOME",
        "env": {},
        "require_env": [],
    },
    "opencode": {
        "adapter": "opencode",
        "command": "opencode",
        "home_env": "OPENCODE_CONFIG_DIR",
        "env": {},
        "require_env": [],
    },
    "amp": {
        "adapter": "amp",
        "command": "amp",
        "home_env": "AMP_SETTINGS_FILE",
        "env": {},
        "require_env": [],
    },
}


def identifier(value: str, kind: str) -> str:
    """Validates a name used as a directory, file, and Git branch component.

    Args:
        value: Candidate participant, provider, or profile name.
        kind: Human-readable field name for the failure message.

    Returns:
        The accepted name.

    Raises:
        BridgeError: If the name is malformed, reserved for bridge state, or
            reserved for the command-line operator identity.
    """
    if (
        not isinstance(value, str)
        or not IDENTIFIER.fullmatch(value)
        or value in RESERVED
    ):
        raise BridgeError(
            f"{kind} must match [a-z0-9][a-z0-9_-]{{0,38}} and must not be "
            f"one of: {', '.join(sorted(RESERVED))}. Dots are refused so a "
            "lane cannot shadow a peer's state file."
        )
    return value


def branch_prefix(value: str) -> str:
    """Validates the prefix every new lane branch is created under.

    Args:
        value: Candidate prefix for this project's lane branches.

    Returns:
        The accepted prefix.

    Raises:
        BridgeError: If the prefix is not a usable Git ref path component.
    """
    if (
        not isinstance(value, str)
        or not BRANCH_PREFIX.fullmatch(value)
        or value.endswith("/")
        or "//" in value
    ):
        raise BridgeError(
            "A lane branch prefix must match [a-z0-9][a-z0-9/_-]{0,38}, "
            "without a trailing or repeated slash."
        )
    return value


def next_lane_branch(manifest: dict, key: str, taken: set[str]) -> str:
    """Derives the next neutral branch name for a new lane.

    A lane branch carries no participant, provider or account name, because a
    participant is usually named after the provider that drives it and that
    name would otherwise reach the user's Git history and their forge. The name
    is the project's prefix, the project key, and the next free lane ordinal.

    Args:
        manifest: Project manifest holding the prefix and existing lanes.
        key: Project key the private state directory is named after.
        taken: Branch names that already exist in the repository.

    Returns:
        A branch name no lane and no existing ref holds.
    """
    prefix = manifest.get("branch_prefix") or DEFAULT_PREFIX
    used = {
        participant["branch"]
        for participant in manifest["participants"].values()
    } | taken
    ordinal = 1
    while f"{prefix}/{key}/lane-{ordinal}" in used:
        ordinal += 1
    return f"{prefix}/{key}/lane-{ordinal}"


def variables(names: list[str]) -> list[str]:
    """Validates environment variable names required from the caller's shell.

    Args:
        names: Variable names the launcher must find already exported.

    Returns:
        The accepted names.

    Raises:
        BridgeError: If a name is not a plain environment variable name.
    """
    for name in names:
        if not isinstance(name, str) or not VARIABLE.fullmatch(name):
            raise BridgeError(
                f"{name!r} is not a valid environment variable name."
            )
    return names


def overrides(pairs: list[str]) -> dict[str, str]:
    """Parses NAME=VALUE overrides while refusing to store secret values.

    Args:
        pairs: Raw NAME=VALUE arguments.

    Returns:
        Mapping of environment variable names to recorded values.

    Raises:
        BridgeError: If a pair is malformed or names a credential value.
    """
    result: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not VARIABLE.fullmatch(name):
            raise BridgeError("Environment overrides use NAME=VALUE.")
        if SECRET_NAME.search(name):
            raise BridgeError(
                f"{name} names a credential. Agent Parley does not store "
                "credential values; export it and list it with --require-env."
            )
        result[name] = value
    return result


def verify_command(
    command: str, label: str = "Verification command"
) -> list[str]:
    """Parses a repository command an operator configured into arguments.

    The command is stored and run as argument tokens, never through a shell,
    so redirection, expansion and chaining cannot ride into a gate. An empty
    command removes the gate rather than configuring an empty one, which keeps
    "no gate" a single represented state. The same parsing serves the
    pre-merge verification gate and the lane initialization command, because
    both are operator-configured argument lists run without a shell.

    Args:
        command: Command line an operator configured for this repository.
        label: Name of the configured command, reported when it is rejected.

    Returns:
        Argument tokens, or an empty list when no command is configured.

    Raises:
        BridgeError: If the command cannot be read as an argument list.
    """
    if not command.strip():
        return []
    try:
        parsed = shlex.split(command)
    except ValueError as exc:
        raise BridgeError(
            f"{label} is not a usable argument list: {exc}."
        ) from None
    if not parsed or len(parsed) > MAX_VERIFY_ARGUMENTS:
        raise BridgeError(
            f"{label} must name an executable followed by at "
            f"most {MAX_VERIFY_ARGUMENTS - 1} arguments."
        )
    if any("\x00" in token for token in parsed):
        raise BridgeError(f"{label} must not contain NUL bytes.")
    return parsed


DEADLINE_FIELDS = ("claim", "offer", "ack")
MAX_DEADLINE = 86400 * 30
MAX_ATTEMPTS = 1000


def deadlines(value: dict) -> dict:
    """Validates the deadline and attempt-budget defaults of one project.

    A default is inherited by a claim, an offer or an acknowledgement that
    passes no explicit window, so lanes carry a budget without repeating a
    flag. A deadline never transfers ownership: it only makes an overdue
    claim, offer or acknowledgement say so.

    Args:
        value: Defaults recorded in the project manifest.

    Returns:
        Validated defaults; an absent field records no default.

    Raises:
        BridgeError: If a field is unknown or holds an unusable value.
    """
    if not isinstance(value, dict) or set(value) - {
        *DEADLINE_FIELDS,
        "attempts",
    }:
        raise BridgeError(
            "Deadline defaults accept only: "
            + ", ".join([*DEADLINE_FIELDS, "attempts"])
            + "."
        )
    result = {}
    for field in DEADLINE_FIELDS:
        seconds = value.get(field)
        if seconds is None:
            continue
        if (
            type(seconds) not in (int, float)
            or not 1 <= seconds <= MAX_DEADLINE
        ):
            raise BridgeError(
                f"The {field} deadline must be between 1 and "
                f"{MAX_DEADLINE} seconds."
            )
        result[field] = seconds
    budget = value.get("attempts")
    if budget is not None:
        if type(budget) is not int or not 1 <= budget <= MAX_ATTEMPTS:
            raise BridgeError(
                f"The attempt budget must be between 1 and {MAX_ATTEMPTS}."
            )
        result["attempts"] = budget
    return result


BUDGET_FIELDS = ("tokens", "calls", "hours")
MAX_BUDGET = 10**12


def budget(value: dict) -> dict:
    """Validates the advisory consumption limits recorded on one record.

    A budget names how many tokens, served calls or session hours a lane may
    consume before it is reported as over budget. It is advisory: crossing it
    marks the lane and sends it one notice, and nothing is stopped, revoked
    or refused. A token budget counts what the lane's own client recorded,
    never billed spend.

    Args:
        value: Limits recorded on a participant, a provider or a project.

    Returns:
        Validated limits; an absent field records no limit.

    Raises:
        BridgeError: If a field is unknown or holds an unusable value.
    """
    if not isinstance(value, dict) or set(value) - set(BUDGET_FIELDS):
        raise BridgeError(
            "A budget accepts only: " + ", ".join(BUDGET_FIELDS) + "."
        )
    result = {}
    for field in BUDGET_FIELDS:
        limit = value.get(field)
        if limit is None:
            continue
        whole = field != "hours"
        if (
            type(limit) not in ((int,) if whole else (int, float))
            or not 0 < limit <= MAX_BUDGET
        ):
            raise BridgeError(
                f"The {field} budget must be a positive "
                + ("whole number" if whole else "number")
                + f" no greater than {MAX_BUDGET}."
            )
        result[field] = limit
    return result


def merged_budget(current: dict, changes: dict) -> dict:
    """Applies budget flags to recorded limits, treating zero as unset.

    Args:
        current: Limits already recorded.
        changes: Flag values; None leaves a field alone and 0 removes it.

    Returns:
        The validated result.

    Raises:
        BridgeError: If a resulting limit is unusable.
    """
    merged = {**current}
    for field, limit in changes.items():
        if limit is None:
            continue
        if limit == 0:
            merged.pop(field, None)
        else:
            merged[field] = limit
    return budget(merged)


MAX_RESOURCES = 64
RESOURCE = re.compile(r"[a-z][a-z0-9_-]{0,15}:[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


def resources(declared: list) -> list[str]:
    """Validates the named resources a project declares as existing.

    A declaration is a convenience, not a security boundary: it catches a
    mistyped resource before two lanes reserve different spellings of the same
    thing. A project that declares nothing accepts every well-formed name.

    Args:
        declared: Resource names such as ``port:5432`` or ``db:local``.

    Returns:
        The accepted names, deduplicated and ordered.

    Raises:
        BridgeError: If a name is malformed or the list is too long.
    """
    if not isinstance(declared, list) or len(declared) > MAX_RESOURCES:
        raise BridgeError(
            f"A project declares at most {MAX_RESOURCES} named resources."
        )
    for name in declared:
        if not isinstance(name, str) or not RESOURCE.fullmatch(name):
            raise BridgeError(
                f"{name!r} is not a named resource; write a scheme and a "
                "name, such as port:5432 or suite:integration."
            )
    return sorted(set(declared))


PAUSED_REASON = (
    "This lane is paused by the operator. Coordination calls and tool use "
    "stay refused until `agent-parley participant resume` runs in the base "
    "checkout. Claims, reservations and the session are all still held."
)


_LOCATIONS: dict[tuple[str, str], tuple[Path, tuple[int, int]]] = {}
_LOCATIONS_LOCK = threading.Lock()


def _manifest_stamp(path: Path) -> tuple[int, int] | None:
    """Returns a change stamp for a manifest, or None when it is gone.

    Args:
        path: Manifest file to stamp.

    Returns:
        Modification time in nanoseconds and size, or None when the manifest
        cannot be stated.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _index_projects(home: Path) -> dict[str, tuple[Path, tuple[int, int]]]:
    """Reads every registered manifest under a home and keys it by root.

    Args:
        home: Private bridge state root.

    Returns:
        Mapping of canonical project root to its state directory and stamp.
    """
    index: dict[str, tuple[Path, tuple[int, int]]] = {}
    for path in (home / "projects").glob("*/project.json"):
        stamp = _manifest_stamp(path)
        if stamp is None:
            continue
        try:
            root = json.loads(path.read_text()).get("root")
        except (OSError, ValueError):
            continue
        if isinstance(root, str):
            index[root] = (path.parent, stamp)
    return index


def locate(home: Path, root: str) -> Path | None:
    """Finds the private state directory a project root was registered under.

    A project directory is keyed by the repository's common Git directory,
    which only a checkout can resolve. A served call names its project by the
    canonical root instead, so the registered manifests are matched on that
    root rather than the key being recomputed without a checkout.

    The match is cached per home and root and confirmed with a single stat of
    the matched manifest. A manifest that was rewritten, renamed or removed
    fails that confirmation and the manifests are read again, so a project
    registered or unregistered while the service runs is still resolved.

    Args:
        home: Private bridge state root.
        root: Canonical project key recorded in the manifest.

    Returns:
        The project state directory, or None when no manifest names that root.
    """
    key = (str(home), root)
    with _LOCATIONS_LOCK:
        cached = _LOCATIONS.get(key)
    if cached is not None:
        directory, stamp = cached
        if _manifest_stamp(directory / "project.json") == stamp:
            return directory
    index = _index_projects(home)
    with _LOCATIONS_LOCK:
        for known in [entry for entry in _LOCATIONS if entry[0] == key[0]]:
            del _LOCATIONS[known]
        for found_root, entry in index.items():
            _LOCATIONS[(key[0], found_root)] = entry
    located = index.get(root)
    return located[0] if located else None


def paused(home: Path, root: str, display: str) -> bool:
    """Reports whether the operator paused the lane behind a served call.

    Args:
        home: Private bridge state root.
        root: Canonical project key recorded in the manifest.
        display: Registered identity the call authenticated as.

    Returns:
        Whether that participant is currently paused. An unreadable or absent
        manifest reports False, because a pause must be recorded to apply.
    """
    directory = locate(home, root)
    if directory is None:
        return False
    try:
        participants = read(directory)["participants"]
    except (BridgeError, OSError, ValueError):
        return False
    return any(
        entry.get("paused", False)
        for entry in participants.values()
        if entry.get("display") == display
    )


def retired(participant: dict) -> bool:
    """Reports whether a participant has retired from its project.

    A retired lane stays in the roster carrying the time it retired, so the
    reading is the presence of that time rather than the absence of an entry.
    It is not relaunched, not woken and never an offer or rebalance target
    until an operator re-admits it.

    Args:
        participant: One participant entry from a normalized manifest.

    Returns:
        Whether that participant is currently retired.
    """
    return bool(participant.get("retired"))


def _registry(home: Path, filename: str, presets: dict) -> dict:
    """Merges built-in presets with locally defined entries."""
    path = home / filename
    stored = json.loads(path.read_text()) if path.exists() else {}
    return {**presets, **stored.get("entries", {})}


def _define(home: Path, filename: str, name: str, entry: dict) -> dict:
    """Publishes one registry entry under the shared registry lock."""
    with lock(home / "registry.lock"):
        path = home / filename
        stored: dict = (
            json.loads(path.read_text())
            if path.exists()
            else {"version": 1, "entries": {}}
        )
        stored.setdefault("entries", {})[name] = entry
        write_json(path, stored)
    return entry


def providers(home: Path) -> dict:
    """Returns every provider definition available to the launcher."""
    return _registry(home, PROVIDERS, PRESETS)


def unavailable_hooks(adapter: str) -> list[str]:
    """Names the lifecycle events an adapter's native CLI cannot deliver.

    Args:
        adapter: One of ``ADAPTERS``.

    Returns:
        Shared checkpoint event names in ``HOOK_EVENTS`` order that no hook
        of that adapter can raise, so inspection shows the gap rather than
        implying every guard is enforced.
    """
    missing = UNAVAILABLE_HOOKS.get(adapter, HOOK_EVENTS)
    return [event for event in HOOK_EVENTS if event in missing]


def delivery_path(adapter: str) -> str:
    """Names how coordination reaches a lane driven by one adapter.

    Coordination is delivered at a native turn boundary whenever the CLI
    raises the events that carry it. An adapter missing any of those events
    cannot be told mid-session by a hook, so the launcher polls the mailbox
    for it instead. The distinction belongs to the adapter's event surface,
    never to a vendor.

    Args:
        adapter: One of ``ADAPTERS``.

    Returns:
        ``HOOK_DELIVERY`` when every event in ``DELIVERY_HOOKS`` is
        available, ``POLLED_DELIVERY`` otherwise.
    """
    missing = set(unavailable_hooks(adapter))
    return POLLED_DELIVERY if missing & set(DELIVERY_HOOKS) else HOOK_DELIVERY


def inspect(home: Path) -> dict:
    """Returns every provider definition with its hook availability.

    Args:
        home: Private bridge state root.

    Returns:
        Every definition ``providers`` returns, each carrying
        ``unavailable_hooks`` and the ``delivery`` path in use for the
        adapter it names.
    """
    return {
        name: {
            **entry,
            "unavailable_hooks": unavailable_hooks(entry["adapter"]),
            "delivery": delivery_path(entry["adapter"]),
        }
        for name, entry in providers(home).items()
    }


def remove(home: Path, kind: str, name: str) -> None:
    """Removes a local definition while preserving native account files.

    Removing a provider override reveals its built-in preset again. Existing
    participants retain their profile names and need a replacement definition
    before their next launch. Native credentials are never deleted.

    Args:
        home: Private bridge state root.
        kind: Either provider or credentials.
        name: Local definition to remove.

    Raises:
        BridgeError: If the kind or local definition does not exist.
    """
    filenames = {"provider": PROVIDERS, "credentials": CREDENTIALS}
    if kind not in filenames:
        raise BridgeError(f"Unknown registry: {kind}.")
    identifier(name, "Definition name")
    with lock(home / "registry.lock"):
        path = home / filenames[kind]
        stored = json.loads(path.read_text()) if path.exists() else {}
        entries = stored.get("entries", {})
        if name not in entries:
            raise BridgeError(f"No local {kind} definition named {name!r}.")
        del entries[name]
        write_json(path, stored)


def provider(home: Path, name: str) -> dict:
    """Returns one provider definition.

    Args:
        home: Private bridge state root.
        name: Provider name.

    Returns:
        The provider definition.

    Raises:
        BridgeError: If the provider is not defined.
    """
    entry = providers(home).get(name)
    if entry is None:
        raise BridgeError(
            f"Unknown provider {name!r}. Run `agent-parley provider list` or "
            "define it with `agent-parley provider add`."
        )
    return entry


def define_provider(
    home: Path,
    name: str,
    adapter: str,
    command: str,
    home_env: str,
    env: list[str],
    require_env: list[str],
) -> dict:
    """Defines or replaces a provider that rides a supported native CLI.

    Args:
        home: Private bridge state root.
        name: Provider name used by `agent-parley run --provider`.
        adapter: Native CLI contract, one of ``ADAPTERS``.
        command: Executable resolved on PATH at launch.
        home_env: Variable that points the CLI at a per-account config home.
        require_env: Variables the launcher requires from the caller's shell.
        env: Non-credential NAME=VALUE overrides, such as a base URL.

    Returns:
        The stored provider definition.

    Raises:
        BridgeError: If the adapter, command, or variables are invalid.
    """
    identifier(name, "Provider name")
    if adapter not in ADAPTERS:
        raise BridgeError(
            "Agent Parley supplies MCP configuration and lifecycle hooks "
            "through one native configuration contract per CLI; adapter "
            f"must be one of: {', '.join(ADAPTERS)}."
        )
    if not command.strip() or "\x00" in command or len(command) > 240:
        raise BridgeError("Provider command must be an executable name.")
    if home_env:
        variables([home_env])
    return _define(
        home,
        PROVIDERS,
        name,
        {
            "adapter": adapter,
            "command": command,
            "home_env": home_env,
            "env": overrides(env),
            "require_env": variables(require_env),
        },
    )


def provider_budget(home: Path, name: str, changes: dict) -> dict:
    """Records advisory consumption limits on one provider definition.

    A built-in preset gains a local override carrying the budget, so the
    preset's launch contract is preserved and `provider remove` drops the
    budget with the override.

    Args:
        home: Private bridge state root.
        name: Provider name.
        changes: Flag values per budget field; None leaves a field alone
            and 0 removes it.

    Returns:
        The stored provider definition.

    Raises:
        BridgeError: If the provider is not defined or a limit is unusable.
    """
    entry = dict(provider(home, name))
    entry["budget"] = merged_budget(
        budget(dict(entry.get("budget") or {})), changes
    )
    return _define(home, PROVIDERS, name, entry)


def credentials(home: Path) -> dict:
    """Returns every locally defined credential profile."""
    return _registry(home, CREDENTIALS, {})


def credential(home: Path, name: str) -> dict:
    """Returns one credential profile.

    Args:
        home: Private bridge state root.
        name: Credential profile name.

    Returns:
        The credential profile.

    Raises:
        BridgeError: If the profile is not defined.
    """
    entry = credentials(home).get(name)
    if entry is None:
        raise BridgeError(
            f"Unknown credential profile {name!r}. Define it with "
            "`agent-parley credentials add`."
        )
    return entry


def define_credential(
    home: Path, name: str, config_home: str, env: list[str], require: list[str]
) -> dict:
    """Defines a per-account profile without recording any credential value.

    The profile records a config home and variable names. The native CLI still
    performs its own sign-in inside that home, so bridge state holds no token.

    Args:
        home: Private bridge state root.
        name: Profile name used by `agent-parley run --credentials`.
        config_home: Directory the native CLI uses for this account.
        env: Non-credential NAME=VALUE overrides for this account.
        require: Variables the launcher requires from the caller's shell.

    Returns:
        The stored credential profile.

    Raises:
        BridgeError: If the name, directory, or variables are invalid.
    """
    identifier(name, "Credential profile name")
    resolved = ""
    if config_home:
        directory = Path(config_home).expanduser()
        if not directory.is_absolute():
            raise BridgeError("Credential home must be an absolute path.")
        resolved = str(directory)
    entry = {
        "home": resolved,
        "env": overrides(env),
        "require_env": variables(require),
    }
    if resolved:
        Path(resolved).mkdir(parents=True, exist_ok=True, mode=0o700)
    return _define(
        home,
        CREDENTIALS,
        name,
        entry,
    )


def config_home(home: Path, entry: dict, profile: str | None) -> str:
    """Returns the config home a launch selects for one provider account.

    A read-only reader of a native client's own files needs the same directory
    the launcher points that client at, so both resolve it here rather than
    assuming a fixed path in the operator's home directory.

    Args:
        home: Private bridge state root.
        entry: Provider definition.
        profile: Credential profile name, or None for the CLI default account.

    Returns:
        The directory the provider's config-home variable is set to, or an
        empty string when the launch leaves that variable to the caller.

    Raises:
        BridgeError: If the profile is undefined, or names a home this
            provider cannot apply.
    """
    if profile is None:
        return ""
    account = credential(home, profile)
    if not account.get("home"):
        return ""
    variable = entry.get("home_env", "")
    if not variable:
        raise BridgeError(
            f"Provider {entry['command']!r} has no home_env, so a "
            "credential home cannot select a separate account."
        )
    return str(account["home"])


def launch_environment(
    home: Path, entry: dict, profile: str | None
) -> dict[str, str]:
    """Builds provider and account environment overrides for one launch.

    Args:
        home: Private bridge state root.
        entry: Provider definition.
        profile: Credential profile name, or None for the CLI default account.

    Returns:
        Environment overrides applied on top of the caller's environment.

    Raises:
        BridgeError: If required variables are unset or the profile cannot
            select a separate account for this provider.
    """
    missing = [
        name
        for name in entry.get("require_env", [])
        if not os.environ.get(name)
    ]
    result = dict(entry.get("env", {}))
    if profile is not None:
        account = credential(home, profile)
        missing += [
            name
            for name in account.get("require_env", [])
            if not os.environ.get(name)
        ]
        result.update(account.get("env", {}))
        selected = config_home(home, entry, profile)
        if selected:
            result[entry["home_env"]] = selected
    if missing:
        raise BridgeError(
            "Export these before launching: " + ", ".join(sorted(set(missing)))
        )
    return result


def normalize(manifest: dict) -> dict:
    """Reads a project manifest, upgrading the two-lane layout in memory.

    Args:
        manifest: Stored manifest content.

    Returns:
        Manifest using the participant roster layout.

    Raises:
        BridgeError: If the manifest was written by a newer bridge.
    """
    if manifest.get("version", 1) > MANIFEST_VERSION:
        raise BridgeError("Unsupported project manifest; use a newer bridge.")
    participants = manifest.get("participants")
    if participants is None:
        participants = {
            name: {
                "provider": name,
                "display": LEGACY_DISPLAY.get(name, name),
                "lane": lane,
                "branch": manifest["branches"][name],
                "credential": None,
            }
            for name, lane in manifest["lanes"].items()
        }
    for participant in participants.values():
        if type(participant.get("wake", True)) is not bool:
            raise BridgeError("Participant wake setting must be a boolean.")
        if type(participant.get("paused", False)) is not bool:
            raise BridgeError("Participant paused setting must be a boolean.")
        if "dialogs" in participant:
            participant["dialogs"] = dialog_answers(participant["dialogs"])
        if "approve_bridge_tools" in participant:
            participant["approve_bridge_tools"] = approval_opt_in(
                participant["approve_bridge_tools"]
            )
        if "answer_questions" in participant:
            participant["answer_questions"] = standing_reply(
                participant["answer_questions"]
            )
        if type(participant.get("retired", 0.0)) not in (int, float):
            raise BridgeError(
                "Participant retirement must be recorded as a time."
            )
        if participant.setdefault("scheme", "participant") not in SCHEMES:
            raise BridgeError(
                "Participant branch scheme must be one of: "
                + ", ".join(SCHEMES)
                + "."
            )
        if "budget" in participant:
            participant["budget"] = budget(dict(participant["budget"] or {}))
    project = dict(manifest.get("supervision", {}))
    if "dialogs" in project:
        project["dialogs"] = dialog_answers(project["dialogs"])
    if "approve_bridge_tools" in project:
        project["approve_bridge_tools"] = approval_opt_in(
            project["approve_bridge_tools"]
        )
    if "answer_questions" in project:
        project["answer_questions"] = standing_reply(
            project["answer_questions"]
        )
    return {
        "version": MANIFEST_VERSION,
        "root": manifest["root"],
        "base": manifest["base"],
        "branch_prefix": branch_prefix(
            manifest.get("branch_prefix") or DEFAULT_PREFIX
        ),
        "verify": list(manifest.get("verify") or []),
        "initialize": list(manifest.get("initialize") or []),
        "resources": resources(list(manifest.get("resources") or [])),
        "deadlines": deadlines(dict(manifest.get("deadlines") or {})),
        "budget": budget(dict(manifest.get("budget") or {})),
        "forge": forge_choice(manifest.get("forge")),
        "approval": approval_steps(manifest.get("approval") or []),
        "pull_request": pull_request_policy(manifest.get("pull_request", {})),
        "supervision": project,
        "participants": participants,
    }


def dialog_answers(value: object) -> dict[str, str]:
    """Validates the native-dialog answers an operator recorded.

    An answer names the option text to choose on one recognized dialog. The
    dialog names themselves are checked where the screens are recognized, so a
    name this bridge does not know leaves that screen escalating rather than
    failing the whole manifest.

    Args:
        value: Recorded mapping of dialog name to option text.

    Returns:
        The answers, with surrounding whitespace removed.

    Raises:
        BridgeError: If the value is not a mapping of names to option text.
    """
    if not isinstance(value, dict) or any(
        not isinstance(name, str)
        or not isinstance(answer, str)
        or not answer.strip()
        for name, answer in value.items()
    ):
        raise BridgeError(
            "Dialog answers must map a dialog name to the option text to "
            "choose."
        )
    return {name: answer.strip() for name, answer in value.items()}


def approval_opt_in(value: object) -> bool:
    """Validates the native approval pre-approval an operator recorded.

    The setting decides whether a launch carries approval of this bridge's own
    MCP server into the client's native permission settings. It grants nothing
    wider, so it is a plain choice rather than a list of tools, and a value that
    is not a boolean is refused instead of read as consent.

    Args:
        value: Recorded opt-in for a project or one of its lanes.

    Returns:
        The recorded choice.

    Raises:
        BridgeError: If the value is not a boolean.
    """
    if type(value) is not bool:
        raise BridgeError("The approve_bridge_tools setting must be a boolean.")
    return value


def standing_reply(value: object) -> str:
    """Validates the reply an operator recorded for a lane's questions.

    The reply is typed into the client's question picker as free text, so it
    is refused when it carries a control character that could submit early,
    cancel the picker or drive the terminal, and when it is too long to read
    as one answer.

    Args:
        value: Recorded reply for a project or one of its lanes.

    Returns:
        The reply with surrounding whitespace removed.

    Raises:
        BridgeError: If the value is not bounded printable text.
    """
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_STANDING_REPLY
        or not value.isprintable()
    ):
        raise BridgeError(
            "The answer_questions setting must be one line of printable text "
            f"of at most {MAX_STANDING_REPLY} characters."
        )
    return value.strip()


def forge_choice(value: object) -> str | None:
    """Validates the forge a project coordinates over.

    Args:
        value: Recorded forge name, or None when the project relies on
            detection at each use.

    Returns:
        The forge name, or None.

    Raises:
        BridgeError: If the value names no known forge.
    """
    if value is None:
        return None
    if value not in FORGES:
        raise BridgeError(
            "The project forge must be one of: " + ", ".join(FORGES) + "."
        )
    return str(value)


def approval_steps(value: object) -> list[str]:
    """Validates the steps a project requires a recorded approval before.

    Args:
        value: Step names from the private project manifest.

    Returns:
        The required steps, ordered and without repetition. An empty list
        requires no approval, which is the shipped default.

    Raises:
        BridgeError: If a step is not one the gate can stand in front of.
    """
    if not isinstance(value, list) or any(
        step not in APPROVAL_STEPS for step in value
    ):
        raise BridgeError(
            "Approval steps must be chosen from: "
            + ", ".join(APPROVAL_STEPS)
            + "."
        )
    return [step for step in APPROVAL_STEPS if step in value]


def pull_request_policy(value: dict) -> dict:
    """Validates optional project pull-request metadata and body settings.

    The ``self_service`` setting is the repository's authorization for a
    lane to open the pull request for its own work. It is off by default,
    so a project that does not set it keeps the operator step it has today.

    Args:
        value: Policy object from the private project manifest.

    Returns:
        Validated settings; omitted fields retain the shipped defaults.

    Raises:
        BridgeError: If a setting is unknown or has an invalid value.
    """
    if not isinstance(value, dict) or set(value) - {
        "change_type_labels",
        "require_label",
        "milestone",
        "body_template",
        "self_service",
    }:
        raise BridgeError("Invalid pull_request policy in project manifest.")
    labels = value.get("change_type_labels", [])
    if (
        not isinstance(labels, list)
        or len(labels) > 100
        or any(
            not isinstance(label, str) or not label.strip() or len(label) > 100
            for label in labels
        )
    ):
        raise BridgeError("change_type_labels must be a list of label names.")
    if type(value.get("require_label", False)) is not bool:
        raise BridgeError("require_label must be a boolean.")
    if type(value.get("self_service", False)) is not bool:
        raise BridgeError("self_service must be a boolean.")
    if value.get("milestone", "match") not in {"match", "required", "ignore"}:
        raise BridgeError("milestone must be match, required, or ignore.")
    template = value.get("body_template", "")
    if not isinstance(template, str) or len(template.encode()) > 20000:
        raise BridgeError("body_template must contain at most 20000 bytes.")
    return dict(value)


def expand(manifest: dict) -> dict:
    """Adds the derived lane and branch views used by callers and tests."""
    return {
        **manifest,
        "lanes": {
            name: participant["lane"]
            for name, participant in manifest["participants"].items()
        },
        "branches": {
            name: participant["branch"]
            for name, participant in manifest["participants"].items()
        },
    }


def read(directory: Path) -> dict:
    """Reads the project manifest for a common repository state directory.

    Args:
        directory: Private state directory for the common repository.

    Returns:
        Manifest using the participant roster layout.

    Raises:
        BridgeError: If no manifest exists yet.
    """
    path = directory / "project.json"
    if not path.exists():
        raise BridgeError(
            "This repository has no bridge project yet; run agent-parley run "
            "or agent-parley setup first."
        )
    return normalize(json.loads(path.read_text()))


def resolve(manifest: dict, lane: Path) -> str:
    """Returns the participant that owns a worktree.

    Args:
        manifest: Manifest using the participant roster layout.
        lane: Resolved worktree path.

    Returns:
        The owning participant name.

    Raises:
        BridgeError: If the path is not an assigned bridge worktree.
    """
    for name, participant in manifest["participants"].items():
        if Path(participant["lane"]) == lane:
            return name
    raise BridgeError(
        "Run this from an assigned agent worktree, not the main checkout."
    )


def describe(manifest: dict) -> str:
    """Formats the roster for operators and for peer discovery."""
    lines = []
    for name, participant in sorted(manifest["participants"].items()):
        account = participant.get("credential") or "default account"
        lines.append(
            f"{name}: provider {participant['provider']}; "
            f"identity {participant['display']}; {account}"
        )
    return "\n".join(lines) or "No participants yet."
