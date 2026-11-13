"""The once-a-day check for a newer published agent-parley release.

Only the `doctor` and `status` commands call this module; no hook and no
service poll does, so a lane's tool calls and the supervision loop never wait
on the package index. The answer, a failed attempt included, is cached in the
state root for a day, so an unreachable index costs at most one bounded
attempt a day. Setting ``AGENT_PARLEY_NO_UPDATE_CHECK=1`` turns the check off.
"""

import http.client
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_parley import protocol, state

INDEX = "https://pypi.org/pypi/agent-parley/json"
CACHE = "update-check.json"
INTERVAL = 86400.0
TIMEOUT = 2.0
DISABLE = "AGENT_PARLEY_NO_UPDATE_CHECK"
FAILURES = (OSError, ValueError, http.client.HTTPException)


def fetch_latest(timeout: float = TIMEOUT) -> str:
    """Reads the latest published version from the package index.

    Args:
        timeout: Seconds allowed for the connection and each read.

    Returns:
        The index's ``info.version`` value.

    Raises:
        OSError: If the index is unreachable or the request times out.
        ValueError: If the answer is not JSON naming a version.
        http.client.HTTPException: If the HTTP exchange breaks off.
    """
    import urllib.request

    request = urllib.request.Request(
        INDEX, headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        document = json.loads(response.read())
    info = document.get("info") if isinstance(document, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError("the package index named no version")
    return version.strip()


def release(version: str) -> tuple[int, ...]:
    """Reads the numeric release segments of a version string.

    Each dot-separated part contributes its leading digits; the first part
    without any ends the release, so ``0.23.0rc1`` reads ``(0, 23, 0)``.
    Trailing zeros are dropped so ``1.0`` and ``1.0.0`` compare equal.

    Args:
        version: A version such as ``0.22.0``, with an optional ``v`` prefix.

    Returns:
        The release segments, empty when the string starts with no digit.
    """
    segments: list[int] = []
    for part in version.strip().removeprefix("v").split("."):
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        segments.append(int(digits))
        if len(digits) != len(part):
            break
    while segments and segments[-1] == 0:
        segments.pop()
    return tuple(segments)


def older(installed: str, latest: str) -> bool:
    """Says whether the installed version precedes the latest one.

    Args:
        installed: The running launcher's version.
        latest: The version the package index publishes.

    Returns:
        True only when both read as releases and installed is lower.
    """
    have, newest = release(installed), release(latest)
    return bool(have) and bool(newest) and have < newest


def upgrade_command(prefix: str = "", executable: str = "") -> str:
    """Names the upgrade command for the installer that placed the launcher.

    A uv tool environment carries ``uv-receipt.toml`` and sits under a
    ``uv/tools`` directory; a pipx one sits under a ``pipx`` directory.
    Anything else is upgraded with pip.

    Args:
        prefix: The environment prefix, `sys.prefix` when empty.
        executable: The interpreter path, `sys.executable` when empty.

    Returns:
        The command that upgrades agent-parley in place.
    """
    root = Path(prefix or sys.prefix)
    paths = (root.as_posix(), (executable or sys.executable).replace("\\", "/"))
    if (root / "uv-receipt.toml").is_file() or any(
        "/uv/tools/" in f"{path}/" for path in paths
    ):
        return "uv tool upgrade agent-parley"
    if any("/pipx/" in f"{path}/" for path in paths):
        return "pipx upgrade agent-parley"
    return "pip install -U agent-parley"


def latest(
    home: Path,
    *,
    now: float | None = None,
    fetch: Callable[[], str] | None = None,
) -> str:
    """Returns the latest published version, asking the index once a day.

    A cached answer younger than `INTERVAL` is returned without a request. A
    failed request is cached as an empty answer, so the next attempt waits a
    day too. A cache that cannot be read or written never fails the caller.

    Args:
        home: The private state root holding `CACHE`.
        now: The current time in epoch seconds, `time.time` when omitted.
        fetch: Reads the latest version, `fetch_latest` when omitted.

    Returns:
        The latest version, or an empty string when it is not known.
    """
    current = time.time() if now is None else now
    path = home / CACHE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if isinstance(cached, dict):
        checked = cached.get("checked")
        answer = cached.get("latest")
        if (
            isinstance(checked, int | float)
            and isinstance(answer, str)
            and 0 <= current - checked < INTERVAL
        ):
            return answer
    try:
        answer = (fetch or fetch_latest)()
    except FAILURES:
        answer = ""
    try:
        state.write_json(path, {"checked": current, "latest": answer})
    except OSError:
        pass
    return answer


def notice(
    home: Path,
    *,
    installed: str = "",
    now: float | None = None,
    fetch: Callable[[], str] | None = None,
    environ: Mapping[str, str] | None = None,
    command: str = "",
) -> str:
    """Builds the one-line upgrade notice, or nothing.

    Args:
        home: The private state root holding the cache.
        installed: The running version, `protocol.launcher_version` if empty.
        now: The current time in epoch seconds, `time.time` when omitted.
        fetch: Reads the latest version, `fetch_latest` when omitted.
        environ: The environment to read `DISABLE` from, `os.environ` if
            omitted.
        command: The upgrade command, `upgrade_command` when empty.

    Returns:
        The notice when a newer release is published, else an empty string.
    """
    settings = os.environ if environ is None else environ
    if settings.get(DISABLE, "").strip() not in ("", "0"):
        return ""
    running = installed or protocol.launcher_version()
    newest = latest(home, now=now, fetch=fetch)
    if not older(running, newest):
        return ""
    return (
        f"agent-parley {running} is installed; {newest} is available: "
        f"{command or upgrade_command()}"
    )
