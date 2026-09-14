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

Steer a lane's life without typing into its terminal either:

```sh
agent-parley participant pause claude-2     # refuse its calls, keep its work
agent-parley participant resume claude-2    # let it act again
agent-parley participant stop claude-2      # end its session cleanly
agent-parley participant restart claude-2   # start it again from clean state
```

A paused lane keeps its session, its claims and its reservations. Only acting is
refused: every coordination call and every tool use comes back denied naming the
operator, and `top` shows `paused`. `stop` tells the lane once, then signals the
recorded session process exactly as a normal exit does, and it never signals a
process whose recorded identity no longer matches. `restart` refuses while a
session is alive, refuses a dirty worktree naming the paths, replays the recorded
`init` command and launches the same provider and account as before. None of the
four releases a claim: ownership still moves only through an explicit release or
an accepted handoff, and all four land in the event log.

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
streams the command's output, refusing the merge on a non-zero exit and
reporting the exit status. It runs as an argument list, never through a shell, and no flag
skips it. It reports the base checkout as it stands before the merge, which is
not a claim about the merged result.

A new lane starts as a bare worktree, so every agent would otherwise spend its
first turns installing dependencies or copying an untracked file. Record that
setup once instead:

```sh
agent-parley init set 'uv sync --locked'   # run it in every new lane
agent-parley init show                     # report what runs
agent-parley init set ''                   # remove it
```

The command runs in the new worktree after it is created and before the native
CLI starts, as an argument list, never through a shell, and no flag skips it. It
runs only when a lane is created, never on a resume, and `participant add` runs
it as well. `AGENT_PARLEY_BASE` names the base checkout while it runs, so the
command can copy a file Git does not track. A non-zero exit refuses the launch
and reports the exit status with the tail of the output; the worktree is left in
place so you can see what happened.

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
If a rename removed the assigned branch, the hook names the exact repair.
Branch drift blocks completion once; a Stop retry can end the session while
status continues to show the drift.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-hooks.svg" width="820" alt="Two hook denials with their reasons, and the bounded briefing a session start receives">
</p>

**A deadline reports; it never transfers.** `issue claim 42 --within 2h`,
`issue offer ... --within 30m` and `say ... --ack --within 15m` record a
deadline, and `agent-parley deadlines set` gives a project defaults to inherit.
Past its deadline a claim reads `overdue` with the seconds over, `top` marks the
issue `#42!`, and a lane that reports `blocked` on work it still holds spends one
attempt of the recorded budget. Ownership never moves on a timer: an overdue
claim is still owned, and only an explicit release or an accepted handoff
transfers it.

**The work order is a file you can review.** Write the issues, the dependencies
between them and the groups that may run in parallel as TOML, then
`agent-parley plan apply work-order.toml`. Applying records the same advisory
dependencies `issue block` records and nothing else — no claim, no assignment, no
gate. `plan diff` previews the edges first, `plan show` prints the plan as a tree
with each issue's current owner, and every apply is versioned by the file's
digest, so an edge added by hand afterwards is reported as exactly that.

**A stalled lane says so.** `top` and `status` mark a lane `idle` when its
process is alive, no coordination call has been served for it within the
configured interval, and it holds unread or unacknowledged mail at least that
old — and they name the oldest waiting item and how long it has waited. The
marker only reports: nothing is revoked, no claim is released and no ownership
moves.

**Your history stays yours.** A lane branch is `parley/PROJECT_KEY/lane-N`: it
carries no participant, provider or account name, and `agent-parley branch set
PREFIX` changes the prefix per project. No lane signs its work either. A commit,
merge, tag or pull request that credits an assistant, names a vendor or model in
an authorship position, or carries a generator signature is denied before it
lands, and every commit an integration would carry is scanned again at
`participant merge` and `participant pr`. There is no flag that skips either
check, on any repository or for any provider. Which assistant did the work stays
in coordination state, where `top` and `status` read it.

**Nine scoped MCP tools carry the coordination.** Conflicting reservations
grant nothing and name the blocking owner with that owner's declared reason. A
reservation can name something that is not a file — `port:5432`, `db:local`,
`suite:integration`, `device:android-1` — because a worktree isolates none of
those; a named resource conflicts on an exact match, and
`agent-parley resources set` declares which ones exist.
Sends need an idempotency key, so a retry returns the original message instead
of a duplicate. Every other write takes one too — reservations, releases,
acknowledgements, issue transitions and reports — so a retried call returns the
first result and changes nothing further, and the same key with different
arguments is refused rather than applied. Fetching an inbox never marks a
message read. A send can answer
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

