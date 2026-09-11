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
  <a href="#providers-and-accounts"><img src="https://img.shields.io/badge/native_CLIs-claude_%2B_codex-orange?style=flat" alt="claude and codex"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#see-it">See it</a> ·
  <a href="#install">Install</a> ·
  <a href="#run-it">Run</a> ·
  <a href="#what-it-enforces">Enforce</a> ·
  <a href="#watch-one-provider">Filter</a> ·
  <a href="#providers-and-accounts">Providers</a> ·
  <a href="docs/architecture.md">Docs</a> ·
  <a href="#what-it-does-not-do">Limits</a>
</p>

---

## See it

Launch a lane, see who owns what, watch every lane at once, narrow to one
provider, and watch a hook refuse a branch switch inside an assigned lane.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-top.svg" width="900" alt="agent-parley top showing three lanes with issues, mail, leases, denials and served calls">
</p>

One screen for every lane: session state, branch drift, issues owned, handoffs
pending, unread mail, held reservations, delivered context, what enforcement
denied, and what that lane's own client recorded for its session. Read-only, no
model call, `q` quits.

A reservation that declared a time to live is counted with `!` once that
deadline passes, so a lane that died holding a path reads differently from one
still working on it; nothing is revoked, and releasing it stays its owner's
decision. `TOKENS` is the session total that lane's own native client already
wrote to disk. No vendor is asked, no key is read and no price is applied, so
it is a relative signal between refreshes rather than billed spend, and the
cell is blank when nothing could be read.

Every frame on this page is real command output from a demo project. Only the
state and project paths are shortened.

## Install

