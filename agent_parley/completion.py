"""Shell completion generated from the command parser itself.

The completion scripts this module prints are derived from the live
``argparse`` tree rather than from a checked-in copy of the command list, so
a command or flag that exists is completable and one that was renamed stops
being offered without a second edit. Nothing here imports a completion
library: each shell's own syntax is emitted directly.

Values that only the local state directory knows, such as participant names
and claimed issue numbers, are not baked into the script. The script calls
back into the program's hidden completion mode, which reads the registered
manifests and issue files without taking the operation lock, so a busy or
slow store delays a keystroke rather than blocking on a writer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_parley import issues, roster

SHELLS = ("bash", "zsh", "fish")

KINDS = ("participants", "providers", "credentials", "projects", "issues")

VALUE_KINDS = {
    "participant": "participants",
    "to": "participants",
    "from": "participants",
    "provider": "providers",
    "credential": "credentials",
    "credentials": "credentials",
    "profile": "credentials",
    "project": "projects",
    "repo": "projects",
    "issue": "issues",
    "number": "issues",
}

NAME_KINDS = {
    "participant": "participants",
    "history": "participants",
    "issue": "participants",
    "provider": "providers",
    "credentials": "credentials",
}


def _manifests(home: Path) -> list[tuple[Path, dict]]:
    """Reads every registered project manifest without taking a lock.

    Args:
        home: Private state directory for the coordination server.

    Returns:
        One pair of state directory and manifest per readable project. A
        manifest that is missing or malformed is skipped, because completion
        offers what it can rather than failing a keystroke.
    """
    found: list[tuple[Path, dict]] = []
    for path in sorted((home / "projects").glob("*/project.json")):
        try:
            found.append((path.parent, json.loads(path.read_text())))
        except (OSError, ValueError):
            continue
    return found


def _issue_numbers(directory: Path) -> list[str]:
    """Reads the issue numbers recorded for one project.

    Args:
        directory: Project state directory holding the issue file.

    Returns:
        Every issue number the ledger records, held or not. An unreadable
        issue file reports nothing rather than raising, so completion stays
        silent on damage.
    """
    try:
        state = issues.snapshot(directory)
    except (OSError, ValueError):
        return []
    return list(state.get("issues", {}))


def candidates(home: Path, kind: str) -> list[str]:
    """Lists the completion candidates of one kind from local state.

    The read takes no lock and touches only files the coordination server
    already publishes, so it returns in bounded time whatever a concurrent
    writer is doing.

    Args:
        home: Private state directory for the coordination server.
        kind: One of the names in `KINDS`.

    Returns:
        Sorted unique candidates, issue numbers ordered numerically rather
        than as text so 9 precedes 10. An unknown kind returns nothing.
    """
    if kind == "providers":
        return sorted(roster.providers(home))
    if kind == "credentials":
        return sorted(roster.credentials(home))
    found: set[str] = set()
    for directory, manifest in _manifests(home):
        if kind == "participants":
            found.update(manifest.get("participants", {}))
        elif kind == "projects":
            root = manifest.get("root", "")
            if root:
                found.add(root)
        elif kind == "issues":
            found.update(_issue_numbers(directory))
    if kind == "issues":
        return sorted(found, key=lambda number: (len(number), number))
    return sorted(found)


def _subcommands(parser: argparse.ArgumentParser) -> dict:
    """Finds the named subparsers one parser offers.

    Names beginning with a double underscore are the program's own hidden
    entry points, such as the callback this module's scripts invoke. They are
    omitted so a completion never offers the operator an internal command.

    Args:
        parser: Parser to inspect.

    Returns:
        Subcommand name mapped to its parser, empty when the parser is a
        leaf.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {
                name: child
                for name, child in action.choices.items()
                if not name.startswith("__")
            }
    return {}


def _flags(parser: argparse.ArgumentParser) -> list[str]:
    """Lists the option strings one parser accepts.

    Args:
        parser: Parser to inspect.

    Returns:
        Every option string except the help flags, which every shell already
        offers, in the order the parser declares them.
    """
    found: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        for option in action.option_strings:
            if option not in ("-h", "--help"):
                found.append(option)
    return found