**Idle time is a number, not an impression.** `top` gains an `IDLE` column: how
long each lane went without coordination activity inside the window, with the
project total and the worst lane in the header. `status` prints the same figure
and, under it, every pending item with the seconds it has already waited — a
message before its first read, an `ack_required` message before acknowledgement,
a handoff offer before an answer, a `ready` report before integration. Every
figure comes from records the runtime already keeps, so it reports observed
coordination inactivity and never claims to know what the native client was
doing inside a turn.

**Ownership history is queryable.** `agent-parley history issue 42` lists every
claim, handoff, reservation, message and report that touched it, with how long
each participant held it; `history participant NAME` does the same for one lane,
and `history claim ID` follows one claim through to the pull request that ended
it. Every claim carries its own identifier, minted again on each claim and each
accepted handoff, and every record made while it is held carries that identifier.
It reads only: no lock, no rewrite, and a record older than the correlation
reports `unknown` rather than being given an invented one.

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
a model. Every provider names one of four adapters, and the adapter decides how
that CLI is handed its MCP server, its coordination prompt and its hooks. Two of
the four take a published plugin, which is why two plugin installations cover
every model-endpoint preset:

| Provider | Native CLI it drives | Plugin that carries `coordinate` |
| --- | --- | --- |
| `claude` | `claude` | Claude Code |
| `codex` | `codex` | Codex |
| `copilot` | `copilot` | none; the launcher writes that lane's files |
| `gemini` | `gemini` | none; the launcher writes a private settings overlay |
| `deepseek`, `kimi`, `grok` | `claude` or `codex`, vendor endpoint | that adapter's plugin |
| your own, via `agent-parley provider add` | the adapter you name | that adapter's plugin |

`claude`, `codex` and `gemini` use their native accounts. The `deepseek`, `kimi`
and `grok` presets carry no endpoint, so their base URL and key must be exported in
the launching shell; the launcher refuses to start when a required variable is
unset rather than falling back to another account. Coordination state records
variable names and config directories, never credential values.

`agent-parley run gemini` starts Gemini CLI with a lane-private MCP and hook
overlay while preserving native system settings and authentication. Existing
explicit provider definitions keep their adapter until you update them.

Credential profiles point a provider's config-home variable at a separate
directory, so one provider can run under several logins. Up to 32 participants
per project.

### Other agent CLIs

Four adapters cover the native configuration contracts. `claude` and
`codex` take MCP servers, the coordination prompt and lifecycle hooks as
command-line arguments. `copilot` reads them from files instead, so Agent
Parley writes `mcp-config.json` and `settings.json` into that lane's own
Copilot configuration directory; a `copilot` lane therefore requires a
credential profile, and the launcher refuses without one rather than writing
hooks into the configuration directory your own sessions use.

