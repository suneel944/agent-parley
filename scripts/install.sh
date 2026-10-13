#!/bin/sh
# Installs Agent Parley and adds its plugin to each supported native CLI.
#
#   curl -LsSf https://github.com/suneel944/agent-parley/releases/latest/download/install.sh | sh
#
# Installs uv only when it is missing, installs or upgrades the agent-parley
# tool, runs `agent-parley plugins install` and `agent-parley doctor`, then
# prints the next command. It never uses sudo and never edits a shell startup
# file: when a directory must join PATH, it prints the line to add. Re-running
# upgrades in place. AGENT_PARLEY_SPEC replaces the package spec, such as a
# wheel path, and is reinstalled with --force on every run. Every step runs
# from main, called on the last line, so a download cut short by the pipe runs
# nothing.

set -eu

REFUSAL="agent-parley: native Windows is not supported; install and run it \
inside WSL2 with the repository in the Linux file system. See \
https://github.com/suneel944/agent-parley/blob/main/docs/operations.md#platforms"

say() {
    printf '%s\n' "$*"
}

on_path() {
    case ":$PATH:" in
        *":$1:"*) return 0 ;;
    esac
    return 1
}

path_notes=""

need_on_path() {
    if ! on_path "$1"; then
        PATH="$1:$PATH"
        export PATH
        path_notes="$path_notes
  export PATH=\"$1:\$PATH\""
    fi
}

main() {
case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW* | MSYS* | CYGWIN* | Windows_NT)
        say "$REFUSAL" >&2
        exit 2
        ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv from https://astral.sh/uv/install.sh"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
    else
        say "agent-parley: install curl or wget, then rerun this script." >&2
        exit 1
    fi
    need_on_path "${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
    if ! command -v uv >/dev/null 2>&1; then
        say "agent-parley: uv installed but was not found on PATH." >&2
        exit 1
    fi
fi

spec="${AGENT_PARLEY_SPEC:-agent-parley}"
if [ "$spec" != agent-parley ]; then
    say "Installing agent-parley from $spec"
    uv tool install --force "$spec"
elif uv tool list 2>/dev/null | grep -q '^agent-parley '; then
    say "Upgrading agent-parley"
    uv tool install --force agent-parley
else
    say "Installing agent-parley"
    uv tool install agent-parley
fi
need_on_path "$(uv tool dir --bin)"

agent-parley --version
failed=0
agent-parley plugins install || failed=1
agent-parley doctor || failed=1

if [ -n "$path_notes" ]; then
    say ""
    say "Add this to your shell startup file so new terminals find it:"
    say "$path_notes"
fi

if [ "$failed" -ne 0 ]; then
    say ""
    say "Setup finished with the errors above; fix them and rerun this script."
    exit 1
fi

provider=""
if command -v claude >/dev/null 2>&1; then
    provider=claude
elif command -v codex >/dev/null 2>&1; then
    provider=codex
fi
say ""
if [ -n "$provider" ]; then
    say "Next: cd your-repo && agent-parley run $provider"
else
    say "Next: install claude or codex, then cd your-repo && agent-parley run <provider>."
    say "No CLI yet? Try: agent-parley demo"
fi
}

main "$@"
