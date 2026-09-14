# Operations

## Install and upgrade

Installing needs no clone. `uv tool install agent-parley` takes the published
distribution from PyPI. `uv tool install git+https://github.com/suneel944/agent-parley`
tracks the default branch instead, and a release wheel URL pins an exact
version. The wheel needs no third-party runtime packages in any of the three
cases.

The distribution, the command and the import package are all named after the
project: `agent-parley`, `agent-parley` and `agent_parley`.

```sh
uv tool install agent-parley
```

From a checkout, `make install` installs a package snapshot in uv's user
executable directory. If needed, run `uv tool update-shell` and open a new
terminal. `make install-dev` installs an editable checkout.
`sudo env "PATH=$PATH" make install-system` installs into `/usr/local/bin`, with
its environment under `/opt/agent-parley`. Each user's runtime state remains
private.

Upgrading needs no action: the store upgrades in place on first use, keeping
every message, claim and lease. Stop running sessions first, and do not point an
older installation at an upgraded state directory afterwards.

## Daily use

```sh
agent-parley status
agent-parley top
agent-parley participant list
agent-parley issue list
agent-parley issue claim 42
agent-parley issue offer 42 --to codex --summary "commit, checks, remaining work"
agent-parley issue accept 42 --offer-id CURRENT_OFFER_ID
agent-parley issue block 42 --on 17
agent-parley issue unblock 42 --on 17
agent-parley report --state ready --summary "Result" --evidence "Checks and results"
agent-parley say codex "Rebase onto main before you open the pull request."
agent-parley mail thread THREAD_ID
agent-parley mail search "reservation conflict"
```

`issue offer --to` names another participant in the same project.

`issue list` also shows the forge title beside the owner, as
`#42: claude — Some issue title`, when `gh` is installed and authenticated and
the repository's `origin` remote points at GitHub. Without any of those the
title is simply absent and nothing else changes: the claim still succeeds and
every listing, dependency and handoff behaves the same.

Under the same conditions a claim assigns the issue to your GitHub account, a
release unassigns it, and a lane's first `report --state ready` posts that
lane's summary and evidence as a comment on every issue it claims. These
mirrors use your own `gh` sign-in and run after the local ledger is written, so
a forge that is missing, offline or unwilling changes nothing about ownership.
Because every lane runs under your one account, a handoff between participants
changes the ledger owner without changing the assignee, and change-type labels
are left to you.

`say NAME "text"` writes one message into that participant's inbox as
`operator`, so a supervisor steering several lanes can redirect one without
typing into its terminal. The message uses the same send path as peer mail: the
lane sees it at its next checkpoint, alongside agent traffic. Without `--key`
the idempotency key follows the message itself, so repeating an identical
message delivers nothing further, while changed text is a new message. `--ack`
requires the lane to acknowledge it, and `--subject` replaces the default
subject line. The command refuses, naming the reason, when the repository has
no project, when the name is not in the roster, and when that participant has
never launched and so has no registered identity.

`operator` is reserved. It cannot be claimed as a participant, provider or
credential profile name, it never holds a coordination credential, and no MCP
tool sends as it, so a served agent cannot write in the operator's name.

`mail thread ID` prints one thread in send order and `mail search QUERY` reports
the messages matching it. Both read as the lane whose worktree they run in, the
same way reports and issue transitions do, so they answer for one participant's
own mail rather than for the project, and neither marks anything read. A thread
page carries up to ten messages and a search up to five, each with a
240-character body preview; `--after-id` continues a thread page and `--limit`
narrows a search. Where SQLite was built without the full-text index a search
matches the query as a literal case-insensitive substring rather than as
indexed terms, and every result names which of the two answered it.

`issue list` prints each owner's session state and the age of its last
checkpoint, so a stalled lane is visible. Reclaiming that work still needs the
owner to release it, or an explicit offer and accept.

`issue block NUMBER --on OTHER` records that one issue waits on another. Only
the current owner of NUMBER can add or drop a dependency, and an issue records
at most ten. `issue list` then names the participant holding each blocking
issue, or reports it unclaimed. Every ledger change bumps the revision, so the
next checkpoint delivers the updated dependency line to each running lane
without polling. Dependencies survive release and reclaim.

A dependency is information, not a gate. Nothing prevents work on a waiting
issue, no transition clears a dependency, and finishing the blocking issue does
not drop the edge; the owner runs `issue unblock` when the wait is over.

`agent-parley top` watches every participant live: session state, event age,
branch with a `!` when a lane left its assigned branch, issues owned and
handoffs pending, unread and unacknowledged mail, held leases with the age of
the oldest, delivered context, denials against retained hook events, served
MCP calls with rejections, and the tokens that lane's own native client
recorded. The header carries server health and the project's
denial rate. The view is read-only and makes no model call; `q` leaves it.

A reservation can name something that is not a file. Lanes collide on one local
database, one dev-server port, one hardware device, one integration suite that
cannot run twice at once, and a worktree isolates none of them. The same
reservation tools accept a named resource written with a scheme, so it can never
be confused with a path:

```sh
port:5432
db:local
suite:integration
device:android-1
```

A named resource conflicts on an exact match only: no glob, no prefix and no
path containment applies, because a port number is not a directory. Everything
else is unchanged — the same 128-lease cap, the same optional `ttl_seconds` and
stale marking, the same conflict naming the owner and that owner's reason, the
same release by owner only, and the same rows in `top`, whose `LEASES` cell
counts paths and named resources together. `status` additionally lists the named
resources a lane holds, because a name is short enough to read and a path list
is not.