OpenCode and Amp remain recipes. OpenCode runs plugins rather than hook
commands; Amp accepts no system-prompt argument.
[Operations](docs/operations.md#other-agent-clis) records what each one
supports and where its MCP and hook configuration lives.

## Command reference

Use `agent-parley COMMAND --help` for arguments. `--home DIR` selects private
state globally; repository commands accept `--repo PATH` unless noted below.
Issue mutations, reports and lane mail resolve identity from the current lane.

| Command | Purpose |
| --- | --- |
| `up` | Start the local coordination server. |
| `down` | Stop the server while retaining state and worktrees. |
| `status` | Show server health, ownership, activity and reported outcomes. |
| `setup PATH` | Register a repository from committed HEAD. |
| `run NAME` | Launch a lane; supports `--provider`, `--credentials`, `--repo`, and `--task`. |
| `top` | Watch lanes; `--once` prints a snapshot, `--interval` sets refresh seconds, `--provider` and `--since` filter it. |
| `report` | Record `--state`, `--summary`, and required `--remaining` or `--evidence`; `--idempotency-key` makes a retry safe. |
| `say NAME TEXT` | Send as `operator`; `--ack` requests acknowledgement and `--key` controls deduplication. |
| `issue list` | Show claims, dependencies and handoff offers. |
| `issue claim NUMBER` | Claim an available issue from this lane. |
| `issue release NUMBER` | Release ownership without closing the GitHub issue. |
| `issue offer NUMBER --to NAME --summary TEXT` | Pause work and offer ownership explicitly. |
| `issue accept NUMBER --offer-id ID` | Accept the current offer addressed to this lane. |
| `issue decline NUMBER --offer-id ID` | Decline the current offer addressed to this lane. |
| `issue cancel NUMBER` | Cancel this lane's pending handoff offer. |
| `issue block NUMBER --on NUMBER` | Record an advisory issue dependency. |
| `issue unblock NUMBER --on NUMBER` | Remove a recorded dependency. |
| `plan apply PATH` | Record a TOML work order as advisory dependencies; `plan diff PATH` previews it. |
| `plan show` | Print the applied plan as a tree with owners; `--json` prints it for scripts. |
| `issue ... --idempotency-key KEY` | Retry any transition safely; the repeat returns the first result. |
| `participant list` | List the project's lanes and their identities. |
| `participant add NAME` | Create a lane with an optional provider and credential profile. |
| `participant restore NAME` | Restore the assigned branch while preserving work. |
| `participant retire NAME` | Retire an idle lane while preserving recoverable work. |
| `participant pause NAME` | Refuse a lane's calls and tool use; keep its session and claims. |
| `participant resume NAME` | Let a paused lane act again. |
| `participant stop NAME` | End a lane's session from the base checkout; keep its claims. |
| `participant restart NAME` | Start a stopped lane again from a clean worktree. |
| `participant merge NAME` | Run the configured gate and merge; `--preview` only inspects. |
| `participant pr NAME` | Push the lane branch and open or locate its pull request. |
| `provider list` | List built-in presets and local overrides. |
| `provider add NAME` | Define a provider; warn when shadowing a built-in preset. |
| `provider remove NAME` | Delete a local definition, restoring a shadowed preset. |
| `credentials list` | List native account profiles. |
| `credentials add NAME` | Define a config home and environment requirements. |
| `credentials remove NAME` | Delete a profile definition, preserving native files and logins. |
| `branch show` | Show the prefix new lane branches are created under. |
| `branch set PREFIX` | Set that prefix; existing lanes keep their branch. |
| `resources show` | Show the named resources lanes may reserve. |
| `resources set NAMES` | Declare them; an empty string accepts any well-formed name. |
| `deadlines show` | Show this project's deadline and attempt defaults. |
| `deadlines set` | Set `--claim`, `--offer`, `--ack` windows and `--attempts`. |
| `verify show` | Show the project's configured pre-merge command. |
| `verify set COMMAND` | Set that command; an empty string removes it. |
| `init show` | Show the command every new lane runs before it starts. |
| `init set COMMAND` | Set that command; an empty string removes it. |
| `mail thread ID` | Read this lane's messages in a thread; `--after-id` pages forward. |
| `mail search QUERY` | Search this lane's mail with an optional `--limit`. |
| `history issue N` | List every record that touched an issue, with each holding. |
| `history participant NAME` | List everything one lane filed. |
| `history claim ID` | Follow one claim to the pull request that ended it. |
| `events export` | Export JSON Lines; filter by `--participant` and `--since`, or write `--output FILE`. |

Every read-only command above also accepts `--json` and prints exactly one JSON
document, so a script, a shell prompt or another agent reads coordination state
without parsing a table: `status`, `top`, `issue list`, `participant list`,
`mail thread`, `mail search`, `verify show`, `init show`, `provider list` and
`credentials list`. `top --json` prints one frame and exits. The document
carries the identifiers the table abbreviates — offer, message and thread IDs —
with every time in RFC 3339, and no credential value. Field names are
documented in [docs/operations.md](docs/operations.md) and carry the same
stability promise as the flags. `events export` stays JSON Lines, because it is
a stream rather than a snapshot.

Removing a definition leaves participant references intact. Redefine that name
before relaunching a lane that uses it. A removed provider override immediately
reveals its built-in preset, if any.

The live dashboard drops columns by priority on narrow terminals and marks
hidden participants when space runs out. `running; no hooks` means the session
needs relaunching to obtain checkpoint reporting. `top --once` prints full detail.

### MCP tools

Authentication supplies the lane identity; tool arguments cannot select another
participant or project. Reservations are advisory, not filesystem locks.

| Tool | Purpose |
| --- | --- |
| `send_message` | Send to peers using an idempotency key; optionally join a thread or require acknowledgement. |
| `fetch_inbox` | Page inbox metadata and optional bodies; filter with `unread` or `unacknowledged`. |
| `mark_message_read` | Explicitly mark a received message read; takes an optional `idempotency_key`. |
| `acknowledge_message` | Explicitly acknowledge a reviewed message; takes an optional `idempotency_key`. |
| `file_reservation_paths` | Reserve advisory path patterns and report conflicts; takes an optional `idempotency_key`. |
| `release_file_reservations` | Release reservations owned by this lane; takes an optional `idempotency_key`. |
| `list_participants` | Discover addressable identities, tasks and last coordination times. |
| `read_thread` | Page messages this lane sent or received in one thread. |
| `search_messages` | Search only messages this lane sent or received. |

Inbox rows include `read_ts` and `ack_ts`. Fetching changes neither. Both filters
can be combined; `unacknowledged` selects messages that requested an acknowledgement
and have not received it. The result budget is 8,192 UTF-8 bytes.

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
approves a command. The runtime can wake an idle lane to review pending mail,
with global and per-lane opt-outs and at most three attempts per backlog.
Reported `ready` is ready for review,
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
