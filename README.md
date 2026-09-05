<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-bridge@cc7ed01606973ffc9b524366da149763bfd119d5/docs/assets/agent-bridge.png" width="560" alt="Agent Bridge — separate work, shared context">
</p>

# Agent Bridge

Work with Claude Code and Codex in separate terminals without losing track of
who owns what. Agent Bridge gives each agent a Git worktree and a shared place
for issue claims, file reservations, messages, and handoffs.

The coordination engine is built in-house with Python's standard library. It
has no runtime dependencies and makes no model calls. Your existing Claude and
Codex logins and permission settings still apply.

## Get started

Requires Linux with pidfd support, Git, [uv](https://docs.astral.sh/uv/), and signed-in
`claude` and `codex` commands. Python 3.12+ is installed by uv if needed.

```sh
git clone https://github.com/suneel944/agent-bridge.git
cd agent-bridge
make install
```

Then, from any directory, open two terminals:

```sh
# Terminal 1
agent-bridge run claude --repo /path/to/your/repo --task "Work on issue 42"

# Terminal 2
agent-bridge run codex --repo /path/to/your/repo --task "Work on issue 43"
```

Start from a committed, clean checkout. The launcher creates both worktrees;
the agents claim issues from their assigned lanes. Run `agent-bridge status`
to see ownership, recent activity, and reported results.

## How it fits together

```mermaid
flowchart TD
    Repo[Your repository] --> Bridge[Agent Bridge launcher]
    Bridge --> Claude[Claude Code · own worktree]
    Bridge --> Codex[Codex · own worktree]
    Claude <-->|Six scoped MCP tools| Server[Local coordination service]
    Codex <-->|Six scoped MCP tools| Server
    Server --> DB[(SQLite WAL · mail and reservations)]
    Claude --> Claims[Atomic issue claims and handoffs]
    Codex --> Claims
    DB --> Hooks[Native checkpoints · bounded updates]
    Claims --> Hooks
    Hooks -.-> Claude
    Hooks -.-> Codex
```

Issue ownership changes only through explicit claims and accepted handoffs.
Conflicting file reservations grant nothing. Hooks deliver short updates when
state changes; unchanged checkpoints add no context. Each notice is capped at
1,536 UTF-8 bytes, and full message bodies are fetched only when needed.

Worktrees and reservations are coordination boundaries, not OS sandboxes.
Agent Bridge does not merge branches, approve commands, or wake idle agents.
Token usage still depends on the native agents; the bridge reports injected
bytes rather than claiming a universal token-saving percentage.

## Plugins and releases

Claude Code and Codex plugins share the same coordination skill. Installation,
upgrades, and marketplace details are in [Operations](docs/operations.md).
Download packages, plugin bundles, changelogs, and checksums from
[Releases](https://github.com/suneel944/agent-bridge/releases).

## Contributing

Run `make check` before opening a PR. It checks formatting, lint, typing,
documentation rules, package builds, and behavior tests.

See [Contributing](CONTRIBUTING.md), [Architecture](docs/architecture.md),
[Security](SECURITY.md), and the [Code of Conduct](CODE_OF_CONDUCT.md).
Agent Bridge is available under the [MIT license](LICENSE).
