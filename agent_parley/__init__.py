"""Native coding agents, separate workspaces, shared coordination."""

from __future__ import annotations

import importlib.util
import sys

__version__ = "0.11.0"


class BridgeError(Exception):
    """An actionable operational failure."""


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