Linux or macOS, Git, and [uv](https://docs.astral.sh/uv/). No clone.
The wheel needs no third-party runtime packages.

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
coordination state, claim an issue and hand work off in its own words. It is deliberately
skill-only: the launcher supplies MCP configuration and lifecycle hooks per
session, and it is also what creates the worktrees and runs the coordination
service. The plugin alone gives an agent the skill and nothing to coordinate
through.

For a pinned, checksummed install, take a wheel from
[Releases](https://github.com/suneel944/agent-parley/releases) instead.

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
you the native CLI. Prompt it exactly as you always do.

A new name creates its own lane, so a second account of the same provider, or
another provider, is one more terminal:

```sh
agent-parley credentials add account-2 --config-home ~/.claude-account-2
agent-parley run claude-2 --provider claude --credentials account-2
```

Then watch the work:

```sh
agent-parley status   # ownership, activity and reported results
agent-parley top      # every lane live, including what enforcement denied
```

Steer one lane without taking over its terminal:

```sh
agent-parley say claude-2 "Rebase onto main before you open the pull request."
```

The message lands in that lane's inbox beside peer traffic, so the agent reads
it at its next checkpoint. It comes from `operator`, a command-line identity: no
participant can be named `operator`, and no MCP tool sends as it, so an agent
cannot write in its name. Repeating the same message delivers nothing further,
and `--ack` asks the lane to acknowledge it.

When a lane's work is ready, integrate it from the base checkout:

```sh
agent-parley participant merge claude-2
```

It always records a merge commit, refuses on a running session, a dirty tree or
a drifted lane, and leaves a conflict in place for you to resolve. It never
resets, cleans, stashes or force-switches.

To see what that would bring in, and everything that would refuse it right now,
ask for a preview first:

```sh
agent-parley participant merge claude-2 --preview
```

The preview only reads. It changes nothing, and it never takes the lane's
session lock, so it is safe while that agent is still working. It attempts no
merge, so it cannot predict conflicts.

A repository can also require its own command to pass before any merge. The
command is recorded in coordination state, not in the repository:

```sh
agent-parley verify set 'make check'   # require it from now on
agent-parley verify show               # report what is required
agent-parley verify set ''             # remove the requirement
```

With one configured, `participant merge` runs it in the base checkout first and
refuses the merge on a non-zero exit, reporting the exit status and the tail of
the output. It runs as an argument list, never through a shell, and no flag
skips it. It reports the base checkout as it stands before the merge, which is
not a claim about the merged result.

Or send it for review instead:

```sh
agent-parley participant pr claude-2
```

That pushes the lane's branch and opens one pull request whose body is the
lane's own recorded report, under the headings your pull-request template asks
for, referencing the issue the lane claimed. It opens assigned to you and
labelled from that issue, so it arrives owned and classified rather than needing
repair. It uses your own `gh` sign-in, refuses when there is no report, no
claimed issue, no change type on that issue or nothing to push, and reports an
already-open pull request rather than opening a second one.

## What it enforces

**Ownership changes only through explicit claims and accepted handoffs.** No
timeout and no process exit moves an issue. `agent-parley status` reports who
owns what, which handoff is waiting on an offer ID, and any lane that left its
assigned branch. An owner can record that one issue waits on another with
`agent-parley issue block 42 --on 17`; the listing then names who holds the
blocking issue, and every lane sees the change at its next checkpoint. A
recorded dependency informs, it does not gate.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-status.svg" width="880" alt="agent-parley status listing issue owners, a pending handoff, and a lane on the wrong branch">
</p>

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-issues.svg" width="880" alt="agent-parley issue list showing an issue that waits on another, the participant holding it, and a pending handoff with its offer ID">
</p>

**Native hooks decide before the tool runs.** They block branch changes inside
an assigned lane, catch drift after any bypass, and deliver short updates only
when coordination state actually changes. Each notice is capped at 1,536 UTF-8
bytes; an unchanged checkpoint adds no context at all.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-hooks.svg" width="820" alt="Two hook denials with their reasons, and the bounded briefing a session start receives">
</p>

**Nine scoped MCP tools carry the coordination.** Conflicting reservations
grant nothing and name the blocking owner with that owner's declared reason.
Sends need an idempotency key, so a retry returns the original message instead
of a duplicate. Fetching an inbox never marks a message read. A send can answer
another message, which puts both in one thread, and a participant can read a
thread in order or search its own mail:

```sh
agent-parley mail thread t12
agent-parley mail search "reservation conflict"
```

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-coordination.svg" width="880" alt="A granted reservation, a denied one naming the blocking owner, a deduplicated send, and an inbox page">
</p>

Enforcement is recorded, not discarded. Every hook decision carries an
enumerated reason and lands in that participant's event log; every served call
is recorded inside the transaction that carried its effect. That is why `top`
can show what was denied, to whom, and how often.

That history is bounded, and it can leave the state directory. A lane keeps two
event files and discards records older than fourteen days, so `top` reports
recent enforcement rather than the whole project. `--since` narrows any count
to a window, and `events export` writes the retained records as JSON Lines you
can keep for as long as you need:

```sh
agent-parley top --since 6h
agent-parley events export --since 7d --output enforcement.jsonl
```

## Watch one provider

With a dozen lanes open, the whole table is rarely what you want. `--provider`
narrows the view to the participants driven by one provider, and the header
counts only the rows it shows:

```sh
agent-parley top --provider codex
agent-parley top --provider claude --provider codex
```

## Providers and accounts

A provider states which native CLI drives a participant and how that CLI reaches
a model. Every provider names one of three adapters, and the adapter decides how
that CLI is handed its MCP server, its coordination prompt and its hooks. Two of
the three take a published plugin, which is why two plugin installations cover
every model-endpoint preset:

| Provider | Native CLI it drives | Plugin that carries `coordinate` |
| --- | --- | --- |
| `claude` | `claude` | Claude Code |
| `codex` | `codex` | Codex |
| `copilot` | `copilot` | none; the launcher writes that lane's files |
| `deepseek`, `kimi`, `grok`, `gemini` | `claude` or `codex`, vendor endpoint | that adapter's plugin |
| your own, via `agent-parley provider add` | the adapter you name | that adapter's plugin |

`claude` and `codex` work out of the box. The `deepseek`, `kimi`, `grok` and
`gemini` presets carry no endpoint, so their base URL and key must be exported in
the launching shell; the launcher refuses to start when a required variable is
unset rather than falling back to another account. Coordination state records
variable names and config directories, never credential values.

A preset names the vendor whose models answer, not a vendor's own agent CLI:
`gemini` reaches Gemini models through the `codex` CLI, exactly as `grok` and
`kimi` do for theirs.

Credential profiles point a provider's config-home variable at a separate
directory, so one provider can run under several logins. Up to 32 participants
per project.

### Other agent CLIs

Three adapters cover three ways of accepting configuration. `claude` and
`codex` take MCP servers, the coordination prompt and lifecycle hooks as
command-line arguments. `copilot` reads them from files instead, so Agent
Parley writes `mcp-config.json` and `settings.json` into that lane's own
Copilot configuration directory; a `copilot` lane therefore requires a
credential profile, and the launcher refuses without one rather than writing
hooks into the configuration directory your own sessions use.

Gemini CLI, OpenCode and Amp are not covered. Gemini CLI publishes no variable
that relocates `~/.gemini`, so it cannot be given a lane of its own; OpenCode
runs plugins rather than hook commands; Amp accepts no system-prompt argument.
[Operations](docs/operations.md#other-agent-clis) records what each one
supports and where its MCP and hook configuration lives.

## How it fits together

```mermaid
flowchart TD
    Repo[Your repository] --> Launcher[Agent Parley launcher]
    Launcher --> Claude[Participant · own worktree]
    Launcher --> Codex[Participant · own worktree]
    Claude <-->|Nine scoped MCP tools| Server[Local coordination service]
    Codex <-->|Nine scoped MCP tools| Server
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
approves a command or wakes an idle agent. Reported `ready` is ready for review,
not verified completion. Token usage still depends on the native agents:
`CONTEXT` reports the bytes coordination itself injects and `TOKENS` repeats
what a lane's own client counted, and neither is billed spend or a claim about
a token-saving percentage.

## Contributing

Run `make check` before opening a PR. It checks formatting, lint, typing,
documentation rules, package builds, and behavior tests.

[Contributing](CONTRIBUTING.md) · [Architecture](docs/architecture.md) ·
[Operations](docs/operations.md) · [Security](SECURITY.md) ·
[Code of Conduct](CODE_OF_CONDUCT.md) · [MIT license](LICENSE)