A project can declare which resources exist, so a mistyped name is refused
before two lanes reserve two spellings of the same thing:

```sh
agent-parley resources show
agent-parley resources set 'port:5432 db:local suite:integration'
agent-parley resources set ''      # accept any well-formed name again
```

With a declaration in place, an undeclared name is refused with the declared
list in the message. With none, every well-formed name is accepted. The
declaration lives in coordination state beside the roster, so it commits
nothing to the target repository, and it grants nothing: it only narrows what
may be reserved.

`top` and `status` mark a lane `idle` when its recorded session process is
alive, no coordination call has been served for it within the configured
interval, and it holds unread mail or an unacknowledged message at least that
old. The marker names the oldest waiting item and how long it has waited, so a
stalled lane reads differently from a busy one instead of looking healthy in
every column. It is read-only: nothing is revoked, no claim is released, no
ownership moves and no lane is woken by it. A lane whose session process is not
alive is never marked idle, because `stopped` already says that. `--provider`
selects which lanes are reported, exactly as for every other column; `--since`
narrows counted history and does not change the idle interval, which is its own
setting.

The interval defaults to 600 seconds. Set it per project under `supervision` in
that project's manifest in coordination state, or globally in
`supervision.json` beside it, as `stalled_after`; it accepts 1 to 86400
seconds, the same bounds as `inactive_after`.

A reservation may declare `ttl_seconds`, and one taken without it never
reports as stale. Once a declared time to live passes, the LEASES count in
`top` gains `!` and the stale count, `status` reports the stale share of a
lane's reservations, and a conflict names that holder as stale. The lease is
still held: nothing revokes it, reassigns it, or narrows what it blocks, and
only its owner releases it. Reading `!` as "an agent died holding this" is the
point; acting on it is the operator's decision, exactly as with a stalled
issue owner.
Use `--once`, or pipe it, for one plain snapshot instead of a live view, and
`--interval` to change the redraw period. `--provider NAME` reports only the
participants driven by that provider and is repeatable; the header then counts
only the reported rows, and a project holding no selected participant says so
rather than reading as empty. Session state follows the recorded
session process, not the session lock, so watching a lane never blocks a
launch. Denial counts cover the whole retained event log, the current file and
the one rotated file together, so a count reports every record still kept rather
than the current file alone. Only those two files are kept, and records older
than fourteen days are discarded at the next session start or session end, so a
long-running lane reports recent enforcement, not project history; served-call
counts cover the most recent 2000 events per project.

`IDLE` is how long that lane went without coordination activity inside the
window. An idle interval opens when a turn ends, which the native client
reports as a `Stop` or `SessionEnd` checkpoint, and closes at the lane's next
recorded activity; the interval still open when the view is drawn counts only
while the recorded session process is alive, because a stopped lane is stopped
rather than idle. The header carries the project total and the lane holding the
largest share. A `+` after the figure means the window reaches past what
retention kept, so the number understates the truth rather than pretending to
be exact.

The figure is derived from coordination state and is honest about it: it says
how long a lane went without coordination activity, and it does not claim to
know what the native client was doing inside a turn. A lane can be thinking
hard and still show idle time here.

`status` prints the same figure per lane and, under it, every pending item with
the seconds it has already waited, so an offer that has waited eleven minutes
reads as eleven minutes rather than as a bare offer ID. Four waits are
measured: a message from delivery to its first read, an `ack_required` message
to its acknowledgement, a handoff offer to its acceptance or decline, and a
`ready` report to the merge, pull request or retirement that ended it. Each
wait reports whether it has ended, so a pending wait is never read as an
answered one. Report and integration records are kept per lane beside the event
log, which is what makes the last of those four a subtraction rather than a
guess; they survive `participant retire`, because history about finished work is
still history.

`events export` carries all of it. Every line names its kind in `record`:
`event` for a hook decision, `idle_interval` for a measured stretch of
inactivity, and `wait` for one of the four waits. The intervals and waits leave
with the records, so they can be compared across sessions, providers and
accounts after retention has discarded the events they were derived from.

`TOKENS` is what that lane's own native client recorded for its session, read
from the session records the client already keeps on disk: no vendor request,
no API key, no price. Treat it as a relative signal between refreshes of the
same lane. It is **not** billed spend, and it is not comparable between
vendors, which count differently. It is also unrelated to `CONTEXT`, which
measures only the bytes coordination itself injects. The records are looked up
under the config home that lane launched with, so a credential profile that
relocates the config home is followed. The cell is blank whenever nothing
could be read — records absent, unreadable, malformed, or in a shape a client
version changed — and a blank cell means "not read", never "spent nothing".
Claude is read from that client's transcript for the lane's working directory.
Codex is read from the rollout whose own record names that lane, searched only
in the last two days of rollouts, so a Codex session older than that reports
nothing. Reading is incremental: each refresh folds only the records appended
since the previous one, up to 1 MiB per lane, so watching a long session never
re-reads its history.

`--since` narrows every event count to a window that ends at the current
reading, so `agent-parley top --since 6h` answers what happened in the last six
hours rather than across the whole retained log. Accepted windows are a count
followed by `s`, `m`, `h` or `d`. The header states the window, or `all
retained` when none is given. A window can only narrow what retention already
kept: it never recovers a record that rotation or the age bound discarded.

