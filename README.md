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
query. Every one of them accepts `--json`. The mail readers take `--as NAME`,
so the message `problems` cites opens from the main checkout: it reads that
lane's mail and sends, acknowledges and marks nothing on its behalf.

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

## Notifications when you step away

A lane can wait a long time on a handoff acceptance, a permission prompt or a
fresh claim. Agent Parley can forward that moment to a Telegram bot or an email
address, outbound only: nothing comes back, no command arrives over the channel,
and a native permission prompt is still answered only in your terminal.

Five changes notify, and nothing else: a handoff offered to a lane, a lane
blocked on a native permission prompt, a lane idle with no claim past the
project's `stalled_after` grace period, a lane run that finished, and a hook
refusal such as a branch switch or detected drift. A situation that has not
changed sends nothing further. Sending never blocks a hook or a tool call, a
failed send is recorded in the lane's event log and dropped, and nothing is
queued for a retry.

Configuration is entirely environment variables, read by the service and the
launcher; no token is ever written into coordination state.

| Variable | Meaning |
| --- | --- |
| `AGENT_PARLEY_NOTIFY` | Comma-separated transports: `telegram`, `email`, or both. Unset means notifications are off. |
| `AGENT_PARLEY_TELEGRAM_TOKEN` | Bot token from BotFather. |
| `AGENT_PARLEY_TELEGRAM_CHAT` | Chat identifier the bot posts to. |
| `AGENT_PARLEY_SMTP_HOST` | SMTP server host. |
| `AGENT_PARLEY_SMTP_PORT` | SMTP port; defaults to 587, or 465 with implicit TLS. |
| `AGENT_PARLEY_SMTP_TLS` | `starttls` (default), `implicit` or `none`. |
| `AGENT_PARLEY_SMTP_USER` | SMTP user; omit for a server that needs no login. |
| `AGENT_PARLEY_SMTP_PASSWORD` | SMTP password. |
| `AGENT_PARLEY_SMTP_FROM` | Sender address. |
| `AGENT_PARLEY_SMTP_TO` | Comma-separated recipients. |

```bash
export AGENT_PARLEY_NOTIFY=telegram,email
agent-parley notify test
```

`notify test` sends one message on each configured transport and prints what
each one answered, so credentials are verified before a lane depends on them.
It exits 1 when any transport refuses.

## Asking for status from the chat

The same Telegram bot can answer one question and only one: what is everything
doing. Send `status` with the filters `agent-parley status` takes, and the
reply is the reading that command prints. Nothing else crosses the channel: no
claim, no handoff, no wake, no permission approval, and no free text into a
session. The service long-polls the Bot API from inside itself, so no port is
opened and no webhook is registered.

Every message starts with a passcode, and both the passcode and the chat
identifier must match:

```
hunter2-and-then-some status --pending
hunter2-and-then-some status codex
hunter2-and-then-some status --provider claude --issue 14
```

| Variable | Meaning |
| --- | --- |
| `AGENT_PARLEY_INBOUND` | `telegram` turns the reader on. Unset means no inbound path at all. |
| `AGENT_PARLEY_INBOUND_PASSCODE` | Passcode every message must start with; at least 12 characters. |

The bot token and chat identifier are the outbound ones above. A message from
another chat, or with a wrong passcode, gets no reply at all: silence, not a
hint. Five wrong passcodes inside ten minutes lock the inbound path for an hour
and send one outbound notification saying so; the counter and the lock live
only in memory. Only a salted hash of the passcode is held, compared in
constant time, and it is never written to coordination state, the event log or
the service log. The accepted message is deleted from the chat when the bot has
permission, so the passcode does not sit in the history. A reading longer than
one Telegram message is cut with a line naming how many rows were left out.

The reader refuses to start when the passcode is unset or shorter than twelve
characters, and `agent-parley status` prints that fault instead of leaving a
silently dead poller behind:

```
Inbound: AGENT_PARLEY_INBOUND_PASSCODE must be set and at least 12 characters;
inbound status queries are off.
```

## What it does not do

Worktrees and reservations are coordination boundaries, not OS sandboxes. Agent
Parley integrates a lane only when you run `participant merge`, and it never
approves a command. The runtime can wake an idle lane to review pending mail,
with global and per-lane opt-outs and a bounded number of attempts per backlog.
Reported `ready` is ready for review, not verified completion. Token usage still
depends on the native agents: `CONTEXT` reports the bytes coordination itself
injects and `TOKENS` repeats what a lane's own client counted, and neither is
billed spend or a claim about a token-saving percentage.

## How it compares

Every tool below runs several coding agents at once, each in its own Git
worktree. The difference is what happens between the worktrees. Each claim is
taken from the project's own documentation, linked so you can check it.

| Project | What its own documentation describes | What Agent Parley records instead |
| --- | --- | --- |
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | A terminal manager for background sessions, each in its own worktree, over Claude Code, Codex, Aider and Amp. Isolation is the conflict answer: separate workspaces, "so no conflicts". | The same isolation, plus state the worktrees share: an atomic issue claim, an advisory reservation that names the blocking owner and reason, and a handoff that only moves ownership when a peer accepts it. |
| [Crystal](https://github.com/stravu/crystal) | Parallel Claude Code and Codex sessions with diffs and test output in one window. The repository now points to its successor, Nimbalyst, and its README describes editor streaming and worktree isolation. | A record rather than a view: who holds which issue, which paths are reserved, what evidence a lane attached to a `ready` report, and whether a peer reviewed that report. |
| [Conductor](https://conductor.build) | A polished macOS app for running Claude Code in parallel worktrees. Closed source, macOS only. | A standard-library service with no runtime dependencies that runs wherever Python 3.12 does, drives Claude, Codex, Gemini, Amp, OpenCode and Copilot through their own CLIs, and keeps its coordination state outside your repository. |
| [Vibe Kanban](https://github.com/BloopAI/vibe-kanban) | A task board in front of coding agents. Its vendor announced a shutdown in April 2026 and the project continues community-maintained and fully local. | Coordination in the agents' own path rather than a board in front of it: native hooks refuse a branch switch inside an assigned lane and catch drift after a bypass, which no board can see. |

Two things none of them document, and the reasons they matter here:

- **A decision log every lane can search.** A message a lane marks as a
  decision, or one an operator records with `agent-parley decide`, becomes
  project-wide, so a third lane stops relitigating a settled question.
- **A refusal to sign your work.** A commit, merge, tag or pull request that
  credits an assistant is denied before it lands, and no flag skips the check.

Agent Parley does not replace these tools' strengths. Conductor is the smoother
macOS experience, and a board is easier to read at a glance than a table. Pick
Agent Parley when several agents must agree about one repository, and the
answer to "who owns this, and on what evidence" has to be recorded rather than
remembered.

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
