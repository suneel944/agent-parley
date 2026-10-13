"""Native coding agents, separate workspaces, shared coordination."""

from __future__ import annotations

import importlib.util
import sys

__version__ = "0.18.2"


class BridgeError(Exception):
    """An actionable operational failure.

    A refusal names what was refused and why in its message, and the exact
    command that resolves it in ``next_command``. The command stays out of
    the message so the text and the ``--json`` error document keep their
    shape; `refusal` joins the two for a reader.

    Attributes:
        next_command: The one command to run next, or empty when no single
            command resolves the failure.
    """

    def __init__(self, *args: object, next_command: str = "") -> None:
        """Records the failure message and the command that resolves it.

        Args:
            *args: Exception arguments; the first is the message.
            next_command: The one command to run next, or empty.
        """
        super().__init__(*args)
        self.next_command = next_command


def refusal(exc: BaseException) -> str:
    """Words a failure in the shared refusal shape a user reads.

    The first line is the message: what was refused and why. A failure that
    names a resolving command adds a ``next:`` line carrying it, so every
    refusal ends on the command that fixes it.

    Args:
        exc: The failure to word.

    Returns:
        The message, followed by a ``next:`` line when the failure has one.
    """
    command = getattr(exc, "next_command", "")
    return f"{exc}\nnext: {command}" if command else str(exc)


def _defer_cli() -> None:
    """Registers the command surface without executing its module body.

    Python documents that ``sys.argv[0]`` is ``-m`` while locating a module
    requested with ``-m``. Leaving the command module absent during that phase
    lets runpy execute it without finding a pre-registered module.

    See https://docs.python.org/3/using/cmdline.html#cmdoption-m.
    """
    if sys.argv[:1] == ["-m"]:
        return
    qualified = f"{__name__}.cli"
    if qualified in sys.modules:
        return
    spec = importlib.util.find_spec(qualified)
    if spec is None or spec.loader is None:
        raise BridgeError(f"Missing module {qualified}")
    spec.loader = importlib.util.LazyLoader(spec.loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    globals()["cli"] = module


_defer_cli()
