# Architecture and contracts

Agent Parley runs on one Linux host under one OS user. It coordinates participating
agents; it does not execute model requests, enforce filesystem permissions, replace
native approvals, or merge work.

## Responsibilities

| Module | Responsibility |
| --- | --- |
| `cli` | Git worktrees, native configuration, launch, status and reports |
| `server` | Authenticated MCP transport and bounded tool contracts |
| `store` | SQLite schema, migration, scoped mail, atomic leases and tool events |
| `process` | Linux process identity, session liveness and pidfd shutdown |
| `issues` | Claim and handoff state transitions |
| `roster` | Providers, credential profiles and project participants |
| `forge` | Optional best-effort issue lookups and mirrors on the host forge |
| `checkpoints` | Lifecycle observations and bounded context delivery |
| `dashboard` | Read-only live operator view of every participant |
| `state` | Private atomic JSON publication and operation locks |

Enforcement and telemetry share one substrate, on purpose, in two places. Hook
decisions are appended per participant beside the lane state, because a hook
blocks its agent and must never wait on the coordination store's write lock.
Served MCP calls are recorded in the store's `events` table inside the very
transaction that carries the call's own effect, so an event exists exactly when
the effect it describes was committed. A rejected call rolls that transaction
back, and a read-only call holds no write lock, so both record afterwards in
their own short transaction; a busy store loses the record rather than the
call. Retention is bounded to the most recent 2000 events per project, and
telemetry never decides an outcome.

The service is a singleton **per private state directory**. An exclusive startup
lock serializes launch and shutdown; the loopback port prevents a second listener.
Independent state directories remain independent. No global mutable application
singleton is needed. State paths and authenticated actors are explicit inputs,
which keeps transactions testable without HTTP.

Use abstractions for actual boundaries. Do not add factories, interfaces, or
inheritance solely to name a pattern. The HTTP server implements its standard-
library base contracts; the type gate checks method overrides.

## Authentication and protocol

The launcher registers lanes locally. Registration is not exposed over MCP.
A bearer token identifies exactly one project and agent. The database stores its
SHA-256 digest; private identity files retain the credential for restart. Tool
arguments cannot select another project or impersonate an agent. A separate
health token grants no tool access. Credentials travel through the native
client's environment, never the model prompt.

The server binds `127.0.0.1`, checks Host and Origin, rejects unauthenticated
requests, and avoids credential/body logging. It supports stateless JSON responses
over MCP Streamable HTTP, not SSE sessions or remote hosting. The independent
official MCP SDK exercises initialization and calls in CI.

Seven tools cover sending, fetching, acknowledging, marking read, reserving
files, releasing reservations, and listing participants. Unknown arguments fail. Sends require an idempotency
key: identical retries return the original message ID; changed retries fail.
Fetching never marks a message read or acknowledges it.

No coordination tool returns a participant's whole starting context. The store
holds projects, agents, messages, recipients, reservations and events, keyed by
an authenticated project and lane. The issue ledger and the participant
manifest are files in the project state directory, whose name derives from the
repository's Git common directory, which the store never records. A served
call carries no repository path, and the detached service must not run Git, so
reaching that directory would need a new schema column, a launch-time map that
goes stale as projects are added, or a scan of every project's manifest. None
of those buys a capability. Dependency edges live in the same ledger file for
the same reason: `issues.json` already records ownership, offers and history
per issue, and the checkpoint that reports a revision change reports the edges
with it, so dependency notification costs no new substrate and no new tool.
Checkpoints already deliver the roster, the issue
ledger and message previews at session start and again on any prompt submit or
pre-tool-use whose state changed; `list_participants` returns the roster on
demand; and `agent-parley issue list`, run from a participant's own worktree,
re-reads the ledger from the one process that can resolve the directory. The
store therefore stays free of repository paths, and refreshing context stays a
checkpoint and CLI concern rather than a coordination tool.

## Participants, providers and accounts