```sh
agent-parley top --since 6h
agent-parley events export --since 7d --output enforcement.jsonl
agent-parley events export --participant claude-1 > claude-1-events.jsonl
```

`events export` writes the retained hook event records as JSON Lines, one
record per line, each naming the participant that produced it. Records go to
standard output unless `--output` names a file, and the count is reported on
standard error in that case so a redirected export stays valid JSON Lines.
`--participant` selects one lane and is repeatable; every participant is
exported by default. `--since` takes the same windows as `top`. Export reads
state and never writes it, and it is the supported way to keep enforcement
history beyond what the state directory retains.

### Machine-readable output

Every read-only command also accepts `--json` and prints exactly one JSON
document on standard output:

```sh
agent-parley status --json
agent-parley top --json
agent-parley issue list --json
agent-parley participant list --json
agent-parley mail thread THREAD_ID --json
agent-parley mail search "reservation conflict" --json
agent-parley verify show --json
agent-parley init show --json
agent-parley provider list --json
agent-parley credentials list --json
```

`top --json` prints one frame and exits rather than drawing the live view;
`--provider` and `--since` narrow it exactly as they narrow the table. Write
commands are unchanged, and a refusal still goes to standard error with a
non-zero exit status, so a script tells a refusal from a document by the exit
status alone.

Every document carries the same envelope:

| Field | Meaning |
| --- | --- |
| `schema` | `agent-parley/read/v1`, the version of this contract. |
| `kind` | The command reported: `status`, `top`, `issues`, `participants`, `mail_thread`, `mail_search`, `verify`, `init`, `providers` or `credentials`. |
| `generated_at` | RFC 3339 UTC instant the snapshot was taken. |

Repeated rows are arrays rather than objects keyed by name, so a reader pages
them without knowing the identifiers in advance, and every recorded time is RFC
3339 in UTC whether the lane state files or SQLite recorded it. The document
carries the identifiers the table abbreviates: offer IDs on `issues`, message
and thread IDs on `mail_thread` and `mail_search`, participant names, registered
identities and branch names everywhere they apply. It carries no credential
value; a credential profile is named, never its contents.

`resources show --json` reports `root`, the declared `resources` array and
`declared`. `status` reports `server`, `state_directory` and one entry per
project holding
`root`, the issue ledger as `revision` and `issues`, and `participants`. Each
participant carries `participant`, `identity`, `provider`, `credential`,
`session`, `availability`, `branch`, `assigned_branch`, `drift`, `paused`,
`outcome`, `summary`, `remaining`, `evidence`, `reported_at`,
`report_age_seconds`, `injected_bytes`, `injections`, `idle`, `idle_seconds`,
`idle_complete`, `waiting`, `wake` and `mail`, whose `named_resources` array
lists the named resources that lane holds. `idle` carries `stalled`, the
waiting item's `kind`, `message_id`, `sender` and `age_seconds`, the
`served_age_seconds` since the last served call, and the same `marker` the
table prints. `waiting` carries one record per pending wait, longest first,
each with its `kind`, its item and `seconds`. A
mailbox that cannot be read reports `{"error": "..."}` in `mail` rather than
failing the document, exactly as the table reports coordination as unavailable.

`top` reports `server`, `state_directory`, `window_seconds`, `providers`,
`totals` and one entry per project holding `root` and `participants`. Each row
carries `participant`, `provider`, `credential`, `state`, `last_event_at`,
`stalled`, `stall`, `branch`, `drift`, `issues`, `offers`, `unread`,
`pending_ack`, `leases`,
`stale_leases`, `lease_age_seconds`, `injected_bytes`, `hook_events`,
`denials`, `calls`, `errors`, `tokens`, `idle_seconds`, `idle_complete` and
`prompt`. `tokens` is null when that
lane's own session records could not be read, and `unread` and `pending_ack`
are null when its mailbox could not be read: null states that nothing was read,
never that the count is zero.

`issues` reports `revision` and an `issues` array whose records carry `issue`,
`owner`, `title`, `blocked_by`, `offer` and `reminder`. `participants` reports
`root` and a `participants` array carrying `participant`, `identity`,
`provider`, `credential`, `branch`, `lane`, `paused` and `wake`.

These field names carry the same stability promise as the command-line flags:
removing a field is a breaking change, and a release that adds one raises the
schema version only when an existing field changes meaning. `events export`
stays JSON Lines, one record per line, because it is a stream rather than a
snapshot; `--json` snapshots and that stream are separate contracts.

## Participants, providers and accounts

### Availability, reminders and waking

The local service observes each launcher's process identity and native checkpoint
age. `status` distinguishes a stopped process from a live but quiet lane and
lists outstanding acknowledgement IDs, senders and ages. Sending an
`ack_required` message returns an availability warning when the latest runtime
observation marks its recipient unreachable. Observed availability is separate
from last coordination and never changes claims.

The private project manifest accepts `"supervision"` with `interval` (default
30 seconds), `inactive_after` (300 seconds), `prompts` and `wake` (both true).
Numeric values range from 1 to 86400 seconds. The same keys in
`$AGENT_PARLEY_HOME/supervision.json` set global defaults; global false values for
`wake` and `prompts` cannot be enabled by a project. A participant entry may set
`"wake": false` to opt out individually. These settings remain outside source.