def _value_flags(parser: argparse.ArgumentParser) -> dict:
    """Maps this parser's options to the state-backed kind they accept.

    Args:
        parser: Parser to inspect.

    Returns:
        Option string mapped to a name in `KINDS`, for options whose
        destination names a kind local state can enumerate.
    """
    found: dict = {}
    for action in parser._actions:
        kind = VALUE_KINDS.get(action.dest, "")
        if not kind or action.nargs == 0:
            continue
        for option in action.option_strings:
            found[option] = kind
    return found


def tree(
    parser: argparse.ArgumentParser,
) -> list[tuple[str, list[str], list[str]]]:
    """Walks the parser into one entry per reachable command path.

    Args:
        parser: Root parser of the program.

    Returns:
        A triple per command path, ordered parents before children: the
        space-joined path, its subcommand names, and its option strings. The
        root has the empty path.
    """
    found: list[tuple[str, list[str], list[str]]] = []
    pending = [("", parser)]
    while pending:
        path, node = pending.pop(0)
        children = _subcommands(node)
        found.append((path, sorted(children), _flags(node)))
        for name, child in sorted(children.items()):
            pending.append((f"{path} {name}".strip(), child))
    return found


def value_flags(parser: argparse.ArgumentParser) -> dict:
    """Collects the state-backed options of every command path.

    Args:
        parser: Root parser of the program.

    Returns:
        Option string mapped to a name in `KINDS`, merged across the tree so
        one shell-level rule serves every command that takes the option.
    """
    found: dict = {}
    pending = [parser]
    while pending:
        node = pending.pop(0)
        found.update(_value_flags(node))
        pending.extend(_subcommands(node).values())
    return found


def _slot(path: str, action: argparse.Action) -> str:
    """Names what one positional argument accepts.

    A positional called ``name`` takes its kind from the command family, so
    ``participant restart`` offers participants and ``provider show`` offers
    providers. An ``add`` command names something new, so it offers nothing.

    Args:
        path: Space-joined command path the argument belongs to.
        action: The positional argument.

    Returns:
        ``@`` and a name in `KINDS` for a state-backed value, the
        space-joined choices for a fixed set, or an empty string when the
        value is free text.
    """
    if action.choices:
        return " ".join(str(choice) for choice in action.choices)
    words = path.split()
    kind = VALUE_KINDS.get(action.dest, "")
    if action.dest == "name" and words and words[-1] != "add":
        kind = NAME_KINDS.get(words[0], "")
    return f"@{kind}" if kind else ""


def positionals(parser: argparse.ArgumentParser) -> dict:
    """Maps each command path's positional arguments to what they accept.

    Args:
        parser: Root parser of the program.

    Returns:
        ``path#index`` mapped to the result of `_slot`, where the index counts
        every positional of that command from zero. Free-text positionals are
        omitted, so the shell falls back to the command's flags there.
    """
    found: dict = {}
    pending = [("", parser)]
    while pending:
        path, node = pending.pop(0)
        index = 0
        for action in node._actions:
            if action.option_strings or isinstance(
                action, argparse._SubParsersAction
            ):
                continue
            slot = _slot(path, action)
            if slot:
                found[f"{path}#{index}"] = slot
            index += 1
        for name, child in sorted(_subcommands(node).items()):
            pending.append((f"{path} {name}".strip(), child))
    return found


def value_options(parser: argparse.ArgumentParser) -> list[str]:
    """Lists every option string that consumes the word after it.

    The shell scripts skip that word when they walk the command line, so
    ``--home DIR participant`` still reaches the ``participant`` command.

    Args:
        parser: Root parser of the program.

    Returns:
        Sorted option strings, across the whole tree, whose action takes a
        value.
    """
    found: set[str] = set()
    pending = [parser]
    while pending:
        node = pending.pop(0)
        for action in node._actions:
            if action.option_strings and action.nargs != 0:
                found.update(action.option_strings)
        pending.extend(_subcommands(node).values())
    return sorted(found)


