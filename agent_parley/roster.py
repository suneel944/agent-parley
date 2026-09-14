"""Defines providers, credential profiles, and project participants."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json

ADAPTERS = ("claude", "codex", "copilot", "gemini")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,38}")
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
MANIFEST_VERSION = 2
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
        adapter: Native CLI contract, claude or codex.
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
            "Agent Parley supplies MCP configuration and lifecycle hooks as "
            "native command-line arguments, which only these invocation "
            f"contracts accept; adapter must be one of: {', '.join(ADAPTERS)}."
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
    return {
        "version": MANIFEST_VERSION,
        "root": manifest["root"],
        "base": manifest["base"],
        "verify": list(manifest.get("verify") or []),
        "initialize": list(manifest.get("initialize") or []),
        "pull_request": pull_request_policy(manifest.get("pull_request", {})),
        "supervision": dict(manifest.get("supervision", {})),
        "participants": participants,
    }


def pull_request_policy(value: dict) -> dict:
    """Validates optional project pull-request metadata and body settings.

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