A project holds a roster of participants. A participant is one lane: its own
worktree, bridge branch, registration credential, activity file, event log and
session lock. Its name addresses it in handoffs and message recipients, and it
also becomes a directory and a branch component, so the validator accepts only
lowercase letters, digits, hyphens and underscores. Dots are refused: a lane
named after a peer's state file would otherwise shadow that file. Adding a
participant creates only that lane and never touches existing lanes or branches.

The activity file holds last state only; the event log,
`<participant>-events.jsonl`, appends one record per observed hook event with an
enumerated reason class and the decision it produced, so denials are retained
rather than overwritten. Hooks block the agent, so the log stays a plain
append-only file in the project state directory, outside the coordination store
and its write lock, and it rotates to `<participant>-events.1.jsonl` at a fixed
byte cap. A reader summarizes the rotated file and then the current one, oldest
record first, so a report covers everything still retained rather than the
current file alone. Retention is bounded twice: two files, because the rotation
that creates a new one discards the older, and a maximum record age. Age
retention rewrites the log, which costs a full read and write, so it runs only
at a session boundary and never on the blocking path a hook takes before a tool
call. A lane that never reaches a session boundary is still bounded by the byte
cap. Each file is replaced atomically, and a failed rewrite leaves the log
exactly as it was. A log failure never changes an enforcement outcome.

Retained records are readable as a whole or over a window. A window filters on
the recorded time, so it reports a period rather than a file, and a record
carrying no time is never counted inside one. Export writes the retained
records as JSON Lines, one record per line, each naming the participant that
produced it, so enforcement history leaves the state directory in the shape it
was stored in rather than a rendered summary.

A lane's session state follows a recorded session process identity, matched by
process ID and Linux creation ticks, never its session lock. The launcher holds
that lock for the whole session, so probing it would make a concurrent launch
fail while merely reporting. A session that ends without clearing its record
reads as stopped, because its process is gone.

A provider states which native CLI drives a participant and how that CLI reaches
a model. Coordination needs MCP server configuration and lifecycle hooks, which
the `claude` and `codex` CLIs supply, so every provider names one of those two
adapters. Providers for other vendors reuse an adapter and change the endpoint
through environment variables. The `deepseek`, `kimi` and `grok` presets carry no
endpoint; `agent-parley provider add` defines further providers locally.

A credential profile selects one account by pointing the CLI's config-home
variable at a separate directory, so the same provider can run twice under
different logins. Profiles record directories, plain variable values and required
variable names. Values whose names look like credentials are rejected, and
required variables are read from the caller's environment at launch, so no
credential value enters bridge state. Sign-in inside each config home remains the
native CLI's own action.

Registering a project reads committed HEAD, so pending base-checkout work would
never reach a lane. Rather than refusing, `setup`, `participant add` and `run`
stash that work with `git stash push --include-untracked` when they create the
project manifest, then report the entry on standard error. One stash stack is
shared by every worktree of a repository, so the entry carries a unique message
and is restored by name with `git stash apply <entry>` rather than by position.
Nothing is reset, cleaned or force-switched, and a checkout Git cannot fully
stash still refuses. Later registrations read the existing manifest and never
touch the checkout.

Branch verification is scoped to the lane an operation touches, so a lane left
on the wrong branch blocks only its own participant. `status` reports every
lane's actual branch. `participant restore` returns one lane to its branch and
`participant retire` removes one lane; both refuse while that participant holds
its session lock or its worktree is dirty, and neither resets, cleans, stashes,
or force-switches. Retiring invalidates that participant's credential and keeps
its branch whenever the branch holds commits the project base does not.

`participant merge` integrates one lane's branch into the base checkout. It runs
in the common repository root, never inside another lane, and always records a
merge commit, so an integration is auditable rather than replayed as a fast
forward. It refuses on a drifted lane, on a running session, on a dirty base
checkout, on uncommitted lane changes the branch does not carry, and on a base
checkout that is already merging or on a detached HEAD. A conflict is left in
the working tree with the conflicting paths named and both `git merge --continue`
and `git merge --abort` reported; Agent Parley never resolves a conflict, and
never resets, cleans, stashes or force-switches. Merging leaves the lane and its
branch untouched, so retiring stays a separate decision.

