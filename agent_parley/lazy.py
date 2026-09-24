"""Binds modules at import time and executes them on first use."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from agent_parley import BridgeError

if TYPE_CHECKING:
    from types import ModuleType


def deferred_module(qualified: str) -> ModuleType:
    """Binds one module without executing it yet.

    A launcher process runs one command, and no command touches more than a
    few of these modules, so importing all of them before the command is even
    parsed is the largest fixed cost the command line pays. The returned
    module is a real module object that executes on its first attribute
    access, which keeps every call site, monkeypatch and `from` import that
    already names it working unchanged.

    Args:
        qualified: Fully qualified module name to bind.

    Returns:
        The submodule, already loaded if something else loaded it first, and
        otherwise a module that loads itself when first read.
    """
    import importlib.util

    loaded = sys.modules.get(qualified)
    if loaded is not None:
        return loaded
    spec = importlib.util.find_spec(qualified)
    if spec is None or spec.loader is None:
        raise BridgeError(f"Missing module {qualified}")
    spec.loader = importlib.util.LazyLoader(spec.loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    package_name, _, attribute = qualified.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None:
        setattr(package, attribute, module)
    spec.loader.exec_module(module)
    return module


def deferred(name: str) -> ModuleType:
    """Binds one coordination module without executing it yet."""
    return deferred_module(f"agent_parley.{name}")


class DeferredCallable:
    """Calls one attribute of a deferred coordination module."""

    def __init__(self, module: ModuleType, attribute: str) -> None:
        """Records the deferred module and attribute name."""
        self.module = module
        self.attribute = attribute

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Loads and calls the recorded attribute."""
        return getattr(self.module, self.attribute)(*args, **kwargs)
