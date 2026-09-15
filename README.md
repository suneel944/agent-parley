<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/agent-parley.png" width="560" alt="Agent Parley — separate work, shared context">
</p>

<p align="center">
  <strong>Separate worktrees. Shared context. One screen.</strong>
</p>

<p align="center">
  Run several coding agents at once and know who owns what.<br>
  Every claim, handoff and refusal is recorded, attributed and visible.
</p>

<p align="center">
  <a href="https://github.com/suneel944/agent-parley/releases"><img src="https://img.shields.io/github/v/release/suneel944/agent-parley?style=flat&color=blue" alt="Release"></a>
  <a href="#install"><img src="https://img.shields.io/badge/runtime_dependencies-0-brightgreen?style=flat" alt="Zero runtime dependencies"></a>
  <a href="#install"><img src="https://img.shields.io/badge/python-3.12%2B-blue?style=flat" alt="Python 3.12+"></a>
  <a href="https://github.com/suneel944/agent-parley/blob/main/docs/providers.md"><img src="https://img.shields.io/badge/native_CLIs-claude_%2B_codex-orange?style=flat" alt="claude and codex"></a>
  <a href="https://github.com/suneel944/agent-parley/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#see-it">See it</a> ·
  <a href="#install">Install</a> ·
  <a href="#run-it">Run</a> ·
  <a href="https://github.com/suneel944/agent-parley/blob/main/docs/coordination.md">Coordination</a> ·
  <a href="https://github.com/suneel944/agent-parley/blob/main/docs/monitoring.md">Monitoring</a> ·
  <a href="https://github.com/suneel944/agent-parley/blob/main/docs/providers.md">Providers</a> ·
  <a href="https://github.com/suneel944/agent-parley/blob/main/docs/commands.md">Commands</a> ·
  <a href="#what-it-does-not-do">Limits</a>
</p>

---

## See it

One screen for every lane: session state, branch drift, issues owned, handoffs
pending, unread mail, held reservations, delivered context, what enforcement
denied, and what that lane's own client recorded for its session. Read-only, no
model call, `q` quits.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-top.svg" width="900" alt="agent-parley top showing three lanes with issues, mail, leases, denials and served calls">
</p>

A lane reads `active` while it is serving coordination calls, `idle` once a
live lane passes the inactivity threshold, and `stopped` only when its
recorded session process is gone. A quiet lane is not a lost one. Every frame
on this page is real command output from a demo project; only the state and
project paths are shortened.