`participant merge --preview` answers the same question without acting. It
reports the commits `HEAD..branch`, the files they change relative to the merge
base, and every refusal above at once instead of one at a time, so a single
report names everything to fix. Merge and preview share one generator of
refusal conditions, so their wording cannot drift; the merge raises the first
condition it meets, the preview collects them all. The preview writes nothing
and takes no session lock, because previewing a lane while its agent works is
the ordinary case and that lock belongs to the session; a running session is
read from the recorded session process, as liveness reporting reads it. It
performs no trial merge, so it reports refusals, never conflicts. The preview
reads Git state only and never runs the verification command below, because
executing a configured command is not a read.

A repository can require one verification command to pass before any merge.
Executing a configured command is a different trust decision from reading Git
state, so the gate is a separate step at the merge entry point rather than one
more Git refusal, and `agent-parley verify` is the only thing that records it.
It lives in that repository's project manifest, beside the roster and the
project base, because the manifest is already the per-repository configuration
and it already sits outside the target source tree; no new file and no
in-repository file is introduced. A repository with no command configured runs
no gate and merges exactly as before. The command is stored as argument tokens
and run without a shell, so redirection, expansion and chaining cannot ride
into a gate, and it runs in the common repository root while the merge already
holds the project setup lock and that participant's session lock, so the lane
cannot start and the roster cannot change underneath it. A non-zero exit
refuses the merge, reporting the exit status and the last twenty lines of the
command's combined output; a command that cannot run is a refusal, not a skip.
No flag bypasses the gate, and removing it is an explicit `verify set ''`.
The gate reports the base checkout as it stands before the merge, which is not
a claim about the merged result, so combined post-merge verification remains a
separate reviewed action.

`participant pr` pushes one lane's branch to `origin` and opens a pull request
carrying that lane's recorded report. It is the only command that writes to the
repository's remote; the forge mirrors above touch issues, never Git, and
reading status stays local. It reads the report from lane activity, the claimed
issues from the issue
ledger, the title from the first commit the lane added, and the base from the
branch the common repository root has checked out. Authentication is the native
`gh` CLI's own, so repository permissions and rules apply unchanged and no token
enters bridge state. The generated body carries the three headings of
`.github/PULL_REQUEST_TEMPLATE.md` and an explicit reference to every claimed
issue, which is what the repository's pull-request hygiene gate requires of a
body. It refuses on an unknown participant, a missing report, an unclaimed lane,
a branch that adds no commits to the project base, and a base checkout on a
detached HEAD or on the lane's own branch. An open pull request for the branch
is reported instead of replaced by a second one.

Manifests written by the earlier two-lane layout upgrade on first read. Migrated
lanes keep their branches and registered identities, so existing mail, claims and
reservations continue to resolve.

## Persistence and concurrency

Mail uses SQLite WAL with indexed inbox and active-lease queries. Each write
acquires an immediate transaction, validates and mutates, then commits once.
Connections close after every operation. A writer waits up to one second for
another transaction to commit.
Conflicting reservation batches grant no paths. Each conflict names the blocking
lane and that lane's declared reason, clipped to 80 characters, so a denied
caller can judge the overlap without a further call. Conflicts are reported
until the serialized result reaches its budget, and `has_more` states that
further conflicts exist beyond the reported ones. Every lease records its
creation time, so an operator can see lease age rather than expiry alone.
Directory overlaps are detected;
two globs conservatively conflict when either lease is exclusive. Use exact paths
when disjoint globs would otherwise be rejected. Renewals replace the owner's
previous lease atomically. Expired or released leases no longer block work.

Issue mutations use a repository-scoped lock and atomic JSON replacement. Only
the owner can offer work; only the named recipient can accept the current offer
ID. Cancellation invalidates that ID. No timeout or process exit transfers
ownership. Reported `ready` outcomes do not establish verified completion.
Ownership listings report each owner's session state and the age of its last
observed checkpoint. That report is for an operator; silence, an idle session
and a stopped session all leave ownership where it is. `agent-parley top`
renders the same state continuously, adding branch drift, denial counts and
served calls; it reads state and never writes it.