def _bash(
    entries: list,
    values: dict,
    slots: dict,
    takes: list,
    program: str,
    mode: str,
) -> str:
    """Emits a Bash completion function for the walked tree.

    The function walks the typed words, skipping each option and the value
    an option in ``takes`` consumes, descends the command tree while the
    words name subcommands, and counts the positionals after that. The count
    selects the positional slot to complete; a word starting with ``-``
    completes the command's flags instead. Table keys start with the program
    name, because Bash rejects the empty key the root path would need.

    Args:
        entries: Result of `tree`.
        values: Result of `value_flags`.
        slots: Result of `positionals`.
        takes: Result of `value_options`.
        program: Installed command name to complete.
        mode: Hidden subcommand that prints state-backed candidates.

    Returns:
        A script defining the completion function and registering it.
    """
    name = f"_{program.replace('-', '_')}"
    table = "\n".join(
        f'    ["{f"{program} {path}".strip()}"]='
        f'"{" ".join([*children, *flags])}"'
        for path, children, flags in entries
    )
    slot_table = "\n".join(
        f'    ["{f"{program} {key}".replace(" #", "#")}"]="{slot}"'
        for key, slot in sorted(slots.items())
    )
    cases = "\n".join(
        f"        {option}) _kind={kind};;"
        for option, kind in sorted(values.items())
    )
    return f"""{name}_tree() {{
  declare -gA {name.upper()}_TREE=(
{table}
  )
  declare -gA {name.upper()}_SLOTS=(
{slot_table}
  )
  {name.upper()}_TAKES=" {" ".join(takes)} "
}}
{name}() {{
  local cur prev key probe word opts slot _kind i n skip
  {name}_tree
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  _kind=""
  case "$prev" in
{cases}
  esac
  if [ -n "$_kind" ]; then
    COMPREPLY=( $(compgen -W "$({program} {mode} "$_kind")" -- "$cur") )
    return
  fi
  key="{program}"
  n=0
  skip=0
  for ((i=1; i<COMP_CWORD; i++)); do
    word="${{COMP_WORDS[i]}}"
    if [ "$skip" = 1 ]; then
      skip=0
      continue
    fi
    case "$word" in
      --*=*) continue;;
      -*)
        case "${{{name.upper()}_TAKES}}" in *" $word "*) skip=1;; esac
        continue;;
    esac
    probe="$key $word"
    if [ "$n" = 0 ] && [ -n "${{{name.upper()}_TREE[$probe]+x}}" ]; then
      key="$probe"
    else
      n=$((n+1))
    fi
  done
  if [ "$skip" = 1 ]; then
    COMPREPLY=( $(compgen -f -- "$cur") )
    return
  fi
  case "$cur" in
    -*) ;;
    *)
      slot="${{{name.upper()}_SLOTS[$key#$n]-}}"
      case "$slot" in
        @*)
          COMPREPLY=( $(compgen -W "$({program} {mode} "${{slot#@}}")" \\
            -- "$cur") )
          return;;
        ?*)
          COMPREPLY=( $(compgen -W "$slot" -- "$cur") )
          return;;
      esac;;
  esac
  opts="${{{name.upper()}_TREE[$key]}}"
  COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
}}
complete -F {name} {program}
"""


def _zsh(
    entries: list,
    values: dict,
    slots: dict,
    takes: list,
    program: str,
    mode: str,
) -> str:
    """Emits a Zsh completion function for the walked tree.

    The walk matches the one `_bash` emits, so both shells offer the same
    candidates for the same words.

    Args:
        entries: Result of `tree`.
        values: Result of `value_flags`.
        slots: Result of `positionals`.
        takes: Result of `value_options`.
        program: Installed command name to complete.
        mode: Hidden subcommand that prints state-backed candidates.

    Returns:
        A script beginning with the `#compdef` marker Zsh expects.
    """
    name = f"_{program.replace('-', '_')}"
    table = "\n".join(
        f'    ["{path}"]="{" ".join([*children, *flags])}"'
        for path, children, flags in entries
    )
    slot_table = "\n".join(
        f'    ["{key}"]="{slot}"' for key, slot in sorted(slots.items())
    )
    cases = "\n".join(
        f"    {option}) _kind={kind};;"
        for option, kind in sorted(values.items())
    )
    return f"""#compdef {program}
{name}() {{
  local -A nodes slots
  local key probe word opts slot takes _kind n skip
  nodes=(
{table}
  )
  slots=(
{slot_table}
  )
  takes=" {" ".join(takes)} "
  _kind=""
  case "${{words[CURRENT-1]}}" in
{cases}
  esac
  if [[ -n "$_kind" ]]; then
    compadd -- ${{(f)"$({program} {mode} $_kind)"}}
    return
  fi
  key=""
  n=0
  skip=0
  for word in ${{words[2,CURRENT-1]}}; do
    if (( skip )); then
      skip=0
      continue
    fi
    if [[ "$word" == --*=* ]]; then
      continue
    fi
    if [[ "$word" == -* ]]; then
      [[ "$takes" == *" $word "* ]] && skip=1
      continue
    fi
    probe="${{key:+$key }}$word"
    if (( n == 0 )) && [[ -n "${{nodes[$probe]+x}}" ]]; then
      key="$probe"
    else
      (( n += 1 ))
    fi
  done
  if (( skip )); then
    _files
    return
  fi
  if [[ "${{words[CURRENT]}}" != -* ]]; then
    probe="$key#$n"
    slot="${{slots[$probe]}}"
    if [[ "$slot" == @* ]]; then
      compadd -- ${{(f)"$({program} {mode} ${{slot#@}})"}}
      return
    fi
    if [[ -n "$slot" ]]; then
      compadd -- ${{=slot}}
      return
    fi
  fi
  opts="${{nodes[$key]}}"
  compadd -- ${{=opts}}
}}
compdef {name} {program}
"""