[Monitoring](https://github.com/suneel944/agent-parley/blob/main/docs/monitoring.md)
covers the whole view: the columns, the keys,
the filters, `problems`, `metrics` and `watch`.

## Install

Linux, macOS or WSL2 with the repository in the Linux file system, Git, and
[uv](https://docs.astral.sh/uv/). No clone. The wheel needs no third-party
runtime packages.

```sh
uv tool install agent-parley
# to track the default branch instead:
# uv tool install git+https://github.com/suneel944/agent-parley
```

Then add the plugin to whichever CLI you drive. One marketplace serves both.

```sh
claude plugin marketplace add suneel944/agent-parley
claude plugin install agent-parley@agent-parley
```

```sh
codex plugin marketplace add suneel944/agent-parley
codex plugin add agent-parley@agent-parley
```

The plugin carries the shared `coordinate` skill, so an agent can read
coordination state, claim an issue and hand work off in its own words. It is
deliberately skill-only: the launcher supplies MCP configuration and lifecycle
hooks per session, and it is also what creates the worktrees and runs the
coordination service. The plugin alone gives an agent the skill and nothing to
coordinate through.

For a pinned, checksummed install, take a wheel from
[Releases](https://github.com/suneel944/agent-parley/releases) instead. Shell
completion, upgrades and the supported platforms are in
[Operations](https://github.com/suneel944/agent-parley/blob/main/docs/operations.md#install-and-upgrade).

## Run it

From a committed, clean checkout, one terminal per agent:

```sh
# Terminal 1
agent-parley run claude

# Terminal 2
agent-parley run codex
```

That is the whole setup. The first run registers the repository, creates that
participant's worktree and branch, starts the coordination service, and hands
you the native CLI. Prompt it exactly as you always do. A new name creates its
own lane, so a second account of the same provider, or another provider, is one
more terminal.

Then watch the work, and steer a lane without taking over its terminal:

```sh
agent-parley status   # one table per project: ownership, activity, outcomes
agent-parley top      # every lane live, including what enforcement denied
agent-parley problems # only what needs you now, oldest first
agent-parley say claude-2 "Rebase onto main before you open the pull request."
```

`agent-parley` on its own prints the grouped command list, `agent-parley
--version` prints the installed version, and `agent-parley version` adds the
state directory in use. One thing at a time reads through a `show` verb —
`issue show 42`, `participant show claude-2`, `provider show claude`,
`credentials show work` — and `mail list` prints the inbox without a search
query. Every one of them accepts `--json`.

When a lane's work is ready, integrate it from the base checkout, or send it
for review:

```sh
agent-parley participant merge claude-2
agent-parley participant pr claude-2
```

[Running lanes](https://github.com/suneel944/agent-parley/blob/main/docs/lanes.md)
covers the rest of the operator surface:
deferred and bulk steering, pausing, stopping and restarting a lane, the
merge plan for several lanes at once, the pre-merge gate, recorded approvals,
and the setup command every new lane runs.

## What it enforces

- **Ownership changes only through explicit claims and accepted handoffs.** No
  timeout and no process exit moves an issue, and a recorded dependency informs
  rather than gates.
- **Choosing work is a reading, not a guess.** `agent-parley issue next` ranks
  the unclaimed, unblocked issues a lane could take, with the reason for each
  place, and claims nothing.
- **Native hooks decide before the tool runs.** They block branch changes
  inside an assigned lane, catch drift after any bypass, and deliver bounded
  updates only when coordination state actually changes.
- **A deadline reports; it never transfers.** A budget informs; it does not
  gate. Both mark the lane and stop nothing.
- **Reservations are advisory.** Conflicts name the blocking owner and that
  owner's declared reason; nothing on disk is locked.
- **Mail stays private; a decision does not.** Only a message a lane marks as a
  decision, or one an operator records with `agent-parley decide`, enters the
  project-wide log every lane can search, so a third lane stops repeating a
  settled question.
- **Your history stays yours.** No lane signs its work, and a commit, merge,
  tag or pull request that credits an assistant is denied before it lands, with
  no flag that skips the check.
- **An out-of-date install says so before it costs a turn.** The launcher, the
  plugin, the store and the running service each state a version, and a
  mismatch is refused at the boundary with the command that fixes it.

Each of these is documented in full, with the commands and the recorded
evidence, in
[Coordination](https://github.com/suneel944/agent-parley/blob/main/docs/coordination.md).

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-hooks.svg" width="820" alt="Two hook denials with their reasons, and the bounded briefing a session start receives">
</p>

## How it fits together

```mermaid
flowchart TD
    Repo[Your repository] --> Launcher[Agent Parley launcher]
    Launcher --> Claude[Participant · own worktree]
    Launcher --> Codex[Participant · own worktree]
    Claude <-->|Ten scoped MCP tools| Server[Local coordination service]
    Codex <-->|Ten scoped MCP tools| Server
    Server --> DB[(SQLite WAL · mail and reservations)]
    Claude --> Claims[Atomic issue claims and handoffs]
    Codex --> Claims
    DB --> Hooks[Native checkpoints · bounded updates]
    Claims --> Hooks
    Hooks -.-> Claude
    Hooks -.-> Codex
```

The coordination engine is built in-house with Python's standard library. It has
no runtime dependencies and makes no model calls. Your existing logins and
permission settings still apply.

## What it does not do

Worktrees and reservations are coordination boundaries, not OS sandboxes. Agent
Parley integrates a lane only when you run `participant merge`, and it never
approves a command. The runtime can wake an idle lane to review pending mail,
with global and per-lane opt-outs and a bounded number of attempts per backlog.
Reported `ready` is ready for review, not verified completion. Token usage still
depends on the native agents: `CONTEXT` reports the bytes coordination itself
injects and `TOKENS` repeats what a lane's own client counted, and neither is
billed spend or a claim about a token-saving percentage.

## Documentation

| Page | What it covers |
| --- | --- |
| [Running lanes](https://github.com/suneel944/agent-parley/blob/main/docs/lanes.md) | Launching, steering, pausing, merging, pull requests, gates and approvals. |
| [Coordination](https://github.com/suneel944/agent-parley/blob/main/docs/coordination.md) | Claims, handoffs, reservations, mail, hooks, deadlines, budgets and history. |
| [Monitoring](https://github.com/suneel944/agent-parley/blob/main/docs/monitoring.md) | `status`, `top`, `problems`, `metrics`, `watch` and the three presence states. |
| [Providers](https://github.com/suneel944/agent-parley/blob/main/docs/providers.md) | Which native CLI drives a lane, adapters, accounts and credential profiles. |
| [Commands](https://github.com/suneel944/agent-parley/blob/main/docs/commands.md) | The whole command surface, the MCP tools and the `--json` contract. |
| [Operations](https://github.com/suneel944/agent-parley/blob/main/docs/operations.md) | The operator reference: install, platforms, recovery, plugins and releases. |
| [Architecture](https://github.com/suneel944/agent-parley/blob/main/docs/architecture.md) | Module boundaries, protocol, persistence and stated limits. |

## Contributing

Run `make check` before opening a PR. It checks formatting, lint, typing,
documentation rules, package builds, and behavior tests.

[Contributing](https://github.com/suneel944/agent-parley/blob/main/CONTRIBUTING.md) ·
[Architecture](https://github.com/suneel944/agent-parley/blob/main/docs/architecture.md) ·
[Operations](https://github.com/suneel944/agent-parley/blob/main/docs/operations.md) ·
[Security](https://github.com/suneel944/agent-parley/blob/main/SECURITY.md) ·
[Code of Conduct](https://github.com/suneel944/agent-parley/blob/main/CODE_OF_CONDUCT.md) ·
[MIT license](https://github.com/suneel944/agent-parley/blob/main/LICENSE)
