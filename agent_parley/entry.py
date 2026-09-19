"""Startup path that loads only what the typed invocation needs.

Every `agent-parley` process pays this module's import before the subcommand
is known, so it imports nothing but `sys` and reaches the command surface
only once the invocation is known to need it. The version flags answer from
the compatibility contract alone, which is the cheapest module on the
launcher's side, and every other invocation falls through to the full parser
with its arguments untouched.

The fall-through is deliberately total: anything beyond a bare version flag,
including a version flag mixed with other arguments, is handed to the command
surface so that parsing, error text and exit statuses stay the ones argparse
produces.
"""

from __future__ import annotations

import sys

from agent_parley import __version__

VERSION_FLAGS = frozenset({"-V", "--version"})


def main() -> int:
    """Runs one invocation and returns its operational exit status.

    Returns:
        The exit status the invocation produced.
    """
    arguments = sys.argv[1:]
    if arguments and VERSION_FLAGS.issuperset(arguments):
        print(__version__)
        return 0
    from agent_parley import cli

    return cli.main()


if __name__ == "__main__":
    raise SystemExit(main())