def _fish_condition(path: str) -> str:
    """Builds the Fish condition that holds once a command path is typed.

    Args:
        path: Space-joined command path; empty for the root.

    Returns:
        A condition for `complete -n`.
    """
    if not path:
        return "__fish_use_subcommand"
    return "; and ".join(
        f"__fish_seen_subcommand_from {word}" for word in path.split()
    )


def _fish(
    entries: list,
    values: dict,
    slots: dict,
    takes: list,
    program: str,
    mode: str,
) -> str:
    """Emits Fish completion rules for the walked tree.

    Fish matches rules by condition rather than by position, so a command's
    positional values are offered for each of its positional words, and the
    options a value follows are declared with ``-x`` so Fish completes their
    value instead of a subcommand.

    Args:
        entries: Result of `tree`.
        values: Result of `value_flags`.
        slots: Result of `positionals`.
        takes: Result of `value_options`, unused because Fish already skips
            the value of an option declared to take one.
        program: Installed command name to complete.
        mode: Hidden subcommand that prints state-backed candidates.

    Returns:
        One `complete` line per command path, per positional slot and per
        state-backed option.
    """
    lines: list[str] = []
    for path, children, flags in entries:
        offered = " ".join([*children, *flags])
        if not offered:
            continue
        lines.append(
            f"complete -c {program} -f -n '{_fish_condition(path)}' "
            f"-a '{offered}'"
        )
    for key, slot in sorted(slots.items()):
        path = key.rpartition("#")[0]
        offered = f"({program} {mode} {slot[1:]})" if slot[0] == "@" else slot
        lines.append(
            f"complete -c {program} -f -n '{_fish_condition(path)}' "
            f"-a '{offered}'"
        )
    for option, kind in sorted(values.items()):
        flag = option.lstrip("-")
        switch = "-l" if option.startswith("--") else "-s"
        lines.append(
            f"complete -c {program} -x {switch} {flag} "
            f"-a '({program} {mode} {kind})'"
        )
    return "\n".join(lines) + "\n"


EMITTERS = {"bash": _bash, "zsh": _zsh, "fish": _fish}


def script(
    parser: argparse.ArgumentParser,
    shell: str,
    program: str = "agent-parley",
    mode: str = "__complete",
) -> str:
    """Generates a completion script for one shell from the parser tree.

    Args:
        parser: Root parser of the program, walked as it exists at run time.
        shell: One of the names in `SHELLS`.
        program: Installed command name the script completes.
        mode: Hidden subcommand the script calls for state-backed values.

    Returns:
        The script text, for the operator to source or install where their
        shell expects it.

    Raises:
        ValueError: The shell is not one this module emits.
    """
    if shell not in EMITTERS:
        raise ValueError(
            f"Unknown shell {shell}. Choose from: {', '.join(SHELLS)}."
        )
    return EMITTERS[shell](
        tree(parser),
        value_flags(parser),
        positionals(parser),
        value_options(parser),
        program,
        mode,
    )