A claim additionally attempts one read-only forge lookup for the issue title,
using the repository's own `origin` remote and the operator's already
authenticated `gh` client. The lookup is best effort: no GitHub remote, no
`gh`, no network, a refused request or unusable output all resolve to no title,
and the claim proceeds unchanged. A recorded title is peer-supplied display
context, clipped to 200 characters; it is never authority for ownership, and
every transition, dependency and handoff rule behaves identically with or
without it.

The ledger is then mirrored back. A completed claim adds the operator's own
forge account as the issue's assignee and a release removes it, so an issue
being worked is visible to a reader who never opens Agent Parley. A lane that
newly reports the ready state posts its recorded summary and evidence as one
comment on every issue it claims, once per arrival at that state rather than on
every repeated report. Every mirror runs after the local write, through the
operator's own `gh` client, with no flag that bypasses a repository rule, and
reports failure instead of raising: an unreachable, unauthenticated or
unwilling forge leaves the transition and the report exactly as recorded.

The forge sees one assignee, the operator's account, because every lane runs
under it. A handoff between participants therefore moves the ledger owner
without moving the forge assignee, and change-type labels are never written at
all: classification is the repository's own decision and a lane does not make
it.

Shutdown verifies the module, state path and process creation ticks, then pins
the process with Linux pidfd before signaling. It does not kill arbitrary PIDs.

## Context and resource budgets

| Boundary | Limit |
| --- | --- |
| HTTP request | 16,384 bytes |
| Concurrent workers / socket timeout | 16 / 3 seconds |
| New message body | 4,096 UTF-8 bytes |
| Inbox page | Up to 5 messages; bodies omitted by default |
| Body page | Up to 1,024 Unicode characters |
| Serialized inbox or conflict result | At most 8,192 UTF-8 bytes |
| Hook preview batch | Up to 3 messages |
| Injected notice | At most 1,536 UTF-8 bytes |
| Message recipients | 1–16 per send |
| Participants per project | At most 32 |
| Roster listing | At most 32 participants |
| Active reservations | At most 128 per lane |
| Reservation lifetime | 30–3,600 seconds |
| Reservation reason | 160 bytes stored; 80 characters reported on conflict |
| Participant event log | Rotated at 262,144 bytes; one rotated file retained |
| Participant event age | 1,209,600 seconds, applied at a session boundary |
| Retained tool events | 2,000 per project |

Inbox pages return `next_after_id` and `has_more`. For `next_body_offset`, refetch
with `after_id=message_id-1`, `limit=1`, and that `body_offset` before advancing.
Stored legacy text is not discarded to satisfy response budgets.

Hooks read local state without network requests or model calls. They reject
branch-changing commands in assigned lanes, detect branch drift after any bypass,
and block normal completion until the manifest-owned branch is restored. Session
cursors prevent duplicate delivery; issue revisions suppress unchanged reminders.
A changed roster is announced once so a joining participant stays addressable;
that announcement never denies a tool call and never blocks completion, because
no peer addressed it to anyone.
New sessions receive a bounded briefing of still-unreviewed mail. Stop retries do
not create continuation loops. Failed observations never request replay of an
already completed action. Coordination errors before edits pause work.

Status reports notice counts and injected UTF-8 bytes, not tokenizer counts or
API billing. Deterministic tests enforce context budgets. `make benchmark`
measures latency separately; results depend on hardware and workload.

## Migration and verification limits

Version 0.3 creates `bridge.sqlite3` and imports `mail.sqlite3` through a consistent
read-only snapshot. IDs, acknowledgements and leases are retained; the original
remains unchanged. Imported rows and the schema version commit together.
Existing identities are rebound locally at launch. Stop old services and sessions
before upgrading; no live workspace is automatically migrated or terminated.

A store written by an earlier 0.3 release upgrades in place on first use: the
event table is created and reservations gain a creation time, which existing
leases date from the upgrade. No coordination row is rewritten, and the schema
version publishes in the same transaction as the change it describes. A store
written by a newer schema is refused rather than downgraded.

CI covers temporary Git repositories, independent MCP clients, concurrent calls,
authorization failures, persistence, resource budgets and isolated wheel installs.
It does not demonstrate compliance by a paid live model session.