Releasing a claim with waiting peers creates a visible handoff reminder.
The service also checks claimed lane PRs on each poll and reminds holders when
one is merged or closed. Forge lookups are bounded and best effort; an offline
forge cannot establish completion. Reminders appear in issue/status output and
at checkpoints. An explicit subsequent message reaching every waiting peer
marks a response observed; that is delivery evidence, not proof of a complete
handoff. Ownership still moves only through the explicit offer/accept protocol.

For eligible idle sessions, the launcher owns a native pseudo-terminal and a
private wake socket. It admits only a fixed coordination prompt at a native idle
checkpoint, with no partially entered operator input. Approval prompts and
active turns refuse injection. A stopped session can resume its recorded session
ID through the same native launch configuration and an interactive terminal;
`agent-parley run NAME --resume` exposes that operation explicitly. Native trust,
authentication and permission prompts remain in force. Environment-only vendor
accounts that cannot be reconstructed safely require manual attention.

Wake attempts are separated by the inactivity interval and capped at three for
each unchanged backlog. Results appear in `status`, the retained event log and
private `<name>-wake.json`; resumed terminal output stays in `<name>-wake.log`.
Lanes launched before wake sockets were introduced require relaunching. An
unavailable adapter or socket is reported for manual attention. Waking never
marks mail read, acknowledges it, releases reservations or transfers an issue.

The automated tests exercise local processes, pseudo-terminals, hook payloads
and real MCP transport. They do not establish live model behavior for a provider.

```sh
agent-parley provider list
agent-parley credentials add account-1 --config-home ~/.claude-account-1
agent-parley participant add claude-1 --provider claude --credentials account-1
agent-parley run claude-1 --task "Work on issue 44"
agent-parley provider add vendor --adapter claude --executable claude \
  --home-env CLAUDE_CONFIG_DIR --env ANTHROPIC_BASE_URL=https://vendor.example \
  --require-env ANTHROPIC_AUTH_TOKEN
```

`run` creates a participant's lane on first use, so `participant add` is only
needed to prepare a roster in advance. Worktrees start at committed HEAD, so the
first registration of a repository stashes any pending work in the base checkout
instead of refusing to start. It names the stash entry and prints the
`git stash apply` that brings that specific entry back, because every worktree
of a repository shares one stash stack. Only that first registration touches the
base checkout; `merge`, `restore` and `retire` still refuse on a dirty tree. The participant name is what peers
address; the provider decides which native CLI starts and which endpoint it uses.
The `deepseek`, `kimi` and `grok` presets need their vendor base URL
and key exported in the launching shell; the launcher refuses to start when a
required variable is unset, rather than falling back to another account.

The `gemini` preset starts the native Gemini CLI. It copies native system policy
into a lane-private settings overlay, adds the MCP endpoint and translated
checkpoint hooks, and selects that overlay with
`GEMINI_CLI_SYSTEM_SETTINGS_PATH`. Native user/project settings and authentication
remain in effect. Credential profiles may select `GEMINI_CLI_HOME`, whose
`.gemini` subdirectory holds that account's native configuration. Existing
explicit provider definitions are preserved. The integration follows Gemini's
[configuration](https://geminicli.com/docs/reference/configuration/) and
[hook contracts](https://geminicli.com/docs/hooks/reference/).

Credential profiles point a provider's config-home variable at a separate
directory so one provider can run under several accounts. Define one profile per
subscription; there is no limit besides the 32-participant project cap, and the
accounts need no relationship to each other. Sign in to each directory with the
native CLI once. Agent Parley stores directory paths and
variable names; it never stores tokens or keys, and rejects `--env` values whose
names look like credentials.

## Other agent CLIs

Agent Parley hands the native CLI its MCP server, the coordination prompt and
its lifecycle hooks at launch. Three contracts implement that, and `--adapter`
names the one to use:

| Adapter | MCP server | Coordination prompt | Hooks |
| --- | --- | --- | --- |
| `claude` | `--mcp-config FILE` | `--append-system-prompt TEXT` | `--settings '{"hooks":…}'` |
| `codex` | `-c mcp_servers.agent_parley.url=…` | appended to the prompt argument | `-c hooks.EVENT=…` |
| `copilot` | `mcp-config.json` in the lane's `COPILOT_HOME` | prepended to the `-p` argument | `hooks` in `settings.json` there |

A provider's `--executable` therefore has to accept every argument of the
contract its adapter names. The `copilot` adapter is the file-configured one:
Copilot CLI reads MCP servers and hooks from its configuration directory rather
than from arguments, so Agent Parley writes both files into the directory
`COPILOT_HOME` selects. That directory also holds the client's own
authentication, and user-level hooks there apply to every session started from
it, so a `copilot` participant requires a credential profile and the launcher
refuses one without a profile instead of writing lane hooks into the directory
your own Copilot sessions use. Sign in to that directory once, as with any
other profile.

Copilot CLI chooses its hook payload format from the case of the configured
event name. A camelCase name such as `preToolUse` delivers camelCase fields
(`sessionId`, `toolName`, `toolArgs`); a PascalCase name such as `PreToolUse`
delivers the compatible snake_case fields (`session_id`, `hook_event_name`,
`tool_input`) that the shared checkpoint parser reads. Lane hooks are registered
under the PascalCase names for that reason, and the hook command carries
`--adapter copilot`. Copilot reads `permissionDecision`,
`permissionDecisionReason` and `additionalContext` at the top level of a hook
result rather than inside `hookSpecificOutput`, so the adapter flattens the
shared output into those fields. It never answers a native approval prompt and
never grants a permission Copilot refused.

The launch path and the hook wire contract were exercised against a stub
executable and by running the exact configured hook command with native
payloads, not against a live Copilot session, so argument handling, file
handling and hook translation are verified while live model behavior is not.

OpenCode and Amp remain uncovered, each for a different reason
recorded below. `agent-parley provider add` will store a definition naming one
of them, because the command is only resolved on `PATH` at launch, but the
resulting session fails inside the native CLI. There is no flag that makes it
work and none should be added.

| Agent CLI | MCP configuration | Lifecycle hooks | Per-account config home |
| --- | --- | --- | --- |
| Gemini CLI | lane-private system settings overlay | translated native hooks | `GEMINI_CLI_HOME` |
| Copilot CLI | `$COPILOT_HOME/mcp-config.json`, or `copilot mcp` | `hooks` in `$COPILOT_HOME/settings.json` | `COPILOT_HOME` |
| OpenCode | `mcp` in `opencode.json`, or `opencode mcp add` | JavaScript plugins only | `OPENCODE_CONFIG_DIR` |
| Amp | `--mcp-config`, or `amp.mcpServers` in its settings file | `amp.hooks` in its settings file | `--settings-file`, `AMP_SETTINGS_FILE` |

Copilot CLI and Gemini CLI have native adapters. The other two do not.
Amp accepts `--mcp-config`, but it has no `--append-system-prompt`, and its
settings arrive through `--settings-file` rather than `--settings`, so two
thirds of the `claude` contract is rejected. OpenCode extends sessions through
JavaScript plugins rather than hook commands, so lane checkpoints and the
enforcement record would have no way to run.

## Recovery and teardown

```sh
agent-parley status
agent-parley participant restore claude-1
agent-parley participant merge claude-1
agent-parley participant pr claude-1
agent-parley participant retire claude-1
```

A lane that ends a session on the wrong branch, or on a detached HEAD, blocks
only its own participant; every other lane still launches. `status` prints the
actual branch whenever it differs from the assigned one.

`participant restore` returns one lane to its bridge branch. It refuses while
that participant has a running session, refuses on an uncommitted change, and
refuses when the current branch holds commits the bridge branch does not,
printing the command that keeps them. It never resets, cleans, stashes, or
force-switches, so no committed or uncommitted work is discarded.

### Lane branches and attribution

A lane branch carries no participant, provider or account name. It is created as
`PREFIX/PROJECT_KEY/lane-N`, where `PREFIX` defaults to `parley`, `PROJECT_KEY`
is the private project key, and `N` is the next free lane ordinal. Which
provider drives a lane belongs in coordination state, where `top`, `status` and
`participant list` read it; it does not belong in your Git history or on your
forge. The prefix is per project:

```sh
agent-parley branch show
agent-parley branch set work
```

Changing the prefix renames nothing. Lanes created before the neutral scheme
keep the branch they were created with until they are retired, and the manifest
records which scheme each lane uses, so `participant merge`, `participant pr`
and drift detection keep working across the change. A branch name that already
exists in the repository only moves the ordinal on: nothing is renamed, reused
or deleted.

Attribution is refused everywhere a lane can publish text, on every repository,
for every provider, with no flag that turns it off:

- **Denied before it lands.** The same hook that refuses a branch switch inside
  an assigned lane denies `git commit`, `git commit --amend`, `git merge`,
  `git tag -m`, `git revert -m` and `gh pr create` whose message, title or body
  claims assistant authorship: a co-author trailer naming an assistant,
  "generated", "written", "created", "authored", "assisted" or "powered" by a
  named assistant, a vendor or model name in an authorship position, or a
  generator signature. Each denial names the enumerated rule it broke, lands in
  that lane's event log, and counts under `DENIALS` in `top`.
- **Refused at integration.** A hook is a tool-level check and a session can
  reach Git another way, so `participant merge`, `merge --preview` and
  `participant pr` scan every commit the lane would integrate — subject, body
  and trailers — and refuse with the offending commit named. `participant pr`
  refuses before it pushes, so a refusal leaves no remote branch behind. This
  is the backstop, and it has no skip flag in the same way the verification
  gate has none.
- **Clean output from the product itself.** The merge commit names the branch
  rather than the participant, and the comment a `ready` report posts on a
  claimed issue reports the result without naming the lane or its provider. The
  pull-request body carries the lane's own summary, evidence and issue
  references and no authorship claim.

Detection is textual and deliberately narrow: it matches authorship claims and
authorship positions, so "fix the codex adapter" is ordinary work while "written
by" an assistant is refused. A message supplied through a file rather than the
command line is not visible to the hook; the integration scan still reads the
commit it produced. Rewriting history you already have is out of scope: a
pre-existing commit carrying a trailer is reported at merge, never amended for
you.

`participant merge` integrates one lane's branch into the base checkout. It
always runs in the repository's main worktree, never inside another lane, and
always records a merge commit, so the integration stays visible in history. It
refuses while that participant has a running session, while the lane has left
its assigned branch, while the base checkout is dirty, already merging, or on a
detached HEAD, and while the lane holds uncommitted changes the branch does not
carry. On a conflict it names the conflicting paths and leaves the merge in
progress in the base checkout for you to finish with `git merge --continue` or
undo with `git merge --abort`; it never resolves a conflict, and never resets,
cleans, stashes, or force-switches. A successful merge leaves the lane and its
branch exactly as they were, so retiring the participant stays a separate step.

`participant merge --preview` answers what that merge would do without doing it:

```sh
agent-parley participant merge claude-1 --preview
```

`--preview` reports what that merge would bring in and what would refuse it,
and changes nothing: no merge commit, no branch movement, no working-tree or
index change, and no coordination state. It lists the commits the base does not
have, the files they change relative to the merge base, and every refusal
gathered into one report rather than raised one at a time. It never takes the
participant's session lock, so it is safe to run while that lane is still
working; a running session is read from the recorded session process and named
as a blocker. Because it attempts no merge, a preview that names no blocker
says only that nothing refuses the merge right now, not that the merge would
apply without conflicts. A preview prints no roster listing, unlike the actions
that change one.

A repository can require one verification command to pass before any merge:

```sh
agent-parley verify show
agent-parley verify set 'make check'
agent-parley verify set ''
```

`verify set` records that command in the repository's project manifest, beside
the roster and outside the target source tree, and `verify show` reports what is
required. A repository with nothing configured runs no gate and merges exactly
as before. With a command configured, `participant merge` runs it in the base
checkout before merging and refuses on a non-zero exit, reporting the exit
status and the last twenty lines of the command's combined output; a command
that cannot run is a refusal, not a skip. The command is stored as argument
tokens and run without a shell, so redirection, expansion and chaining cannot
ride into a gate, and no flag skips it. Removing it is an explicit
`verify set ''`. `--preview` never runs it, because executing a configured
command is a different decision from reading Git state. The gate reports the
base checkout as it stands before the merge, which is not a claim about the
merged result.

Four commands drive a lane's life from the base checkout, and every one of them
writes an event so `top` and `events export` show what the operator did and when.

`participant pause NAME` and `participant resume NAME` change whether a lane may
act. A paused lane keeps its session, its claims and its reservations: pausing is
not a handoff and releases nothing. Every served coordination call from that lane
is refused at the service boundary with an enumerated reason naming the operator,
the lifecycle hooks refuse tool use with that same reason, and `top` reports
`paused` in `STATE`. Resuming clears the flag and nothing else. Pausing a lane
that is already paused reports that and changes nothing.

`participant stop NAME` ends the session from outside its terminal. It delivers
one final operator notice, signals the recorded session process exactly as a
normal exit signals it, and waits a bounded time for it to leave. Identity is the
recorded process ID together with its kernel creation time, checked before
signalling and again inside the platform's terminate step, so a recycled process
ID is never signalled. The command-line check that recognizes the coordination
server does not apply here, because a lane runs a native client rather than this
package. Claims and reservations stay owned and the command prints what the lane
still holds, so an operator moves that work deliberately. A stop that finds no
running session is still recorded.

`participant restart NAME` starts a lane again. It refuses while a session is
alive, because two clients in one worktree would fight over it. It refuses a
dirty worktree and names the paths, and it refuses a lane that is not on its
assigned branch: nothing here resets, cleans, stashes or force-switches. It
replays the recorded lane initialization command when one exists, then launches
the same provider and credential profile as the previous run.

These are command-line actions only. No MCP tool exposes them, so a participant
cannot pause, stop or restart itself or a peer.

A repository can also record one command that prepares every new lane:

```sh
agent-parley init show
agent-parley init set 'uv sync --locked'
agent-parley init set ''
```

`init set` records that command in the same project manifest, outside the target
source tree, and `init show` reports it. A repository with nothing configured
hands the native CLI a bare worktree exactly as before. With a command
configured, the launcher runs it in the new worktree after `git worktree add`
and before the native CLI starts, so dependencies, an untracked environment
file or a warmed build are in place for the agent's first turn rather than
costing it several. `participant add` runs it on the same path, because both
create a lane through the same step.

The command is stored as argument tokens and run without a shell, like the
verification gate, and no flag skips it. It runs only at lane creation, never on
a resume, so a resumed session does not repeat setup. `AGENT_PARLEY_BASE` names
the base checkout while it runs, which is how a command copies a file Git does
not track. A non-zero exit refuses the launch and reports the exit status with
the last twenty lines of the combined output; a command that cannot run at all is
a refusal, not a skip. The worktree is left in place in both cases, because an
operator needs to inspect what the command did before it failed. The participant
is not registered in the roster, so correcting the command and rerunning starts
from the same point.

`participant pr` pushes one lane's bridge branch to `origin` and opens a pull
request for it. The body is the lane's own recorded report: its summary, its
verification evidence and its remaining work, under the three headings the
repository's pull-request template asks for, followed by an explicit `Refs #N`
for every issue the lane still claims. The title is the first commit the lane
added, so it already follows the target repository's commit rules, and the base
is the branch the main checkout has out. Authentication is the native `gh` CLI's
own: Agent Parley stores no token and adds no flag that bypasses a repository
rule. It refuses when the repository has no project, when the participant is
unknown, when no report is on file, when the lane claims no issue, when the
branch adds no commits to the project base, and when the main checkout is on a
detached HEAD or on the lane's own branch. A pull request already open for that
branch is reported rather than duplicated, and its branch is still updated by
the push. Pushing to `origin` happens only here; `status` never reaches a
remote.

Your native GitHub account becomes the assignee. Labels and milestone are read
from claimed issues before pushing. Unlabelled issues are supported; a project
can require classification explicitly. The private `project.json` accepts a
`pull_request` object with these optional settings:

```json
{
  "pull_request": {
    "change_type_labels": ["bug", "enhancement"],
    "require_label": false,
    "milestone": "match",
    "body_template": "## Review checklist\n\n- [ ] Reviewed"
  }
}
```

The default label vocabulary remains the shipped eight change types.
`milestone` accepts `match` (reject conflicting milestones), `required` (every
issue must also have one), or `ignore`. With no configured body template, the
launcher reads the target repository's PR template when present. The report and
issue references are always included; optional `$summary`, `$evidence`,
`$remaining`, `$outcome` and `$issues` placeholders are expanded as text.

The PR also carries independently recorded evidence: the configured command is
executed in the lane, denials are counted by reason within the current claim
window, and advisory reservation history and recorded conflicts are summarized.
A failed gate, active session, dirty lane or changed commit refuses integration.
No gate and no recorded denials are stated explicitly. A JSON evidence snapshot
and a JSON Lines event slice are written beside the lane in private project
state. The body names the slice and its SHA-256 digest; it is not uploaded or
committed automatically. Counts cover retained observations, so missing or expired
records cannot prove that no event occurred. Older logs did not distinguish
reservation conflicts from successful calls.

`participant retire` removes one lane: it refuses while a session is running or
the worktree is dirty, removes the worktree, invalidates that participant's
coordination credential, and drops its manifest entry. The branch is deleted
only when it adds no commits to the project base; otherwise the branch is kept
and named in the output. Message history is always preserved, so past handoffs
still resolve their sender.

Mutations and reports run from the assigned lane. The owner pauses offered work
until acceptance, decline, or cancellation. Use `issue decline`, `issue cancel`,
and `issue release` explicitly; release does not close a GitHub issue. Partial or
blocked reports require `--remaining` instead of `--evidence`.

`up` starts the detached service; `down` stops its verified process and retains
state. Default state is `~/.local/state/agent-parley`, mode 0700. Logs are in
`server.log`. Set `AGENT_PARLEY_HOME` or pass `--home` for another private root.
Set `AGENT_PARLEY_PORT` before first initialization to override port 8876.

Worktrees start at a captured commit and persist. Ignored environment files,
dependencies and untracked configuration are not copied. Set up each worktree
as needed. Review each lane's branch, integrate it with `participant merge` or
with Git directly, then run the target repository's combined verification gate.
Merging is never automatic, and a reported `ready` outcome does not establish
that a branch is fit to merge.

## Plugins

Install the CLI first, then add the repository marketplace:

```sh
claude plugin marketplace add suneel944/agent-parley
claude plugin install agent-parley@agent-parley
codex plugin marketplace add suneel944/agent-parley
codex plugin add agent-parley@agent-parley
```

Both plugins provide `coordinate`; the launcher supplies MCP and native hooks.
Avoid installing the same skill from both personal and repo marketplaces. Start
a new native session after updates.

Repository installation does not imply public directory approval. For Claude,
validate `plugins/agent-parley` with `claude plugin validate`, then use the
[community submission form](https://platform.claude.com/plugins/submit).
The official catalog is curated separately; see
[Claude's guide](https://code.claude.com/docs/en/plugins).

For Codex, follow [OpenAI's submission guide](https://developers.openai.com/plugins/deploy/submission).
This is a skills-only plugin. Submission requires a verified publisher, listing
and policy URLs, a skill bundle, and review cases. `make codex-bundle` builds
the skill bundle the portal accepts. The Codex listing is live; the Claude
submission is awaiting review.
CI builds artifacts; it does not submit review forms.

`docs/catalog-submission.md` records what each catalog asks for, the checks that
can be run in this repository before submitting, and the steps that are bound to
the owner's accounts and cannot be delegated.

## Releases

A release measures itself, then ships itself. Every push to `main` runs
`Auto version`, whose first step measures release eligibility from delivered
product work and reports the counts it measured. A push that warrants no
version stops there, and so does a push whose changes never reach the package.
The release commit is skipped by its `chore(main): release` subject, so a
release cannot trigger another release.

An eligible push mints a token for the release GitHub App, raises the next
version across `pyproject.toml`, both plugin manifests, the Claude marketplace
manifest, `uv.lock` and `.release-manifest.json`, and prepends a `CHANGELOG.md`
entry built from the same commits the measurement counted. It then runs the
policy gate against the raised markers, so a marker the bump misses fails the
run rather than reaching a release. The App commits that to `main` and pushes
the annotated `vVERSION` tag. The job's repository `GITHUB_TOKEN`, granted
`actions: write`, then dispatches `Release` for that tag.

There is no release pull request. An earlier design kept one, and it did not
prevent the incident it appeared to guard: a build-tooling commit with a `fix:`
title produced a proposal, the workflow approved it as a second identity,
auto-merge fired, and a withdrawn version reached PyPI seconds after the
resulting tag push. What stops that today is the measurement. A commit counts
only when a releasing conventional type introduces it and it touches
`agent_parley/` or `plugins/agent-parley/`, and a version is raised only when
the counted work crosses a threshold and the package differs from the approved
release. Tooling commits measure zero, so the proposal that started the
incident cannot exist. `check`, `secrets` and `pr-hygiene` stay required for
human pull requests, and independent approval still applies to them; the App
bypasses review only for the release commit it authors, and cannot approve or
merge anything.

If `main` advances between the measurement and the push, the push is rejected
and the run fails rather than tagging a tree nothing measured. The next
eligible push measures again and succeeds, so the failure costs a run, not a
release.

Publication is a separate `Release` run naming an existing tag. It reruns the
gate, creates a draft, uploads assets, downloads and verifies their checksums,
then publishes. Failed verification leaves a draft.
After uploading to PyPI, verification allows six retries ten seconds apart
for missing index metadata to become visible. Conflicting or yanked files
still fail immediately; missing files after the retries leave the draft intact.

The release workflow answers only to `workflow_dispatch`. It deliberately has
no tag trigger: publication must name a tag explicitly, so a tag pushed by any
other route publishes nothing. `Auto version` dispatches it by name after
pushing the tag it measured. Publishing an existing tag
is idempotent, so a rerun verifies the uploaded bytes again instead of failing.

Release notes start with `What's Changed`, contain only the selected version's
section from `CHANGELOG.md`, and end with its full changelog link. A standing
product description is not injected into the changes. Regression tests cover
version isolation, exclusion of changelog preambles, missing or empty entries,
and preservation of the full changelog link.

The bump step generates a version section from the product commits counted by
release eligibility. Use descriptive Conventional Commit titles that name the
user-visible change. Maintenance and tooling cannot raise a release version.
An editorial correction can expand terse entries into concrete behavior after
checking the shipped commits and linked issues. Keep section names consistent:
`Features`, `Bug fixes`, `Performance`, `Documentation`, and `Known limitations`
when applicable. Preserve historical archive and provenance notices.

Keep reviewed wording in `CHANGELOG.md`. Updating a published GitHub page is an
editorial operation: change its description only, never replace the tagged
source, packaged notes, release assets or checksum manifest. Packaged notes
remain the record created when that version was built.

Every GitHub release is titled `Agent Parley vVERSION`, minted by publication
rather than typed, so the releases page reads as one series. Releases published
before that title existed were renamed to match. Do not retitle a release by
hand: a page where one entry names the product and another shows a bare tag
reads as two different projects.

One-time owner setup, without which `Auto version` fails as soon as a push
warrants a version:
register a GitHub App under the owner account with repository permissions
Contents: read and write; install it on
`suneel944/agent-parley`; set the repository variable `RELEASE_BOT_APP_ID` to
the App ID; set the repository secret `RELEASE_BOT_PRIVATE_KEY` to a generated
private key in full PEM form; and add the App to the bypass actors of the
`main` PR/check and independent-approval rulesets so it can push the release
commit. Keep deletion, force-push and linear-history rules in a separate
ruleset with no bypass actors. Classic branch protection must be migrated to
equivalent active rulesets before removing it; otherwise it still rejects
the release App's push. Human pull requests retain their required checks.
The App's Contents permission covers the commit and tag. Dispatch uses the
job's repository token, so the App needs no Actions permission. GitHub permits
`workflow_dispatch` events from `GITHUB_TOKEN` to start another workflow.

To cut a release by hand in an emergency, `Release` still accepts a
`workflow_dispatch` with an existing tag, and reruns the same verification.

After the GitHub draft's uploaded bytes have been verified against the local
checksums, the workflow publishes the distribution to PyPI. It
stages a clean `dist/pypi` directory holding only the two files the index
accepts, the `agent_parley` wheel and the source tarball, copied by exact name
from the verified `dist/release` bundle, so the plugin archive, the exported
requirements file, the changelog, the release notes and the checksum manifest
are never uploaded. Authentication uses PyPI Trusted Publishing over OIDC: the
job requests a short-lived identity token through an `id-token: write`
permission scoped to that job, and PyPI exchanges it for a one-time upload
token, so the repository stores no PyPI API token and no publishing secret. The
workflow must run as its own top-level workflow, started by its own dispatch
rather than called from `Auto version`. This is why `Auto version` finishes by
dispatching `Release` instead of calling it as a reusable workflow, even though
calling it would be shorter. The upload carries a signed
attestation whose build configuration names the workflow that started the run,
and PyPI checks that name against the trusted publisher: called from another
workflow, the attestation names the caller, the check fails, and the upload is
rejected with `400 Bad Request`. The current workflow keeps the GitHub release
as a draft until both PyPI files are visible with matching hashes, then marks
it public and latest. Rerunning against an existing tag verifies the existing
files and stages only missing packages; conflicting or yanked files fail.
Publication does not close issues or milestones. After verification, move
unfinished issues to the next milestone and close the released milestone.

Publishing needed one manual step that only the repository owner could take,
and it is done. The `agent-parley` project exists on PyPI and its trusted
publisher is this repository's `release.yml`, with the environment name left
empty because the workflow declares no environment. It had to be registered as
a *pending* publisher, because a trusted publisher cannot be added to a project
that has no releases, and the first successful run created the project and
converted it into a normal one. Nothing was added to repository secrets.

Registering another one repeats the same path: PyPI, account settings,
Publishing, "Add a new pending publisher", GitHub, with the PyPI project name,
owner `suneel944`, repository `agent-parley` and workflow `release.yml`. A
pending publisher does not reserve the name until it is first used, so register
it before tagging. The earlier candidate `agent-bridge` is permanently
unavailable: PyPI strips separators when comparing names, and an unrelated
`agentbridge` project already holds that form.

Download assets together and run `sha256sum --check SHA256SUMS`. The wheel needs
no third-party runtime packages. Development tools and the independent MCP test
client are locked in `uv.lock`.
