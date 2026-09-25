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
every message, claim and lease. Issue ledgers upgrade lazily: 0.11 derives
lifecycle defaults for older records without an `execution` field and persists
them when a lifecycle transition next writes the record. Stop running sessions
before upgrading.

After 0.11 records a verified completion or recovery, do not run 0.10 against
that state directory. Version 0.10 does not understand those lifecycle and
recovery records; in particular, it can read an ownerless completed issue as
unclaimed and offer it again. Keep that state directory on 0.11 or newer.

## Platforms

Linux and macOS are supported. WSL2 is supported for repositories that live in
the Linux file system, such as under `$HOME`. `run` refuses a repository under
`/mnt/`: Git worktrees and the coordination locks do not hold on a mounted
Windows drive, so the repository must be cloned into the Linux file system
first. Native CLIs installed on the Windows side are out of scope: a lane is a
Linux process driving a Linux install, and the loopback mail server is not
reachable across the boundary. `doctor` names the kernel release, the WSL
generation (`1`, `2` or `none`) and whether `pidfd_open` is available, so a
platform gap is read before a lane starts. CI runs the behaviour suite inside
WSL on a Windows runner as an advisory job; it never blocks a merge.

## Daily use

```sh
agent-parley status
agent-parley top
agent-parley participant list
agent-parley issue list
agent-parley issue next
agent-parley issue claim 42
agent-parley issue offer 42 --to codex --summary "commit, checks, remaining work"
agent-parley issue accept 42 --offer-id CURRENT_OFFER_ID
agent-parley issue assign 42 codex --reason "Codex already read that parser"
agent-parley issue block 42 --on 17
agent-parley issue unblock 42 --on 17
agent-parley report --state ready --summary "Result" --evidence "Checks and results"
agent-parley say codex "Rebase onto main before you open the pull request."
agent-parley mail thread THREAD_ID
agent-parley mail search "reservation conflict"
```

`issue offer --to` names another participant in the same project.

### Directing work to a lane

`issue assign NUMBER NAME` hands a lane work without typing into its terminal.
It runs from any checkout as `operator`, and it offers rather than takes: what
it records is an offer, so ownership still moves only when a lane accepts one.

An unclaimed issue is offered to the named lane directly, with an offer
identifier the lane quotes to `issue accept` or `issue decline` like any peer
offer. An issue another lane holds is not taken from it: the command records a
request to that owner instead, mails the owner the same identifier, and the
owner's `issue accept NUMBER --offer-id ID` is what creates the offer to the
named lane. The command prints which of the two it recorded, and the owner
keeps the issue throughout either way.

`--reason "text"` travels with the offer, is stored on the record, and is what
`history issue NUMBER` reports beside the transition, so a later reader sees why
the work moved. `issue list` and `status` show a pending offer with `operator`
as its source, an unclaimed issue appearing for as long as an offer waits on it.

`issue assign NUMBER --unassign` withdraws an operator offer or request that
nobody has answered. An offer that was already accepted is refused, naming the
lane that holds the issue, because only that lane can hand it on.

### Reading status

`status` prints the server line, the code line, the state directory, and then
one table per
project with a row per participant: `PARTICIPANT`, `PROVIDER`, `ACCOUNT`,
`SESSION`, `BRANCH` with `!` when the lane left its assigned branch, `OUTCOME`,
`ISSUES` held with `!` on an issue past its deadline or attempt budget and `+N`
for offers waiting on that lane, `MAIL` as unread over pending acknowledgement,
`LEASES` held with `!` and the stale count, `REPORTED` as the age of the last
report, and `TASK`. A mailbox that could not be read prints `?` rather than a
zero. Column widths follow the widest value and then the terminal, by the rule
`top` uses: the least useful column is dropped first, the dropped headings are
named under the table, and `TASK` takes whatever width is left. A pipe or a
file receives the whole table, because no width is imposed on a stream that is
not a terminal.

The code line reports the build the running service is serving against the code
installed here. `Code: ok` says they match; `Code: stale` says the service is up
and answering but was started from an older build, so the frame is read through
that build until `agent-parley down && agent-parley up` restarts it. A service
running behind the checkout is therefore visible in the reading rather than
reported as ready, and `doctor` reports the same comparison as its `service`
component.

`SESSION` carries the lane's presence, which has four states. `active` is a
lane that served a coordination call inside the configured interval. `idle` is a
live session process that served none inside the inactivity threshold: it is a
quiet lane, not a lost one. `stopped` is a lane whose recorded session
process is gone. `unknown` means no trustworthy native process identity was
recorded, so the service cannot distinguish a live manual session from one that
was killed. A live lane past the threshold therefore never reads `stopped`, and
presence only reports: no claim is released and no ownership moves on any of
the four. A send result keeps the older operator wording for the dead case and
summarises a message to a `stopped` lane as `queued for NAME (unreachable)`.

Appending a participant name reports that lane as the whole reading —
availability, drift, waiting items, claims, reported outcome, mail counters and
the latest prompt — instead of as a row. Filters combine, and a lane is
reported only when it satisfies all of them:

```sh
agent-parley status --project /path/to/repo
agent-parley status --provider codex --outcome blocked
agent-parley status --drifted
agent-parley status --pending
agent-parley status --idle --since 45m
agent-parley status --over-budget
agent-parley status --issue 42
```

`--pending` reports a lane holding unread mail, an unanswered acknowledgement,
an offer, or a reservation past its declared time to live. `--idle` reports a
live lane that served no coordination call inside `--since`, or inside the
project's configured interval when no window is given — the lanes reading
`idle`, never the stopped ones; it measures
coordination inactivity, not what a native client was doing inside a turn.
`--over-budget` reports the lanes over any of their advisory token, call or
hour limits and exits non-zero when one matches; the budget informs and does
not gate. `--issue` reports the lanes that hold or are offered one issue. A selection
that matches nothing prints one line naming the filters that were applied.

`--drifted` and `--pending` exit non-zero when at least one lane matches, so a
shell gate fails on drift or on unfinished coordination without parsing text.
Every other reading exits zero. `--json` prints the same document as an
unfiltered `status --json`, holding only the matching rows.

`status` stays read-only: it takes no lock, calls no vendor and writes nothing.

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

`--as NAME` names the lane to read instead of taking it from the worktree, so
`mail show`, `mail thread`, `mail search` and `mail list` answer from the main
checkout. It exists because `problems` cites message identifiers from the main
checkout that the operator then has to open, and changing directory into a lane
worktree to read one is the wrong price. The flag selects a reader and nothing
more: no message is sent, acknowledged or marked read for that lane, and the
write verbs keep the lane identity path they always had. A name that is not a
participant of this project is refused.

`issue list` prints each owner's session state and the age of its last
checkpoint, so a quiet lane is visible. Reclaiming that work still needs the
owner to release it, or an explicit offer and accept.

A pending offer is listed with the state a peer needs to take the work over: the
offering lane's head commit, the reservations it holds for that issue, and the
remaining work from its last report. `status --json` carries the same three
fields on the offer record. Accepting moves those reservations to the acceptor
with the issue, so the paths the work needs are not reserved twice and a
released offer strands nothing. Mail does not move: an acknowledgement stays
owed by the lane that received the message.

`issue block NUMBER --on OTHER` records that one issue waits on another. Only
the current owner of NUMBER can add a dependency, and an issue records at most
ten. The blocker must already be in the issue ledger; an edge to a complete
issue is not recorded, and an edge that would close a dependency cycle is
refused. `issue list` then names the participant holding each blocking issue,
or reports it unclaimed. Every ledger change bumps the revision, so the next
checkpoint delivers the updated dependency line to each running lane without
polling. Dependencies survive release and reclaim.

A dependency is information, not a gate. Nothing prevents work on a waiting
issue. Verified completion of the blocking issue drops the edge, and every
supervisor poll also drops any edge whose blocker is complete or no longer in
the ledger, whether or not anybody owns the waiting issue. The owner runs
`issue unblock` to drop an edge early. The operator runs the same command from
the project base checkout, which works on a released issue with no owner.

### Compatibility and `doctor`

Three numbers move independently: the launcher's package version, the wire
protocol a hook or served call speaks, and the store schema on disk. A fourth
reading, the build the running service is serving, moves with none of them,
because a service keeps serving the code it started from. Several
package versions normally share one protocol, so equal package versions are not
the only compatible combination.

<!-- compatibility:start -->
| Launcher | Wire protocol | Store schema |
| --- | --- | --- |
| 0.13.0 | 1 | 11 |
| 0.12.0 | 1 | 10 |
| 0.11.0 | 1 | 10 |
| 0.10.0 | 1 | 9 |
| 0.9.1 | 1 | 9 |
| 0.9.0 | 1 | 8 |
| 0.8.0 | 1 | 8 |
| 0.7.0 | 1 | 8 |
| 0.6.0 | 1 | 8 |
<!-- compatibility:end -->

The release path rewrites that table from the constants in
`agent_parley/protocol.py` and `agent_parley/store.py`, so it cannot drift from
the numbers the code uses.

```sh
agent-parley doctor
agent-parley doctor --json
```

The plugin lines read the manifest of each client from wherever the plugin tree
was installed. A wheel carries that tree inside the package directory, at
`agent_parley/plugins/agent-parley`, because a wheel is unpacked into an
environment that holds nothing else of this project. A checkout keeps it at the
top level, where the client directories read it. Both layouts therefore report
the same protocol.

The launcher version reported here is the version of the code that is running.
An editable install, which `make install-dev` creates, records its version once
at install time and keeps reporting it after the checkout has moved on, so the
project file beside the package is read wherever it exists and installation
metadata is used only for a package installed without one.

`doctor` prints the launcher version and protocol, the protocol each shipped
plugin manifest declares, the store's schema against the schema this build
writes, and the `service` component naming the code a running service answers
from, then a verdict line. Each line carries the state this build puts that
component in, printed in upper case when the build does not accept it.

The `service` component has three states. `not running` means nothing answered
on the configured port, which is no drift. It is still an outage wherever a
lane is registered, because every hook on that machine then pays the in-process
decision, so the report names `agent-parley up` and the verdict is not
consistent; a machine with no lane registered has nothing to serve and stays
consistent with no service running. `ok` means the service is answering
from the sources on disk. `stale` means the checkout moved after the service
started, so it is answering from modules the tree no longer holds, and a module
a merge added is missing from that process for as long as it runs. A service
that reads itself stale stops accepting connections first, logs one line
naming the drift, and refuses further calls with the status its clients
already treat as an outage, so hooks decide in-process. The stop is started
before the line is written, so the window in which calls are refused is
bounded by the revision interval rather than by an append to a log file, and
the whole exit is bounded by that same interval once in-flight calls finish.
The first hook to read the published record of that gone process asks for a
new service, so the relaunch follows the stop. `status` prints the same drift
as a
`Code: stale` line, and `agent-parley up` then starts a service on the code in
the checkout.

The store has four states. `absent` means no store has been created yet, which
is consistent because the service writes it at the current schema. `ok` means
the store matches this build. `needs migration` means the store predates this
build and has not been migrated: every process running this code queries
columns the older store does not have, so this is a mismatch, not merely an
older number. `unsupported` means a newer build wrote the store, which is
refused rather than downgraded.

The verdict names one command per distinct cause, because a store behind this
build and a plugin speaking another protocol need different commands. A behind
store is resolved by `agent-parley up`, which migrates it in place and leaves
a running service and every lane running; a newer store by installing the
build that wrote it; a plugin mismatch by reinstalling the plugin. Its exit
status is non-zero on a mismatch, so a
script can gate on it. It reads only: it opens no lane, writes no configuration,
repairs nothing, and prints no credential or profile path.

Drift is refused where the call already crosses a boundary, not discovered
mid-turn:

- `agent-parley run` refuses to start a lane whose installed plugin declares an
  unaccepted protocol, naming both numbers and the one command that updates it.
- A lane's hooks carry the launcher's protocol in the command the launcher
  wrote, so the hook boundary is checked locally and no hook reaches the network
  to learn a version.
- A served call declaring an unaccepted protocol in its `Agent-Parley-Protocol`
  header is denied with both numbers, and `top` counts that denial.
- Opening a store written by a newer schema is refused, never migrated
  downwards, exactly as a newer project manifest already is.
- A hook whose read fails on a store behind its build migrates the store in
  place, under the store lock, and denies that one call with both schema
  numbers and a retry; the next call reads the migrated store. A package
  upgrade therefore no longer refuses every lane until a restart. A store
  stamped current that lacks a column the failed read names is repaired the
  same way, and every service start or `up` verifies each column the build
  owns instead of trusting the stamp.

### Triage with `problems`

`doctor` answers whether the installation is consistent and `top` shows every
lane; `problems` answers what needs an operator right now.

```sh
agent-parley problems
agent-parley problems --ack-after 900
agent-parley problems --json
```

The command derives its rows from the same reading `status` and `top` print,
the supervision thresholds and the store classification, and writes nothing.
Rows are ordered by how long each has held, longest first; a store or service
row carries no age and leads the list, because no lane can be acted on until
the store is usable and the service is up. One lane holding twenty messages,
three overdue claims or four changed files is one row per cause, carrying how
many items that row covers and the age of the oldest, so the list is as long
as the work rather than as long as the backlog. Each row names the lane, the
condition, that count, its age and what clears it:

| Condition | When | Clears with |
| --- | --- | --- |
| `store` | The store schema is behind or ahead of this build. | The `doctor` remedy for that state. |
| `service` | The coordination server is not ready, or is serving a build older than the installed code. | `agent-parley up`, or `agent-parley down && agent-parley up` for a stale one. |
| `stalled` | A lane reading `idle` holds mail older than `stalled_after` and served no call inside it. | Whatever the lane's state allows, from the remedy table below. |
| `inactive` | A live lane published no native activity inside `inactive_after`. | Whatever the lane's state allows, from the remedy table below. |
| `overdue claim` | One or more held issues are past their recorded deadline. | `agent-parley issue release NUMBER` for the oldest, named in the row. |
| `unanswered offer` | One or more handoff offers to the same lane have no answer yet. | `agent-parley issue cancel NUMBER`, or `issue assign NUMBER NAME --unassign` for an operator offer. |
| `unresolved completion` | One or more claimed issues read closed on the forge, or merged or closed on the lane branch when the forge cannot say, and their holder left `completion_reminders` reminders unanswered. | `agent-parley issue resolve NUMBER` for the oldest, named in the row, with `--release` when the pull request was closed without merging. |
| `awaiting acknowledgement` | Messages needing acknowledgement have waited past `--ack-after`, which defaults to `stalled_after`. | Whatever the lane's state allows, from the remedy table below. |
| `holding a refused key` | A lane that is not active refused a peer a reserved key and still holds it. The row names the refused lanes and how long the holder has been quiet. | Whatever the lane's state allows, from the remedy table below; an expired lease of a holder observed idle past `inactive_after` is also reclaimed by the next sweep. |
| `bounced share` | A share the sender is still waiting on reached a recipient that cannot act on it. The row sits on the sender's lane. | Whatever the first blocked recipient's state allows, from the remedy table below. |
| `ready to retire` | Every claim the lane holds has been orphaned for longer than `orphan_retire_after` and no peer took it. | `agent-parley participant retire NAME` |
| `branch drift` | The lane left its assigned branch. | `agent-parley participant restore NAME` |
| `dirty worktree` | The lane holds uncommitted work and is not active, or it retired and its uncommitted work kept the worktree. | Commit or stash the named files in the named worktree; `agent-parley participant add NAME` returns a retired lane to service with that work still in place. |
| `over budget` | The lane crossed an advisory token, call or hour limit. | `agent-parley participant budget NAME` |

A retired lane reports nothing but that kept worktree. Its quiet is the state
it was asked for, so it raises no stall, no inactivity and no acknowledgement
row, and no printed command wakes it.

A lane row's remedy follows the lane's state rather than the condition alone,
because a lane that cannot read mail does not become reachable by being sent
more of it:

| Lane state | Remedy | Why |
| --- | --- | --- |
| `stopped` | `agent-parley run NAME --resume` | The launcher exited, so nothing is holding the session lock. |
| Waiting on a native prompt | Answer the prompt in the lane's own client. | The client reads no mail until the prompt is cleared. |
| Refused every wake | Take the turn waiting in the lane's own client. | The service stopped asking after its refusals. |
| Paused | `agent-parley participant resume NAME` | Delivery resumes with the lane, not before it. |
| The service is still waking it | Nothing yet; the row says how many wakes the loop has already made and that it wakes again on its next poll. | The supervision loop owns this row. |
| Otherwise reachable | `agent-parley say NAME "<text>"` | The lane is alive and reading. |

A row an operator must act on and a row the supervision loop is already
working are therefore different rows. The wake, the delivery, the orphan
reclaim and the lease bounce belong to that loop, which runs them on its own
interval; `problems` reports what it has attempted and what it will do next,
and the closing line counts how many rows need an operator against how many
the service is handling. An active lane is never told to leave its session,
and a dirty worktree row names the worktree and the changed files rather than
offering to retire the lane, because retiring it would drop the claims it
holds to clean one directory.

An empty list prints one line saying so and exits zero; any row exits 1, so a
shell or a cron can gate on it. `--json` prints the same rows inside the shared
snapshot envelope, each with `count` and `actor`, alongside the totals
`count`, `operator` and `service`. The `P` key in `top` shows the same rows in
place of the table until any key returns. Every command named is a suggestion:
the view itself revokes nothing, releases nothing and wakes nobody.

### Recording the work order as a plan file

Entering a dozen dependencies one `issue block` at a time leaves no artifact to
review. Write the order once, as TOML:

```toml
[plan]
name = "Parser rewrite"

[dependencies]
"42" = ["17"]
"43" = ["17"]
"44" = ["42", "43"]

[groups]
parallel = ["42", "43"]
```

```sh
agent-parley plan diff work-order.toml
agent-parley plan apply work-order.toml
agent-parley plan show
agent-parley plan show --json
```

`plan apply` records exactly the advisory dependencies `issue block` records and
nothing else. It claims no issue, assigns no lane and gates no transition, so a
plan that turns out to be wrong blocks nobody. Run `plan diff` first to see the
edges an apply would add before it adds them.

`plan show` prints the applied plan as an indented tree, each issue under the
issues it waits on, with its current owner and any recorded forge title beside
it. Each apply records a version carrying the file's digest and the identity
that applied it, and the twenty most recent versions are kept.

Applying adds edges and never removes one. An edge entered with `issue block`
after the apply is listed as `recorded by hand`, and an edge the file no longer
names stays in the ledger and is reported by `plan diff` as `unlisted` until
`issue unblock` removes it, so the document and the ledger never silently
disagree.

A plan that names a malformed issue number, exceeds a bound, or describes a
dependency cycle is refused before any edge is written. The cycle check covers
the edges already in the ledger, so two plans that each look acyclic cannot
together form one. An edge to an issue that is already complete is skipped.
Groups name issues that
may proceed together, and `participant merge --group NAME` integrates one.

### Integrating several lanes at once

```sh
agent-parley participant merge --all
agent-parley participant merge --group rewrite
agent-parley participant merge --group rewrite --preview
```

`--all` takes every lane whose latest report is ready; `--group` takes the lanes
holding one group's members, refusing when a member is unclaimed. Both order the
candidates from the recorded dependency edges, so a lane whose issue waits on
another is merged after the lane holding that issue. Edges leaving the candidate
set constrain nothing, and a cycle among the candidates is refused and named
rather than quietly ordered.

Every candidate is preflighted with the conditions `--preview` reports, and each
merge runs through the single-lane path, so no lane is integrated on easier
terms than it would be alone. A group is admitted whole or not at all: one
refused member leaves the group unmerged and the base checkout unchanged.
Execution is ordered rather than all-or-nothing: the run stops at the first
refusal or failure, a refused lane is never followed by a lane that waits on it,
and the report names what was integrated, what refused and what was not
attempted, with the dependency reason. Nothing is reset or reverted, and a
conflict is left in the working tree for you.

With a verification command recorded, it runs before each merge as usual and
again after it, so a set whose halves pass alone but fail together is caught
before you move on. A failure after a merge stops the run and leaves that merge
commit present and visibly unverified.

`plan show` and `status` mark a group whose every member is reported ready, and
the `top` header counts those groups, so an integrable set is visible before
anyone merges. A reported state is the lane's own account of its work, never
review or independent verification.

### Selecting several lanes for one command

```sh
agent-parley say "Wrap up for today." --all
agent-parley participant stop --provider claude --yes
agent-parley participant resume --idle
agent-parley participant merge --outcome ready
agent-parley issue assign 42 --idle --provider codex
```

`say`, `issue assign`, `participant stop`, `participant pause`,
`participant resume`, `participant pr` and `participant merge` take a lane
selector where they otherwise take a positional name. `--all` selects every
lane; `--provider NAME`, `--outcome STATE`, `--drifted`, `--idle` and
`--over-budget` narrow the
selection, and a lane matches only when every given filter holds. The filters
read the same facts `status` reports. A positional name and a selector together
are refused, because a command that means two things is a command that loses a
lane.

Every bulk run prints the lanes it matched and what will happen to each, then
asks once for the whole set. `--yes` answers that one question in advance.
A selector matching nothing does nothing and says so.

Non-integration operations are independent, so a lane's refusal is printed
beside that lane and the remaining lanes are still attempted. The closing tally
names what was done and what refused, and the exit status is non-zero when any
lane refused or failed. Integration keeps its ordered contract instead: the run
stops at the first refusal or failure and leaves every later lane unattempted,
including the lanes that wait on the one that stopped.

A bulk merge considers only lanes whose own report is ready, so a selector
narrows that set rather than widening it, and its plan names every prerequisite
that lies outside the selected set: held by a lane that is not selected, and so
not satisfied here, or released and held by nobody. Narrowing a selection never
lifts a recorded dependency and never admits a lane on easier terms than the
single-lane merge would.

One issue carries one offer, so `issue assign` accepts a selector only while it
matches a single lane. A wider match is refused and names every lane it matched,
because choosing between them is the operator's decision. `--unassign`
withdraws the offer recorded on one issue and takes no selector. `say --key`
names one message and is refused with a selector, because the default key
already gives each lane its own copy.

### Retrying a write safely

A command that fails after its change has landed cannot be told apart from one
that changed nothing, so a retry can apply the change twice. Give the command an
idempotency key and the retry is the same call:

```sh
agent-parley issue offer 42 --to codex --summary "commit, checks" \
  --idempotency-key handoff-42-first
agent-parley report --state blocked --summary "Waiting on the schema decision" \
  --remaining "Apply the migration" --idempotency-key blocked-42-first
```

Rerunning either command with the key it first used prints the first result and
changes nothing further: one offer, one recorded attempt, one forge comment. The
same key with different arguments is refused by name, so a stale retry cannot
release a different issue or hand off to a different lane. A command that was
refused replays as the same refusal, even if ownership changed in between, so a
retry never gains authority the first call was denied. A refusal that says to
retry, such as a busy ledger lock or recovery evidence that changed, is not
recorded against the key, so retrying with the same key is evaluated afresh.

Every `issue` transition and `report` accepts `--idempotency-key`. Over MCP the
same contract is carried by the optional `idempotency_key` argument on
`file_reservation_paths`, `request_reservation`,
`cancel_reservation_request`, `release_file_reservations`,
`acknowledge_message` and
`mark_message_read`; `send_message` has always required one. A replayed tool
result carries `"replayed": true` beside the original fields.

Keys belong to one participant and one operation, so two lanes can use the same
key text without colliding, and the most recent 500 keys per participant are
retained. Past that window a repeated key is treated as a first call, which is
why a key is worth reusing for a retry and not for bookkeeping.

### Deadlines and attempt budgets

A claim, a handoff offer and an acknowledgement can carry a deadline:

```sh
agent-parley issue claim 42 --within 2h
agent-parley issue offer 42 --to codex --summary "commit, checks" --within 30m
agent-parley say codex "Confirm the schema change" --ack --within 15m
agent-parley deadlines show
agent-parley deadlines set --claim 4h --offer 30m --ack 15m --attempts 3
```

**An overdue claim is still owned.** Past its deadline the claim reads `overdue`
in `status`, `issue list` and `top` — where the issue is marked `#42!` — with the
seconds it is over. Ownership does not move, nothing is revoked, and only an
explicit release or an accepted handoff ever transfers an issue. The same is
true of an exhausted attempt budget.

A lane that reports `blocked` on an issue it still holds spends one attempt.
`status` and `issue list` show attempts against the budget, and exceeding the
budget is another visible state with the same guarantee: what to do about it
stays the owner's or the operator's decision.

`deadlines set` records the defaults every claim, offer and acknowledgement
inherits when it passes no `--within`, so lanes carry a budget without repeating
a flag. Windows take the same units as `--since` (`45m`, `6h`, `7d`), and a
project that records none gives a claim and an offer a deadline only when they
ask for one.

**Every acknowledgement request carries a deadline.** A send marked
`ack_required` that names no window takes the project's `--ack` default, and a
project that records none takes 240 seconds, which is shorter than the 300-second
`inactive_after` default so a missed acknowledgement is known before the lane
itself reads as idle. A served lane sets its own window with the `ack_within`
argument of `send_message`.

**A missed acknowledgement goes back to its sender.** Past the deadline the
supervision sweep sends the sender one message naming each recipient that did
not acknowledge and what the runtime could read about why — no running session,
a native dialog waiting for the operator, exhausted provider capacity, or an
idle stretch — and retires the expectation, so the request stops being reported
as outstanding. The notice is deduplicated by the message it reports, and
retiring records no acknowledgement for any lane: a recipient that never
answered still carries no acknowledgement time. A sender that holds no inbox,
such as the supervising operator, is not mailed and the expectation is still
retired.

**Acknowledgement debt expires.** A request past its deadline, one a newer
message on the same topic superseded, and one bound to a claim that closed no
longer lower the lane's fitness for new work, and a checkpoint no longer lists
them as owed. A lane that reads as stale by `inactive_after` owes nothing to
the fitness check at all, because it cannot answer until it returns. The
overdue request itself still appears in `status` and still goes back to its
sender as above.

**A share no recipient can act on comes back before its deadline.** A share is
an acknowledgement request still inside its window: the sender is waiting for an
answer that decides whether work moves. Writing it into a mailbox is not
receipt, so each sweep reads whether every recipient could answer at all — a
live session process, no native dialog on its screen, provider capacity that is
not exhausted, and no claim of its own blocked by unfinished dependencies. A
recipient failing one of those cannot answer, and the sweep returns the share to
its sender naming each such recipient and its reason. The sender keeps the work
and may offer it to a lane that reads as fit; the notice is ordinary mail, so it
joins the sender's backlog rather than leaving that lane waiting quietly. The
notice is deduplicated by the share it reports. Nothing is withdrawn, no
ownership moves, no mail is deleted, and the acknowledgement expectation stays in
force, so a recipient that recovers can still answer and the deadline above
remains the only place an expectation is retired. `problems` carries the same
reading as a `bounced share` row on the sender's lane while it holds. A request
the operator sent is not returned this way: that operator is at a terminal and
already reads it as awaiting acknowledgement.

Deadlines are evaluated when a checkpoint, a `status`, a `top` refresh or a
served call reads the record, from the stored timestamps. The service gains no
background scheduler, and a stopped service produces no phantom transitions. The
runtime records one bounded notice per breach, addressed to the owner and to any
lane waiting on that issue through a recorded dependency, so a peer can decide
whether to ask for a handoff; the notice reaches them through the checkpoint
delivery that already carries ledger changes.

### Giving a lane a budget

```sh
agent-parley participant budget claude --tokens 2000000 --calls 5000 --hours 8
agent-parley participant budget claude
agent-parley provider budget codex --calls 5000
agent-parley budget set --tokens 2000000
agent-parley budget show
agent-parley status --over-budget
agent-parley participant pause --over-budget
```

A budget is distinct from a deadline: deadlines are windows and attempt
counts on claims, offers and acknowledgements, while a budget is a ceiling on
what a lane consumes. `participant budget NAME` records limits on one lane,
`provider budget NAME` on every lane that provider drives, and `budget set` on
every lane of the project; a participant's limit wins over its provider's,
which wins over the project's, field by field, and any limit may stay unset.
Passing `0` for a field removes it. `participant budget NAME` with no flags
prints the lane's own limits and its standing against the limits that apply.

Consumption is measured from readings that already exist. Tokens are what the
lane's own native client recorded for its session, exactly as the `TOKENS`
column reports them: no vendor request, no key, no price, so a token budget is
a count and **not** spend. Calls are the coordination calls the store served
for the lane within retention. Hours are how long the session process the
launcher recorded has been alive; a stopped lane counts no hours.

Crossing a limit is a visible state and one notice. `top` and `status` print
`budget; tokens 1,200,000 of 2,000,000 (60%)` under a lane that carries a
limit and `over budget; tokens 2,400,000 of 2,000,000 (120%)!` once one is
crossed; `status NAME` and the JSON documents carry the figures under
`budget`. The lane receives one bounded checkpoint notice naming the crossed
limit, recorded in its lane state so it is not repeated until the limit is
crossed again. Nothing is stopped, revoked or refused: `--over-budget` selects
the crossed lanes, and `participant pause --over-budget` or `participant stop
--over-budget` is the operator's decision to make.

### Delivering a message or an offer later

An operator message and a handoff offer can carry a delivery condition, so a
steer recorded now reaches the lane when it is useful rather than when it was
typed:

```sh
agent-parley say codex "Rebase onto main" --after 30m
agent-parley say codex "Wrap up for today" --at 18:00
agent-parley say codex "Pick up 18 next" --when-released 17
agent-parley say codex "Still nothing?" --after 30m --unless-reported
agent-parley say codex "Status, please" --every 1h --until 18:00
agent-parley issue offer 21 --to codex --summary "commit, checks" \
  --when-released 22
agent-parley mail pending
agent-parley mail cancel 3
```

**Recording is not delivering.** The item waits in the coordination store and is
delivered by the supervision poll that already observes every project. There is
no scheduler process and no extra thread. `status`, `top`, `mail pending` and
every other read-only path report pending items and deliver none, so a stopped
service delivers nothing and loses nothing: the item waits and is delivered on
the first poll after the service returns.

`--at` and `--until` read a 24-hour time of day in the timezone of the machine
the command is typed on, and resolve to today while that time is still ahead and
to tomorrow once it has passed. The instant is then recorded absolutely, so a
later timezone change, a daylight-saving transition or a restart never moves a
recorded item. A time that passed while the service was stopped delivers on the
next poll rather than being skipped.

`--when-released` is answered from recorded ledger transitions only: an explicit
release of that issue, or supervision's own record that the pull request of the
current ownership generation ended. An issue that was released and claimed again
reads as held, and an old pull request on a reused lane branch never answers for
a later claim. An item carrying both a time and a condition waits for both.

`--unless-reported` applies to a delayed message and drops it once the lane files
a report of its own, which is the answer the reminder was going to ask for.

**A repeat is bounded and there is no cron syntax.** `--every` requires
`--until`, and the repeat is capped at 24 deliveries however wide that window
is. Each occurrence carries its own deduplication key, so a restart delivers an
occurrence once; a repeat that fell behind while the service was stopped catches
up one delivery per poll. Each occurrence keeps the time it was planned for
beside the time it was actually delivered, so the two are compared rather than
conflated.

`mail pending` lists every recorded item with its recipient, its time, its
condition and the deliveries it has left; `mail cancel ID` removes one before it
is delivered. `status` counts a lane's pending operator items.

### Attaching what does not fit

Every coordination payload has an explicit UTF-8 byte cap: a message body
4,096, report `--evidence` 4,096, a handoff summary 2,048, and report
`--summary` and `--remaining` 4,096 each, which are refused above it. A
body above its cap is not refused and not truncated. It is written whole to
`attachments/` under the project's private state directory, keyed by the
record it belongs to, and the record keeps the first bounded slice ending
with a line such as `[attachment message-12: 20480 bytes]`. The recipient's
checkpoint notice stays within its 1,536-byte budget and ends with that
reference; for a handoff the reference is restated after the clipped issue
notice. Nothing is delivered whole into a context automatically.

```sh
agent-parley mail show 12 --full
agent-parley report show 3f9a1c2e4b5d6e7f --full
```

Over MCP, `read_attachment` takes the reference and an optional character
`offset` and returns 2,048 characters per page beside the full byte count.
A reference is an opaque `kind-identifier` token, never a path: it is
validated by pattern before it reaches the file system and resolved only
inside the attachment folder. Only the participant that wrote an attachment
and the participants its record was addressed to can read it; a report's
attachment is readable by its own lane.

One attachment is capped at 65,536 bytes and a lane holds at most 1 MiB of
attachments in total; a body past either cap is refused with the cap named.
A message attachment lives as long as its message; a report attachment is
removed when the report log rotates past that record; an offer attachment is
removed when the offer is declined, cancelled or replaced, and an accepted
offer keeps it until the issue is released. `top` and `status` count as
`CONTEXT` only the bytes coordination injected, never an attachment's size,
and the `top` legend says so.

`agent-parley top` watches every participant live: session state, event age,
branch with a `!` when a lane left its assigned branch, issues owned and
handoffs pending, unread and unacknowledged mail, held leases with the age of
the oldest, delivered context, denials against retained hook events, served
MCP calls with rejections, and the tokens that lane's own native client
recorded. The header carries server health, the project's denial rate and
how long the frame took to read. The view is read-only and makes no model
call; `q` leaves it.

Like a task manager, the view lists what is live. A lane is out of a live
state when it is retired or when its lane state record is `stopped`, `dead`
or `reclaimed`; a lane with no record yet falls back to a session cell that
reads stopped. Such a lane which owns no issue, holds no offer, holds or
waits on no lease, has no unread or unacknowledged mail and has no ready
report awaiting approval is left out. So is every lane of a project whose
root no longer exists, such as a run under `/tmp` after a reboot. A lane
whose mailbox or lease store could not be read stays on screen. The session
cell names the recorded state, its cause and how long it has held it. The
header counts what was left out. `a` in the live view and
`--all` on the command line show it again, and `--json` always reports every
lane. A stopped lane that still holds work stays on screen, because that work
needs the operator.

Git readings dominate a frame, so the live view reads each lane's branch
and each project's operator edits and base advances at most once every five
seconds. The rest of the frame is read on every redraw.

The table is fitted to the terminal rather than fixed. Each column is as
wide as the widest value in that frame and never narrower than its declared
minimum. When the set does not fit, columns are dropped in this order, and
the header names the ones that went:

```text
PROVIDER, EVENT, BRANCH, CONTEXT, CALLS, TOKENS, UNUSED, LEASES, FIT, IDLE,
REVIEW, ISSUES, DENIALS
```

`PARTICIPANT`, `STATE` and `MAIL` are never dropped; if they alone still do
not fit they share the width that is left, and a value a column could not
show in full ends in an ellipsis. Rows past the last line are paged rather
than discarded: the footer reads `rows 1-8 of 31`, and the page follows the
selection, so moving past the last visible lane scrolls the table. A lane
never loses its stall marker or its last prompt to a page break.

Keys in the live view:

| Key | Does |
| --- | --- |
| `j`, `k`, down, up | Select the next or previous lane, paging the table. |
| `enter` | Show every recorded field of the selected lane, unclipped. |
| `s` | Order by the next column. |
| `r` | Reverse the order. |
| `f` | Narrow to participants, comma separated; empty clears. |
| `o` | Narrow to projects, comma separated; empty clears. |
| `c` | Choose the columns shown; empty shows all. |
| `a` | Show or hide stopped lanes and projects whose root is gone. |
| `?` | Show the key map and the column legend. |
| `q` | Leave. The view never writes state. |

The same choices are available on the command line as `--sort COLUMN`,
`--reverse`, `--project ROOT`, `--participant NAME` and `--columns LIST`,
and they apply to `--once` and `--json` as well as to the live view. A sort
on a counted column orders from the largest value down. Narrowing recounts
the header, so it never counts a row the table does not show. A project is
matched by path or by its trailing directory name.

Branch drift, a stale lease, a rejected call, an overdue issue and a lost
session process are drawn in colour where the terminal reports colour
support and in bold where it does not. Each also prints its own `!` or word
in the table, so a monochrome terminal and a piped capture read the same.
The legend that explains the columns moved behind `?` and out of every
frame.

A resize redraws the frame from scratch. Each line is written on its own,
so a write the terminal refuses is counted and named in the footer instead
of leaving a half-drawn frame. `--once` prints at the width of the terminal
when one is attached and at the full width of the table when the output is
a pipe, so a captured file holds every column intact.

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
`top` gains `!` and the stale count, `status` counts the expired leases apart
from the live ones and names the age of the oldest in seconds past its
deadline, and a conflict names that holder as stale.

An expired lease does not stay expired. A holder that is still working renews
it at that lane's next tool call, restoring the window the holder declared, so
a lane working under a key keeps it; a session start, a prompt, a turn end or a
supervisor resume renews nothing. A holder whose last observation found no live
session process, or found it idle past the inactive threshold, or whose lease has been expired
longer than the 1800-second grace, loses it: the runtime releases the lease,
grants the oldest queued request for each key, tells the lane that took the key
who lost it, and tells the former holder what was released and why. A lease
whose correlated claim is closed is released at the holder's next checkpoint in
the same way. Nothing here is enforcement: reservations stay advisory, nothing
on disk is locked or reverted, and a lane that is still editing a reclaimed key
reserves it again.

A reservation is also forecast against the base checkout's co-change history.
When a lane files reservations, the store reads `git log --name-only` over the
last 500 commits of the base checkout once per project, bounded by a timeout
and cached as `cochanges.json` in the project state directory keyed by the
head commit, so the read repeats only when the base moves and never runs
inside the store's write transaction. Every file that changed in the same
commit as a newly reserved path at least three times, and that a peer holds
right now, is reported in the grant as `forecast`, with `path`, `peer` and
`count`, bounded like `conflicts`. `issue claim` carries the same forecast
from the paths the issue's earlier pull requests touched, read through `gh pr
list --search "closes #N"` when a forge is configured, and stays silent
otherwise. The forecast is advisory: nothing is withheld, and a history Git
cannot read in time is no forecast rather than a failed call.

A person editing the base checkout is the one writer reservations never saw.
Each `top` and `status` frame runs `git status --porcelain` once for the base
checkout of every project and compares the dirty paths against each lane's
active reservation patterns, using the same overlap rule a competing
reservation is judged by, so a glob reservation matches a new file under it. A
match is printed as an indented line under the lane's row in `top`, listed
under the reservation count in `status`, and carried in the `operator_edits`
field of `top --json` and `status --json`. The lane's lifecycle hook delivers
one bounded advisory notice naming the paths and stating that nothing was
reverted; the notice repeats only when the set of paths changes, which the hook
records in the lane's activity state. Nothing pauses, reverts or locks, and a
Git failure or timeout reports nothing rather than an error. `--no-operator-edits`
on `top` skips the reading for a repository whose base checkout is always dirty.
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

A base branch that moves is the other writer no lane sees. Each `top` and
`status` frame reads the head of the base checkout once per project and
compares it with the point each lane branched from, through `git merge-base`.
A lane still forked from that head is current and is read no further, so a
project whose lanes are all up to date costs one Git read and no store read at
all. Where the base did advance, the paths it changed since the fork point are
matched against the lane's active reservations, using the same overlap rule a
competing reservation is judged by, and against the paths the lane itself
holds: those committed on its branch and those still uncommitted in its
worktree. A match is printed as an indented line under the lane's row in `top`,
under the reservation count in `status`, and carried in the
`base_advance_paths` field of `top --json` and `status --json`. The lane's
lifecycle hook delivers one bounded advisory notice naming those paths and
stating that nothing was rebased; the notice repeats only when the set of paths
changes, which the hook records in the lane's activity state. Nothing rebases,
pauses or reverts, and a Git failure or timeout reports nothing rather than an
error.

Every recorded decision carries the failure that produced it, when one did, so
a run of denials stays answerable after the lane recovers. The live copy of
that failure is cleared by the first call that succeeds, which is exactly what
the remedy for an outage produces, so recovery would otherwise destroy the only
record of the cause. The retained text is bounded, and the most recent one is
reported beside the counts.

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

`UNUSED` is the lane state accounting `status` prints on its `Lanes:` line,
per lane: idle lane-minutes per observed lane-hour, then unaccountable
claim-minutes, as `35.0/4.0`. Both are totals since the first poll that
accounted the lane. The cell is `-` until a poll has accounted the lane.

`FIT` is the capacity check the runtime last read for that lane, and a `+`
after it means an advisory work offer is waiting for that lane to act on. Four
checks run, each from what the host can already read and none of them asking a
vendor: the recorded session process is running and its session has not ended
or stopped for an approval; the lane's own client records carry no rate-limit
or usage-window refusal inside the stall interval, read from the same files
`TOKENS` parses; the worktree is on its assigned branch or has nothing
uncommitted elsewhere; and the lane owes no acknowledgement older than the
stall interval. Each check is provider specific and defaults to no opinion, so
a provider whose client publishes nothing skips that check rather than blocking
an offer, and only a check that actually failed makes a lane unfit. An unfit
lane prints the failed check under its row, and no offer names it. A blank cell
means nothing has been published for that lane yet.

Three offers are built on that check, all advisory and none moving ownership.
A lane that holds no claim and passes the check is offered, at its next
checkpoint, the unclaimed ledger issues no recorded dependency blocks — ordered
so the ones other owned issues wait on come first — together with the peers
holding more than one claim. A lane that holds more than one claim is told
which fit peers have been idle past the stall interval, so it can shed one.
A lane that holds one claim carrying a countable backlog, and that has itself
recorded no coordination event for that same interval, is offered a split of
that backlog. Every message counts against the same 1,536-byte checkpoint
budget as every other injection and is delivered once per distinct offer: a
lane whose situation has not changed sees nothing new. Nothing is claimed for a
lane, `issue offer` remains the only transfer path, and the recipient still
accepts or declines.

**An idle holder offers the split itself.** A claim held by a lane that has
gone quiet on it is a defect that reads as healthy: the claim is held, the
remaining work is untouched, and no row says anything is wrong. The runtime
cannot count that remaining work by itself, because a claim's units are
whatever its own domain counts — issue families in a target project, files to
convert, subtasks of a migration — so the owner states the count on its own
progress report:

```sh
agent-parley report --state partial --summary "converted 12 families" \
  --remaining "families still to convert" --backlog 129
```

The count is recorded on that claim's execution state, bound to the claim
generation the report named, and a later report that does not restate it leaves
it standing. A new claim generation starts with no count, because the lane that
takes the work states its own. Once the count is above zero and the lane's own
idle stretch passes the stall interval, the sweep offers it a split without an
operator asking for one, naming the issue, the count and the peers that could
take part of it. A recipient qualifies only when both readings already in the
runtime agree: the fit check above, and the same share conditions a returned
share is judged by — a live session process, no native dialog on its screen,
capacity that is not exhausted, and no claim of its own blocked by unfinished
dependencies. When no peer qualifies, nothing is offered and nothing is
invented; if the lane does split its work and sends a part, a recipient that
cannot answer returns that share through the bounced-share path above. The
holder's own capacity check must also not have failed, since it has to take the
turn that sends the share, and an exhausted owner belongs to recovery instead.
The offer and its dispatch outcome sit on the holder's row in `status` and
`top` as `split offer pending`, like every other work offer. The lane decides
what to split, nothing moves until a recipient answers, and no ownership
changes here.

Idleness here is observed coordination inactivity, which is not the same thing
as a live process or as provider capacity; the three are checked separately and
reported separately, because a quiet coordination channel alone does not prove
that a native turn is idle or that a lane can safely accept input.

When an owner is alive but its provider has published an exhausted capacity
window, the operator can approve recovery of that exact claim and native
session from the project base checkout:

```sh
agent-parley issue recover 42 --reason "continue on an available lane"
```

The command records an approval; it does not stop a process by itself. A later
supervision pass must publish a matching current capacity observation before
the runtime stops the process, captures its committed, staged, unstaged and
non-ignored untracked content, and marks the claim recoverable. A refusal from
the provider, elapsed time, or the approval alone cannot transfer ownership.
Recovery also refuses while the owner is paused, waiting for a native approval,
or waiting for more operator input.
The receiving lane still runs `issue claim 42 --take-orphaned`. That claim
revalidates the stopped process and ownership generation, moves only the old
claim's reservations, fast-forwards to the captured committed HEAD, and restores
the captured index and working tree into a clean destination. It permits
unrelated ignored caches, but refuses dirty or untracked content and ignored
content at a path recovery would change. Both worktrees stay intact. Restore
progress is durable; after interruption, the new owner reruns
`issue claim 42` to resume the exact recorded phase. Only the ownership
generation the take created restores anything: an accepted handoff, a release
or any later claim moves the take and any orphan marker into that transition's
history entry, so a lane that accepts the work never replays the dead owner's
checkpoint. An owner that re-claims its own orphan-marked issue keeps its
accepted handoff and attachment and retires its previous generation's mail.

The checkpoint and its Git bundle live in the private Agent Parley state
directory. The bundle carries an exact size and SHA-256 digest, so binary and
large files are referenced rather than embedded in the issue ledger. Ignored
untracked files are excluded. Each checkpoint records a fingerprint of the
lane's HEAD, its porcelain status and the size and modification time of every
changed path. A hook event that finds the same fingerprint and an existing
bundle refreshes only the step, gate and blocker fields and writes no new
commit or bundle, so repeated tool calls on an unchanged tree stay cheap. The
service takes the capture after it has answered the hook, so a slow bundle
never delays a native call. The old session generation is refused by later
lifecycle hooks after takeover; this is runtime fencing, not a filesystem
security boundary against another process writing directly into the old lane.

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

### Ownership history

`agent-parley history` answers over the ledger, the per-lane report log and the
store:

```sh
agent-parley history issue 42
agent-parley history participant claude-1 --since 7d
agent-parley history claim CLAIM_ID --json
agent-parley history issue 42 --kind claim --kind handoff
```

`history issue 42` lists every claim, release, handoff, dependency change,
reservation, message and report that touched it, in order, and heads the listing
with each ownership generation and how long it was held. `history participant
NAME` lists the same for one lane, and `history claim ID` prints the whole chain
from a claim to the pull request that ended it.

`--kind claim|handoff|report|reservation|message`, `--participant`,
`--provider`, `--issue` and `--since` combine on any listing, and `--json`
prints one document with a `records` array, consistent with the snapshot
contract below.

**A correlation key follows the work.** Every claim carries its own identifier,
minted again on each claim and each accepted handoff, so releasing and
reclaiming an issue produces two distinct generations rather than one blurred
one. Every record made while a claim is held carries that identifier:
reservations and messages in the store, reports and integrations in the lane's
report log, and the pull request that ended the claim.

**It reads, only.** The store is opened read-only, no lock is taken and no
record is rewritten, so a history query is safe beside running lanes. Retention
follows each substrate: the issue ledger keeps its records until the project is
removed, the report log keeps the newest 2000 records and is rewritten in
place once it passes 256 KB, and mail and reservations keep theirs for as long
as the store does. A record written before this correlation existed carries
no claim and is reported as `unknown`; nothing is back-filled, because an
invented correlation is worse than an honest gap.

### Following one lane

`agent-parley watch NAME` sits on one participant and prints its coordination
events as they happen, one `TIME KIND description` line each:

```sh
agent-parley watch codex-2
agent-parley watch claude-1 --since 1h --kind claim --kind handoff
agent-parley watch claude-1 --json | jq .description
```

The stream starts from the most recent twenty events, or from every event
inside `--since`, and then follows the ledger, the report log, the store and
the lane's hook event log on a short interval, printing only what is new.
`--kind` takes the history kinds (`claim`, `handoff`, `report`, `approval`,
`reservation`, `message`) plus `call` for a served coordination call, `denied`
for a hook denial and `session` for a session boundary. A lane whose session
ends prints one `session ended` line and the stream keeps following, so a
restart appears as `session started` in the same stream. `q` or an interrupt
leaves.

When standard output is a pipe the lines are plain with no cursor control, and
`--json` prints one JSON object per line rather than one document, so
`watch NAME --json | jq` works. Each object carries `at` in RFC 3339, `kind`,
`participant`, `description` and the identifiers the line abbreviates, such
as `issue`, `claim_id`, `message_id`, `thread_id` and `tool`.

**It reads, only.** The store is opened without a write transaction, the event
files are read under their shared lock for the bounded lifetime of one read so
a rotation cannot split the snapshot, and no coordination state is mutated.
Every record is identified by its content rather than by its position in a
file, so a rotation between two reads neither drops nor repeats a line.

**It never shows the agent's conversation.** The stream is coordination only.
The native client's transcript stays in that client; nothing the agent said or
was told is read or printed.

### Machine-readable output

Every read-only command also accepts `--json` and prints exactly one JSON
document on standard output:

```sh
agent-parley status --json
agent-parley top --json
agent-parley version --json
agent-parley issue list --json
agent-parley issue show 42 --json
agent-parley issue next --json
agent-parley participant list --json
agent-parley participant show claude --json
agent-parley mail thread THREAD_ID --json
agent-parley mail search "reservation conflict" --json
agent-parley mail list --json
agent-parley approval show --json
agent-parley verify show --json
agent-parley init show --json
agent-parley branch show --json
agent-parley forge show --json
agent-parley state show ARCHIVE --json
agent-parley provider list --json
agent-parley provider show claude --json
agent-parley credentials list --json
agent-parley credentials show work --json
```

The commands that change something report their outcome the same way, so a
skill parses one document instead of scraping a sentence:

```sh
agent-parley up --json
agent-parley down --json
agent-parley setup PATH --json
agent-parley run claude --json
agent-parley say claude "Rebase first" --json
agent-parley mail send claude "Rebase first" --json
agent-parley mail cancel ITEM_ID --json
agent-parley approve claude --json
agent-parley reject claude "Rebase first" --json
agent-parley problems ack MESSAGE_ID --json
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
| `kind` | The command reported: `status`, `top`, `metrics`, `version`, `issues`, `issue`, `issue_next`, `participants`, `participant`, `history`, `mail_thread`, `mail_search`, `mail_list`, `mail_pending`, `mail_cancel`, `approval`, `verify`, `init`, `branch`, `forge`, `state`, `setup`, `up`, `down`, `run`, `say`, `approve`, `reject`, `problems`, `problems_ack`, `resources`, `providers`, `provider`, `credentials` or `credentials_show`. |
| `generated_at` | RFC 3339 UTC instant the snapshot was taken. |

Repeated rows are arrays rather than objects keyed by name, so a reader pages
them without knowing the identifiers in advance, and every recorded time is RFC
3339 in UTC whether the lane state files or SQLite recorded it. The document
carries the identifiers the table abbreviates: offer IDs on `issues`, message
and thread IDs on `mail_thread` and `mail_search`, participant names, registered
identities and branch names everywhere they apply. It carries no credential
value; a credential profile is named, never its contents.

`version --json` reports `version` and `state_directory`. `issue show --json`
reports the `issue` asked for, the ledger `revision`, its `record` as the
`issues` array shapes one entry or null where the ledger never recorded it,
the advisory `reservations` its owner holds, and the same `history` document
`history issue N --json` prints. `participant show --json` reports every field
a `status` participant carries, plus the `worktree` the lane lives in, the
advisory `budget_limits` the manifest records and `wake_enabled`.
`mail list --json` reports `limit`, `messages` newest first and `has_more`.
`approval show --json` reports `root`, the `approval` steps and `required`;
`branch show --json` reports `root` and `prefix`; `forge show --json` reports
`root` and `forge`; `state show --json` reports the `archive` path and its
`manifest`. `provider show --json` reports the definition beside its
`provider` name, and `credentials show --json` reports `credential`,
`config_home`, `env` as `NAME=<redacted>` entries and `require_env`: no
recorded value is printed. `problems ack --json` reports the message `id` and
the `participants` the acknowledgement was recorded for.

`issue next --json` reports the reading lane as `participant`, its `provider`,
`forge_paths` stating whether the forge answered with any paths at all, and
`candidates`. Each candidate carries `issue`, the recorded `title`, its plan
`group` and whether that group is `group_underway`, the owned issues it
`unblocks`, the `provider` it declares, the peer `overlaps` and forecast
`collisions` its likely paths run into, and the ordered `reasons` for its
place.

`resources show --json` reports `root`, the declared `resources` array and
`declared`. `status` reports `server`, `state_directory` and one entry per
project holding
`root`, the issue ledger as `revision` and `issues`, and `participants`. `server`
carries the service reading the `Code:` line prints, so a stale service is
readable without parsing text. Each
participant carries `participant`, `identity`, `provider`, `credential`,
`session`, `availability` as `active`, `idle` or `stopped` with the derived
`activity`, its `evidence` and whether that evidence is `stale`, `branch`,
`assigned_branch`, `drift`, `paused`,
`outcome`, `summary`, `remaining`, `evidence`, `reported_at`,
`report_age_seconds`, `injected_bytes`, `injections`, `claims`, `idle`,
`idle_seconds`, `idle_complete`, `waiting`, `wake` and `mail`, whose `unread`
counts live mail alone and whose `superseded` counts the deliveries a closed or
reassigned claim retired, whose
`named_resources` array lists the named resources that lane holds, whose
`queued_requests` counts the reservation requests waiting on the keys it holds
and whose `queued_by` names the lanes that asked. `claims`
carries one record per issue that lane owns, with its `deadline_at`, `overdue`,
`overdue_seconds`, `attempts`, `budget` and `budget_exceeded`. `idle` carries
`stalled`, the waiting item's `kind`, `message_id`, `sender` and `age_seconds`,
the `served_age_seconds` since the last served call, and the same `marker` the
table prints. `waiting` carries one record per pending wait, longest first,
each with its `kind`, its item and `seconds`. A
mailbox that cannot be read reports `{"error": "..."}` in `mail` rather than
failing the document, exactly as the table reports coordination as unavailable.

`top` reports `server`, `state_directory`, `window_seconds`, `providers`,
`totals` and one entry per project holding `root` and `participants`. Each row
carries `participant`, `provider`, `credential`, `state`, `last_event_at`,
`stalled`, `stall`, `branch`, `drift`, `issues`, `offers`, `unread`,
`superseded`, `pending_ack`, `leases`,
`stale_leases`, `lease_age_seconds`, `queued_requests`, `queued_by`,
`injected_bytes`, `hook_events`,
`denials`, `calls`, `errors`, `tokens`, `idle_seconds`, `idle_complete` and
`prompt`. `queued_requests` counts the reservation requests waiting on the keys
that lane holds and `queued_by` names the lanes that asked; a queued request
holds nothing itself. `tokens` is null when that
lane's own session records could not be read, and `unread` and `pending_ack`
are null when its mailbox could not be read: null states that nothing was read,
never that the count is zero.

`history` reports the `subject` and `value` queried, a `holdings` array of
ownership generations with `participant`, `claim_id`, `started_at`, `ended_at`
and `seconds`, and a `records` array whose entries carry `kind`, `action`, `at`,
`participant`, `provider`, `issue`, `claim_id` and `detail`, plus the
identifiers their kind adds: `path` for a reservation, `message_id` and
`thread_id` for a message, `report_id` for a report.

`issues` reports `revision` and an `issues` array whose records carry `issue`,
`owner`, `title`, `deadline_at`, `overdue`, `overdue_seconds`, `attempts`,
`attempt_budget`, `budget_exceeded`, `blocked_by`, `offer` and `reminder`; an
`offer` additionally carries `deadline_at`, `overdue`, `overdue_seconds`, the
offering lane's `head` commit, the `reservations` array that moves to the
acceptor, and the `remaining` work from its last report. The same `offer`
record appears on the `status` document.
`deadlines show --json` reports `root` and the recorded `deadlines` defaults.
`budget show --json` reports `root` and the recorded `budget` defaults. `participants` reports
`root` and a `participants` array carrying `participant`, `identity`,
`provider`, `credential`, `branch`, `lane`, `paused`, `wake` and `budget`.
Each `status` lane and each `top` row carries `over_budget` and a `budget`
record with `limits`, `used`, `share`, `crossed` and `over`.

These field names carry the same stability promise as the command-line flags:
removing a field is a breaking change, and a release that adds one raises the
schema version only when an existing field changes meaning. `events export`
stays JSON Lines, one record per line, because it is a stream rather than a
snapshot; `--json` snapshots and that stream are separate contracts.

### Metrics a monitoring stack can read

`agent-parley metrics` prints the counters and gauges the live view computes,
in the Prometheus text exposition format:

```sh
agent-parley metrics
agent-parley metrics --json
agent-parley metrics --provider codex --since 6h
agent-parley metrics --output /var/lib/node_exporter/parley.prom --every 30
```

Every lane series is labelled `project`, `participant` and `provider`, and
every project series `project`. The lane families are
`agent_parley_lane_session_alive`, `agent_parley_lane_branch_drift`,
`agent_parley_lane_issues_held`, `agent_parley_lane_offers_pending`,
`agent_parley_lane_mail_unread`, `agent_parley_lane_mail_pending_ack`,
`agent_parley_lane_leases_held`, `agent_parley_lane_leases_stale`,
`agent_parley_lane_idle_seconds`, `agent_parley_lane_context_bytes_total`,
`agent_parley_lane_hook_events_total`,
`agent_parley_lane_hook_denials_total`,
`agent_parley_lane_served_calls_total`,
`agent_parley_lane_served_rejections_total` and
`agent_parley_lane_tokens_total`. The project families are
`agent_parley_project_participants`, `agent_parley_project_idle_seconds`,
`agent_parley_project_context_bytes_total`,
`agent_parley_project_hook_events_total` and
`agent_parley_project_hook_denials_total`, each summed over the rows that
project reports, so a total never counts a lane the export does not show.
`agent_parley_lane_hook_denials_by_cause_total` splits a lane's denials by
`reason` and `tool`, so a refusal over a reserved path or an expiring offer
reads apart from a policy refusal.
`tokens` is what the native client counted, not billed spend, exactly as the
`TOKENS` column is.

A measurement that could not be read reports no sample rather than a zero:
an unreadable mailbox or session record leaves `mail_unread`, `mail_pending_ack`
or `tokens` absent for that lane, while the family still prints its `# HELP`
and `# TYPE` lines so a reader sees that the metric exists. A label value
carrying a quote, a backslash or a newline is escaped as the exposition format
requires, so a repository path never breaks a frame.

`--provider` and `--since` narrow the export exactly as they narrow the table.
`--json` prints the same values as one snapshot document of kind `metrics`,
carrying `state_directory`, `window_seconds`, `providers` and a `metrics`
array whose records carry `name`, `type`, `help` and `samples`, each sample
carrying its `labels` and its `value`.

`--output PATH` writes the frame to a file by atomic rename instead of
printing it, so the textfile collector of `node_exporter`, or any scraper that
reads a file, never reads a partial frame. `--every SECONDS` rewrites that file
on an interval until the command is interrupted, and needs `--output`. There is
no HTTP endpoint and no new port: the file is the interface, and the
coordination server's loopback listener is unchanged. The command reads the
same records `top` reads, takes no lock and writes no coordination state.

## Participants, providers and accounts

### Availability, reminders and waking

The local service observes each launcher's process identity and native checkpoint
age, and derives one lane state from them. That derivation runs once per reading
and every column reports from it, so the session cell, availability and the
`problems` rows cannot describe the same lane differently in the same frame. The
derived state is `working`, `idle`, `waiting` on a prompt or an approval,
`stopped` when no session process answers, or `unknown` when no trustworthy
process identity was recorded. Each reading carries the evidence it was derived
from and that evidence's age.

A `PreToolUse` that has not yet been closed by its `PostToolUse` counts as work
in flight until the longest tool call the runtime tolerates, so a lane inside a
long command reads as working rather than as the activity before it. Published
activity older than `inactive_after` is reported as stale with its age, not as
the present. A lane whose session process still answers is never described as
stopped: a finished session under a live process is a client waiting for
whoever owns its terminal.

Observed availability is that derived state read coarsely, not a second
derivation. It is one of four values. `active` is a live launcher that is
working or waiting on a prompt; `idle` is a live launcher whose evidence has
aged past `inactive_after`, which is what a lane between turns looks like;
`stopped` is a launcher whose recorded session process is gone. A lane without a
trustworthy native process identity is `unknown`, because it may still be a live
session. A lane that is only idle is never reported with the word a dead
launcher gets. `status` reports that state beside process liveness and lists
outstanding acknowledgement IDs, senders and ages.

A lane that has recorded no native activity yet has no age to report, so
`last_active_at` and `age_seconds` are both `null` rather than an age measured
from the Unix epoch, a live launcher without a checkpoint reads as `active`
instead of ageing into `idle` on its first poll, and a `problems` row for such
a lane prints `-` in the age column and sorts below every row whose age is
known.

Sending an `ack_required` message returns an availability warning when the
latest runtime observation puts its recipient in either non-active state. An
idle recipient reports `state` `idle` with the summary
`queued for NAME (idle; wake requested)`, because the next supervision poll
asks an idle lane holding a backlog to take its turn. A recipient whose
process is gone reports `state` `unreachable` with the summary
`queued for NAME (unreachable)`. A presence row written before this release
still carries `unreachable` and is read as `stopped`. Observed availability is
separate from last coordination and never changes claims. An `unknown` recipient
reports that native process identity is unavailable and requires manual
attention; the service does not wake or resume it.

The private project manifest accepts `"supervision"` with `interval` (default
30 seconds), `inactive_after` (300 seconds), `start_deadline` (30 seconds),
`completion_reminders` (3 reminders, 1 to 100), `orphan_retire_after`
(3600 seconds), `claim_idle_after` (3600 seconds), `takeover_grace`
(300 seconds), `max_claims_per_lane` (2 claims, 1 to 100), `prompts`, `wake`,
`reclaim` and `titles` (all true). Numeric second values range from 1 to 86400 seconds.

Claim liveness is measured per claim, not per lane. A claim advances on its
own generation start and on `agent-parley report ... --issue N` naming it; a
lane holding a single claim also advances it with every tool call. `issue list`
prints `last progress Ns ago` beside each claim. A claim with no progress for
`claim_idle_after` takes the overdue path even while its holder is busy with
other work: one notice to the holder and to the lanes whose issues wait on it,
then an offer to the fittest peer below the claim cap with the recovery
checkpoint attached, then release once that offer expires. Each step is
recorded in history and counted in `attempts`. Blocked and ready work is
waiting rather than idle and is not moved. `issue claim` refuses a lane that
already holds `max_claims_per_lane` claims and names each held claim with its
progress age. `issue request N [--summary REASON]` asks the holder to hand the
issue to the requesting lane; the holder answers with `issue accept` or
`issue decline` and the request identifier, and a holder that neither answers
nor records progress on the claim within `takeover_grace` has the request
granted by the supervisor as an offer to the requesting lane, whose acceptance
moves ownership.
The same keys in
`$AGENT_PARLEY_HOME/supervision.json` set global defaults; global false values for
`wake`, `prompts` and `reclaim` cannot be enabled by a project. A participant
entry may set
`"wake": false` to opt out individually. These settings remain outside source.

`agent-parley run NAME` publishes the lane's activity as `starting; awaiting
native hook` before it starts a client, and the client's first hook event
replaces that label. A client parked on a native trust, authentication or update
dialog fires no hook, so `start_deadline` bounds how long that label may stand.
A launch still carrying it `start_deadline` seconds later is published as
`not started; no native hook`, with the deadline and the observed wait in the
lane's private activity state, and the lane state moves to `blocked: dialog`
with the evidence `it never started within Ns of its launch`. The mark is an
observation: nothing is killed, no claim moves and no dialog is answered. The
blocked state fails the lane's session fitness check, so such a lane is neither
offered work nor named as a share target for a peer's rebalance, and a wake is
deferred with the cause `blocked: dialog` rather than spending an attempt on a
client that cannot read an injected prompt. Answering the dialog produces the
first native hook event, which moves the lane state out of `blocked` and clears
the mark however late it arrives. Client startup on a developer machine is a few seconds;
raise `start_deadline` on a slow machine or a cold cache, where a legitimate
launch can take longer than the default.

Releasing a claim with waiting peers creates a visible handoff reminder.
The service also checks each claimed issue on the forge and reminds the holder
when the issue closed inside the current claim, recording the pull request that
closed it, its head branch and merge commit, whichever branch it came from. A
closing pull request from another lane's branch is named in the reminder, and
so is one from a per-issue branch that exactly one other lane's worktree
checked out. Every lane pushes as the same forge account, so the pull request
author cannot tell lanes apart; the worktree's own HEAD reflog can. A branch
no lane's reflog moved to, or several did, is attributed to nobody. Only
when the forge cannot say anything about the issue does the newest pull request
on the lane branch speak for it, and a lane branch merge never marks a claimed
issue the forge still reads as open. Issue readings are reused for five
minutes. Forge lookups are bounded and best effort; an offline forge cannot
establish completion. Reminders appear in issue/status output and
at checkpoints. An explicit subsequent message reaching every waiting peer
marks a response observed; that is delivery evidence, not proof of a complete
handoff. Ownership still moves only through the explicit offer/accept protocol.
A `pull request ended` reminder is marked answered as soon as its holder no
longer owns the issue, whether it released, handed off, was reclaimed or was
resolved, so the former holder is not woken, sent or listed a reminder for
work it no longer holds.

Repeating a reminder at a lane that has stopped answering changes nothing, so
the supervisor counts the reminders left unanswered on a claim observed
complete. The reminder is written once and a silent lane is asked
again at most once per `inactive_after` window, so the count is one plus the
whole windows elapsed since the reminder was written, never the number of
polls. With the defaults a claim escalates about ten minutes after its first
reminder. Past `completion_reminders` it records the
claim as an unresolved completion once, which `status` carries on the claim and
`problems` lists for the operator. A holder that answers before the threshold
clears its own escalation. Nothing moves on the marker: the issue keeps its
owner, its offer and its reservations, and the escalation goes to the operator,
never to the other lanes.

`agent-parley issue resolve NUMBER` is the terminating transition for such a
claim. It reads the forge at that moment, refuses unless the issue closed, or,
when the forge cannot say, a merged or closed lane branch pull request was
opened, inside the current ownership generation, and refuses a
claim the supervisor has not escalated, so an answering holder is never
resolved out from under it and an unverified claim is never ended this way. It
records the branch, the pull request state, its merge commit and the instant it
was observed beside the operator's reason, and history shows a `resolve` by
`operator`, never a completion filed by the lane. A merged pull request records
the work complete and frees the issues waiting on it; a pull request closed
without merging integrated nothing, so `--release` is required there and
returns the work to the queue.

For eligible idle sessions, the launcher owns a native pseudo-terminal and a
private wake socket. It admits a coordination prompt at a native idle checkpoint,
with no partially entered operator input. When supervision selected an actionable
work offer, the launcher reads that revalidated selection from private state
after admission and names its offer and issues in the turn. Injecting it is
delivery, not a claim or completion. Approval prompts and active turns refuse
injection. A stopped session can resume its recorded session ID through the same
native launch configuration and an interactive terminal;
`agent-parley run NAME --resume` exposes that operation explicitly. Native trust,
authentication and permission prompts remain in force. Environment-only vendor
accounts that cannot be reconstructed safely require manual attention.

Wake attempts are separated by the inactivity interval and capped at three for
each unchanged backlog. Work dispatches add their offer generation and
issue-scoped progress digest to that backlog. Delivery without a claim, handoff
or other recorded issue progress leaves the obligation pending. Exhaustion
records the offer, issues, attempt count, last result and operator action in the
work publication. A `busy:turn`, `busy:input`, or `busy:repeat` answer is not
one of the three. The suffix distinguishes an active turn, pending operator
input, and an accepted wake that produced no later checkpoint. After the
inactivity interval, a repeated checkpoint admits one retry. A second stalled
wake reports `manual attention required`. Terminal control replies such as
cursor position reports and focus events do not count as partially entered
operator input, so they do not refuse the wake. Complete replies are removed
from the input-state check without hiding operator bytes that arrived in the
same read; incomplete replies are carried until the next read and refuse a wake
until they complete.
Wake attempts, backoff, the last result and escalation are fields of the
lane's state in the store, and every wake decision reads them there. Results
appear in `status`, the retained event log and private `<name>-wake.json`,
which is a published copy of those fields that no decision reads; resumed terminal output stays in `<name>-wake.log`,
which the launcher truncates before a write would take it past 1 MiB. A failed
write to that log, including a full disk, is dropped and never ends the resumed
session. A wake request whose lane activity record is missing or unreadable is
answered `unknown` rather than with an empty reply.

The launcher ends a session when the client exits, even while a background
process the client started still holds the terminal. `SIGTERM` and `SIGHUP`
to the launcher run the same cleanup as a normal exit (terminal restored, wake
socket removed, lane published `stopped`) and are then forwarded to the client.
A client binary that cannot be started exits the launcher with status 127.

An attached `agent-parley run` owns its terminal tab title. The title leads
with the lane name, then one state word (`working`, `idle`, `idle with claim`,
`blocked: dialog`, `blocked: approval`, `starting`, `stopped`), the lowest
issue number the lane holds and its progress as `done/total`, or `0 open`, for
example `[claude-a] idle with claim #412 - 2/5 done`. The title the client sets
for itself follows after ` | ` and is truncated first. The title is refreshed
when the lane's activity record or the issue ledger changes or a dialog holds
the screen, and the terminal's previous title is restored on exit. Set the
supervision key `titles` to false to keep the client's native title.

`agent-parley title` prints the same line for the lane that contains the
current directory, without the client's title, and prints nothing outside a
lane. It reads only the lane's private records, so a status line can run it on
every refresh. Per provider:

- Claude Code: add a command status line to the user settings
  (`~/.claude/settings.json`) or the project's `.claude/settings.local.json`:

  ```json
  {"statusLine": {"type": "command", "command": "agent-parley title"}}
  ```

  The command runs in the session's directory, so every lane shows its own
  line and sessions outside a lane show an empty one.
- Codex, Gemini CLI, GitHub Copilot CLI, OpenCode and Amp: not supported. Agent
  Parley configures no command-driven status line for them; their lanes carry
  the summary in the tab title only.
Lanes launched before wake sockets were introduced require relaunching. A live
native session started outside `agent-parley run` has no wake socket. Exit that
session and launch it through `agent-parley run` before automatic waking can
reach it. Generated hooks record the native foreground process identity when
the operating system exposes one, and otherwise the client the lane's recorded
launcher started, so a provider that gives its hooks no controlling terminal
still leaves a lane eligible for work. If neither reading establishes an
identity, the lane remains `unknown` rather than being resumed into a possibly
live session. An unavailable adapter
or socket is reported for manual attention.
Waking never marks mail read, acknowledges it, releases reservations or
transfers an issue.

The automated tests exercise local processes, pseudo-terminals, hook payloads
and real MCP transport. They do not establish live model behavior for a provider.

```sh
agent-parley provider list
agent-parley credentials add account-1 --config-home ~/.claude-account-1
CLAUDE_CONFIG_DIR=~/.claude-account-1 claude
agent-parley participant add claude-1 --provider claude --credentials account-1
agent-parley run claude-1 --task "Work on issue 44"
agent-parley provider add vendor --adapter claude --executable claude \
  --home-env CLAUDE_CONFIG_DIR --env ANTHROPIC_BASE_URL=https://vendor.example \
  --require-env ANTHROPIC_AUTH_TOKEN
```

The third line is the native CLI signing in to that account's directory once;
nothing here records a token. `--provider` defaults to the participant name, so
name it whenever the participant is not named after its provider, and the
provider and profile a participant is created with are fixed for that
participant's life. [Providers](providers.md#accounts) carries that model, the
second-account walkthrough, the `participant list` verification line and the
retire-and-add-again route for a binding that is already wrong.

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
explicit provider definitions are preserved: a local definition named `gemini`
that rides the `claude` or `codex` adapter through a vendor endpoint shadows
the native preset and keeps working unchanged, `provider list` prints the
definition that is in force, and `provider remove gemini` reveals the native
preset again. Nothing rewrites such a definition. The integration follows
Gemini's [configuration](https://geminicli.com/docs/reference/configuration/)
and [hook contracts](https://geminicli.com/docs/hooks/reference/). Gemini has
no `PermissionRequest` hook, so `provider list` reports it under
`unavailable_hooks`; approval prompts remain Gemini's own.

The overlay is written under the private state root as
`<participant>-gemini-settings.json`, never into the repository or the native
directories, and is rebuilt from the native policy on every launch, so a
corrupt or stale overlay left by a crash is replaced rather than reused.
`participant retire` deletes it. A native policy that disables hooks refuses
the launch with that reason instead of starting an unguarded session.

The `opencode` preset starts the native OpenCode CLI. OpenCode reads
`opencode.json` from the directory `OPENCODE_CONFIG_DIR` names and loads
JavaScript plugins from `plugin/` beside it; it has no hook command contract.
The launcher copies that directory's `opencode.json` into a lane-private
directory `<participant>-opencode/` under the private state root, adds the
lane's server under `mcp` as a `remote` entry whose bearer header is
`{env:AGENT_PARLEY_TOKEN}`, writes `plugin/agent-parley.js`, and points
`OPENCODE_CONFIG_DIR` at the copy for that launch only. Every other key is
carried unchanged and the source directory is never written. OpenCode keeps
its credentials in its own data directory, outside the configuration
directory, so authentication is untouched; a credential profile may select
`OPENCODE_CONFIG_DIR` to choose the configuration that is copied. A
configuration that already defines `agent_parley`, or that exists only as
`opencode.jsonc`, refuses the launch with the fix named. The overlay is rebuilt
on every launch and removed by `participant retire`.

The plugin maps OpenCode's plugin events onto the shared checkpoint events:
`session.created` to `SessionStart`, `chat.message` to `UserPromptSubmit`,
`tool.execute.before` to `PreToolUse`, `tool.execute.after` to `PostToolUse`,
`permission.ask` to `PermissionRequest` and `session.idle` to `Stop`. For each
it spawns the configured hook command with `--adapter opencode`, and
`opencode.py` translates the event and the `agent_parley_*` tool names for the
shared parser and flattens the result to `decision`, `reason` and `context`.
A denied `tool.execute.before` is raised as an error inside the plugin, which
is how an OpenCode plugin refuses a tool call; a blocked `session.idle` posts
the reason back into the session as the next prompt; `context` is appended to
the user's message parts. The plugin never sets a `permission.ask` status, so
OpenCode's own approval prompt is left to the user. OpenCode raises no end of
session event, so `SessionEnd` is reported under `unavailable_hooks`. Resume
passes the recorded session as `--session ID`. Plugins in the user's global
`plugin/` directory are not carried into the overlay; project `.opencode/`
configuration still applies because OpenCode reads it from the worktree.

Credential profiles point a provider's config-home variable at a separate
directory so one provider can run under several accounts. Define one profile per
subscription; there is no limit besides the 32-participant project cap, and the
accounts need no relationship to each other. Sign in to each directory with the
native CLI once. Agent Parley stores directory paths and
variable names; it never stores tokens or keys, and rejects `--env` values whose
names look like credentials. A shell alias or wrapper function is not an
account: the launcher resolves a provider's executable on `PATH`, so an alias is
never seen, and a profile is the supported way to say the same thing.

### Reclaiming landed lanes

Every lane owns a worktree in the private project state directory and a branch
in the repository, and both outlive the claim they were created for. The
service sweeps them at most once every 900 seconds, after the rest of a poll,
and `agent-parley gc` runs the same sweep on demand: without `--apply` it
reports what it would do, with `--apply` it removes what it may. The outcome
of the service's own sweep is published in `reclaim.json` in the project state
directory, so the next sweep is bounded even when one fails.

A lane is reclaimed only when every one of these holds: its worktree is a
registered worktree directly inside this project's state directory; no session
is running in it; the ledger records no claim it still owns; it has nothing
uncommitted; the base checkout's head already carries every commit on its
branch; its branch carries nothing its configured upstream lacks; its branch
has moved at all since the lane was created; and the forge reports the newest
pull request from that branch as merged, or, with no pull request to read, the
upstream no longer carries the branch. Removal is the ordinary retirement,
followed by `git branch -d`, which deletes the branch under Git's own
merged-branch rule and refuses otherwise.

Anything else is kept and reported with the one condition that held it, and
uncommitted files and unmerged or unpushed commits are reported by name. A
pull request closed without merging, an open one, an unreachable forge, a
worktree Git cannot inspect and a path outside the project's own lanes all
decide against reclaiming. The sweep never touches a remote branch, and it
never fails because the remote branch is already gone.

A lane that never did any work is retired too. When its runtime state reads
`stopped`, its branch is still at the project base, it has nothing
uncommitted, the ledger records no claim or pending offer for it, it holds no
reservation, and neither its last activity nor its worktree changed inside
`inactive_after`, the sweep retires it with reason `stopped`. A lane already
retired whose worktree is gone is dropped with reason `vanished`, which
takes it out of `status` and `top`. Retiring a lane supersedes the mail still
addressed to it, so no share bounces off a lane that no longer exists.

The lane sweep does not let a session that is alive but idle past
`inactive_after` hide the lane's real state. Such a lane is judged on every
other condition, so the report names what actually holds it, such as its
claim or its uncommitted files. A lane that would otherwise be reclaimed is
still kept, because no worktree is removed from under a live process, and
every such line names `agent-parley participant stop NAME` as the command
that ends the idle session.

Worktrees a lane made for itself, such as one per pull request, are swept in
the same pass and published under `worktrees` in `reclaim.json`. Every
worktree `git worktree list` reports is attributed to a lane by path, when it
sits inside a lane's worktree, or by branch, when its branch is the lane's
name or branch followed by `-` or `/`, such as `claude-pr-12`. One inside the
project state directory belongs to the project. A worktree nothing accounts
for is reported with reason `no lane made it` and never touched, even with
`--force`.

An attributed worktree is removed with `git worktree remove` when it is not
locked, has no session running in its lane, has nothing uncommitted, and
every commit it holds beyond the base checkout is on its upstream; and then
only when its head is already on the base, its lane retired, or it has not
changed inside `inactive_after`. A registration whose directory is gone is
dropped. Its branch is kept. Uncommitted changes, unpushed commits and a
recent change keep it and are reported by name. `agent-parley gc --apply
--force` removes those too, but only after writing a recovery checkpoint
bundle of the whole worktree, index and untracked files included, to the
project's `recovery` folder; a checkpoint that fails removes nothing.
`agent-parley gc`, `gc --dry-run` and its alias `agent-parley reclaim`
without `--apply` add each worktree's size on disk.

Each sweep also records the project state directory's size and how many
worktrees a reclaim, and a forced reclaim, would still remove.
`agent-parley status` prints them under the project heading and carries them
as `reclaim` in its JSON document, reading the sweep's record instead of
walking the disk.

When the project root itself disappears, the first poll records
`root-missing.json` in the project state directory. One interval later every
lane still registered is captured for recovery, retired and has its
reservations revoked. The marker names, per lane, the claims released and the
checkpoint each one left, and the state directory for the operator to remove.
The project then leaves `status`, `top` and `metrics`; a root that
returns clears the marker on the next poll. When the service starts, it
removes every wake socket in its home that no launcher is listening on.

## Other agent CLIs

Agent Parley hands the native CLI its MCP server, the coordination prompt and
its lifecycle hooks at launch. Six contracts implement that, and `--adapter`
names the one to use:

| Adapter | MCP server | Coordination prompt | Hooks |
| --- | --- | --- | --- |
| `claude` | `--mcp-config FILE` | `--append-system-prompt TEXT` | `--settings '{"hooks":…}'` |
| `codex` | `-c mcp_servers.agent_parley.url=…` | appended to the prompt argument | `-c hooks.EVENT=…` |
| `copilot` | `mcp-config.json` in the lane's `COPILOT_HOME` | prepended to the `-p` argument | `hooks` in `settings.json` there |
| `gemini` | `mcpServers` in the lane-private system settings overlay | prepended to `--prompt-interactive` | `hooks` in that overlay, `--adapter gemini` |
| `opencode` | `mcp` in the lane-private `opencode.json` | prepended to `--prompt` | `plugin/agent-parley.js` in the lane-private directory, `--adapter opencode` |
| `amp` | `amp.mcpServers` in the lane-private `settings.json` | prepended to the prompt argument after `--settings-file` | `amp.hooks` in that file, `--adapter amp` |

`agent-parley provider list` prints `unavailable_hooks` for every definition,
naming the shared events that adapter's CLI cannot raise. A launch refuses an
adapter that cannot deliver `SessionStart`, `PreToolUse` or `Stop`, with the
missing events named, rather than starting a lane whose branch, claim and
turn guards would silently never run.

The same listing prints `delivery`, naming how coordination reaches a lane
driven by that adapter. `hooks` means a native lifecycle event carries mail,
handoff notices, operator edits and reservation state into the session at a
turn boundary, which is every adapter that raises `SessionStart`,
`UserPromptSubmit`, `PreToolUse` and `Stop`. `polled` means the adapter is
missing at least one of those, so the launcher runs a delivery thread beside
that session instead: it reads the same mailbox the served checkpoint reads,
on the interval `AGENT_PARLEY_DELIVERY_SECONDS` sets (20s by default, clamped
to 0.05s-600s), and publishes what is undelivered into
`STATE/PROJECT/NAME-delivery.md`, which is lane-private and outside the
target repository. The lane's coordination prompt names that file and tells
it to read it at every turn; a file that cannot be written falls back to a
notice on the launcher's terminal. Each delivery is written to the same
participant event log a served checkpoint records into, under the reason
class `polled_delivery`, so `agent-parley top` counts its bytes in `CONTEXT`
like any other lane's.

Polled delivery is delivery, never enforcement. It cannot deny a tool call,
cannot hold a turn open, and reaches the lane only as text that lane has to
read, so an adapter missing a required guard is still refused at launch. It
carries no budget threshold notice, it never advances a paused lane's mail,
and it is bounded by the same context budget the hook path uses, so a large
batch of mail arrives over several intervals.

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

A relaunch appends no duplicate hook; `participant retire` removes only that
lane's hook entries from the profile's `settings.json`, leaves the operator's
own and other lanes' entries in place, and drops the `agent_parley` server
from `mcp-config.json` once no lane hook remains. A lane that crashed leaves
its entries until it is relaunched or retired; they point only at that lane's
private state.

### Stub verification and live trials

Every adapter's launch path and hook wire contract is verified by tests that
run the real launcher against a stub executable, read the configuration the
launcher generated, register through the local MCP service and run the exact
configured hook command with native-format payloads, covering session
identity, denial output, approval requests and resume without a recorded
session. That verifies argument handling, file handling and hook translation.
It does not verify live model behaviour, and the following points need a live
trial with the native CLI installed and signed in:

| CLI | Verified against a stub | Needs a live trial |
| --- | --- | --- |
| Gemini CLI | overlay contents, `GEMINI_CLI_SYSTEM_SETTINGS_PATH` selection, every `hooks` event runs the command, denial and context schema | that Gemini honours `decision: deny` from a `BeforeTool` hook in a system overlay, and `--resume ID` |
| Copilot CLI | `mcp-config.json` and `settings.json` merge, PascalCase event payloads, flat result schema, retire cleanup | that a `PreToolUse` `permissionDecision: deny` stops the tool, and `--resume ID`; #173 tracks the payload contract |
| OpenCode | `opencode.json` copy and `mcp` entry, plugin file and its embedded command, event and tool-name translation, `--session ID` on resume | that OpenCode loads `plugin/agent-parley.js` from `OPENCODE_CONFIG_DIR`, that `{env:AGENT_PARLEY_TOKEN}` is substituted in `headers`, that a thrown error in `tool.execute.before` refuses the call, and that `client.session.prompt` delivers a blocked-stop reason |
| Amp | `settings.json` copy, `amp.mcpServers` entry with the literal bearer token, one `amp.hooks` entry per tool event and its command, field and event translation, `reject` on refusal, `threads continue ID` on resume, retire cleanup, the required-guard refusal | that Amp accepts `--settings-file` with a prompt argument, that `amp.hooks` entries with `event`, `command` and `args` run around tool calls and read `{"action":"reject","reason":…}` from stdout, the input field names for the tool, its input and the thread, that a remote `amp.mcpServers` entry honours `headers`, and that no thread start or idle hook exists; the `amp` lane stays refused until that last point is disproved |

Record the outcome of a live trial in the issue that requested it, with the
CLI version, and file a gap as its own issue. Operator recovery after a lane
loses its session is #153 and saved-session recovery is #175; both apply to
every adapter, since each one records the native session identifier the same
way through `SessionStart`, or through the first tool hook for `amp`, whose
thread identifier arrives with every tool event.

The `amp` preset starts the native Amp CLI. Amp reads one `settings.json`,
from `~/.config/amp` or the file `AMP_SETTINGS_FILE` or `--settings-file`
names, holding MCP servers under `amp.mcpServers` and hook commands under
`amp.hooks`; a credential profile may point `AMP_SETTINGS_FILE` at the file,
or the directory holding it, that is copied. The launcher copies it to
`<participant>-amp-settings.json` under the private state root, adds the
lane's server with its bearer header written literally, since Amp substitutes
no environment references in settings and the identity file beside it already
holds the token, appends one hook per supported event, and passes the copy
with `--settings-file` for that launch only. Every other key is carried
unchanged and the source file is never written; Amp keeps its credentials in
its own store, so authentication is untouched. Settings that already define
`agent_parley` refuse the launch. The overlay is rebuilt on every launch and
removed by `participant retire`, so a stale copy left by a crash is replaced.

Amp's hooks fire around tool execution only: `tool:pre-execute` maps to
`PreToolUse` and `tool:post-execute` to `PostToolUse`, each spawning the hook
command with `--adapter amp`. `amp.py` reads the tool name, input, result
and thread identifier under Amp's field names and flattens a refusal to
`{"action": "reject", "reason": …}`; any other result is an empty object, so
the hook never allows a call on the lane's behalf and Amp's own approval
prompt decides. Amp raises no thread start, prompt, approval, idle or end
event, so `SessionStart`, `UserPromptSubmit`, `PermissionRequest`, `Stop` and
`SessionEnd` are reported under `unavailable_hooks`, and because two of
those are required guards, `agent-parley run amp` is refused with one
sentence naming them rather than started without branch and turn guards. Its
`delivery` therefore reads `polled`: the moment Amp raises those two events,
that lane launches and its mail arrives on the delivery interval rather than
at a turn boundary, because the events that would carry it are still absent.
Resume passes the recorded thread as `threads continue ID`; the thread is
recorded from the first tool hook, so a lane that never reached a tool call
has no session to resume and the launcher says so.

| Agent CLI | MCP configuration | Lifecycle hooks | Per-account config home |
| --- | --- | --- | --- |
| Gemini CLI | lane-private system settings overlay | translated native hooks | `GEMINI_CLI_HOME` |
| Copilot CLI | `$COPILOT_HOME/mcp-config.json`, or `copilot mcp` | `hooks` in `$COPILOT_HOME/settings.json` | `COPILOT_HOME` |
| OpenCode | `mcp` in the lane-private `opencode.json` | `plugin/agent-parley.js` spawning the hook command | `OPENCODE_CONFIG_DIR` |
| Amp | `amp.mcpServers` in the lane-private `settings.json` | `amp.hooks` in that file, tool events only | `AMP_SETTINGS_FILE` |

Copilot CLI, Gemini CLI, OpenCode and Amp have native adapters. Amp does
not accept `--append-system-prompt`, and its settings arrive through
`--settings-file` rather than `--settings`, so naming it as a `claude`
provider executable still fails; use the `amp` preset.

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

### Exporting and importing the state directory

```sh
agent-parley state export --output parley-2026-09-15.tar.gz
agent-parley state export --output one-project.tar.gz --project ~/src/app
agent-parley state show parley-2026-09-15.tar.gz
agent-parley state import parley-2026-09-15.tar.gz
agent-parley state import one-project.tar.gz --merge --project ~/src/app
```

`state export` writes the coordination state as one tar archive. The store is
copied through the SQLite backup interface while the setup and ledger locks of
every exported project are held for at most two seconds, so the snapshot, the
project manifests, the issue ledgers, the activity files, the retained event
and report logs and the attachments describe one moment; an export that cannot
take those locks in time is refused rather than written inconsistently.
`--project ROOT` exports one registered project alone, including only its rows
of the store. The archive manifest names the store schema, the export time,
the SHA-256 digest of every member, the projects and their participants.

Credentials never leave the state directory. Registration tokens are stripped
from the identity records, their digests are cleared in the exported store,
and credential profiles and native MCP configurations are not archived; the
manifest states the omission. `state show PATH` prints the projects,
participants, issue counts and export time from that manifest without
restoring anything.

`state import PATH` restores into an empty state directory and refuses one
that already holds a store or a project unless `--merge` is given. Before it
writes, it validates every member against the manifest digests, refuses an
absolute path, a `..` component, a symbolic link or a hard link, and refuses
an archive written at a newer store schema than this build reads. The archive
is unpacked into a temporary directory inside the state root, migrated to
this build's schema there, and moved into place only after that validation.
With `--merge`, a project the directory does not hold is added beside the
existing ones; a project that already exists is a collision, so the import
refuses it and leaves the directory as it was. `--project ROOT` restores one
archived project alone.

Every imported participant registers again on its next `run`, because the
archive carries no credential. The import then lists every participant whose
lane path does not exist on this machine. Those lanes are not recreated: the
operator adds the worktree again, on the recorded branch, before launching
that participant.

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

### Selecting the forge

Issue titles, assignee mirrors and report comments go to the forge the project
manifest records. Registration detects it: a repository carrying a Beads ledger
in `.beads/` selects `beads`, any other selects `github`. The choice is per
project and can be set by hand:

```sh
agent-parley forge show
agent-parley forge set beads
agent-parley forge set null
```

`github` speaks through your own `gh` login. `beads` speaks through the `bd`
CLI: `bd show ID --json` for titles, `bd update ID --assignee` for the claim
and release mirrors, and `bd comment` for reports. `null` keeps issue numbers
bare, calls nothing and never fails. Every forge exchange stays best effort:
the ledger is written first and decides ownership, and a forge that is missing,
offline or unwilling changes nothing. Only `github` opens pull requests, so
`participant pr` refuses in one sentence under `beads` or `null` before it
pushes anything.

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

## Requiring a recorded approval

A repository can require your own recorded decision before either command that
carries a lane's work out of its worktree:

```sh
agent-parley approval show
agent-parley approval set merge pr
agent-parley approval set
agent-parley approve claude-1
agent-parley reject claude-1 'Needs a test for the retry path'
```

`approval set` records the requirement in the project manifest beside the
roster, outside the target source tree, and takes any of `merge`, `pr`, both,
or nothing at all. A repository with nothing required integrates exactly as
before.

With a step required, `participant merge` and `participant pr` refuse until a
decision for that lane's current ready report is recorded, and the refusal
names the report and the `agent-parley approve NAME` that grants it. The
decision is bound to the identifier of that report, the exact commit the lane
branch points at, the branch and base it targets, the repository root, and a
digest of the verification command, the pull-request policy and the approval
requirement in force. Anything in that binding changing invalidates the
decision, so new commits invalidate it even when the lane never reports again,
and the refusal says which of those changed. The binding is read again
immediately before the merge or the push, while the lane's session exclusion is
held, so an approval recorded for earlier commits cannot carry a later head
into the base repository or the forge. A decision log that cannot be read, or
that holds a damaged record, refuses integration rather than treating the
missing decision as consent.

`reject` requires a reason, records it, and delivers it to the lane as operator
mail. It gates nothing else: the lane keeps its session, its claims and its
work, and can go on committing. Only these two commands are refused, and only
until a further decision is recorded.

Both commands run from the base checkout and refuse to run inside an assigned
worktree, so no lane records the approval of its own work through them. That is
this tool's command-line boundary, not an operating-system one: a program
running under your account can write coordination state directly. Separate the
operator from the lanes as different operating-system users, or in different
containers, when that distinction has to hold. The decision also records that a
named local account decided, not that the code is correct. The verification
command, the attribution scan and GitHub's own checks all still run unchanged.

`status` prints `awaiting approval`, `approved` or `rejected` beside a lane's
ready report, naming what invalidated an earlier decision; `top` counts the
lanes awaiting one in its header; and `history --kind approval` lists the
decisions, their operator and their reasons with the rest of the chain.

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
normal exit signals it, and waits a bounded time for it to leave. A process that
ignores that signal, such as a client wedged in a native dialog or left behind by
a system hang, is sent `SIGKILL` through the same pinned identity. The session
record is cleared only once the process is verified gone; a process that
survives both signals is reported and its record kept. Identity is the
recorded process ID together with its kernel creation time, checked before
signalling and again inside the platform's terminate step, so a recycled process
ID is never signalled. The command-line check that recognizes the coordination
server does not apply here, because a lane runs a native client rather than this
package. Claims and reservations stay owned and the command prints what the lane
still holds, so an operator moves that work deliberately. A stop that finds no
running session is still recorded.

`participant restart NAME` starts a lane again. It refuses while a current
session is alive, because two clients in one worktree would fight over it. A
session whose process is alive but whose evidence is older than
`inactive_after` is a wedged client and is ended first through the same
escalating stop. It refuses a lane that is not on its assigned branch. A dirty
worktree is what a crash leaves behind, so it is not refused: every claim the
lane owns is captured into a recovery checkpoint, the files stay exactly where
they are, and the new session's opening task names the checkpoints. Nothing here
resets, cleans, stashes or force-switches. It replays the recorded lane
initialization command when one exists, then launches the same provider and
credential profile as the previous run.

After a host restart every recorded session is dead, even when its process ID
now names an unrelated process. The supervision loop records the host's boot
identifier in the project state directory; when it changes, each lane with a
recorded session has its state record moved to `stopped` with the restart as
its evidence, and is published as stopped and marked with the session the
restart ended. That lane is never resumed or signalled, its claims move
through the orphan path, `participant stop` reports no verified session, and
`participant restart` launches it without hand edits to the state directory.

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
    "body_template": "## Review checklist\n\n- [ ] Reviewed",
    "self_service": false
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

`self_service` is the repository's standing authorization for a lane to run
`participant pr` for its own work from its own worktree. It is `false` unless
the project sets it, and with it off nothing changes: `participant pr` stays an
operator command that excludes a live lane session. With it on, that one
command, run inside the lane it names, is admitted while every condition holds:
the lane reported `ready`, the project configures a verification command, the
lane still sits on its assigned branch, and no peer holds an advisory
reservation over a path the branch changed. The gate itself still runs in the
lane during the push, so the pull request is opened after a green gate and not
merely after a claim of one. A condition that does not hold refuses the command
and names that condition; unreadable reservation state is a refusal too, since
it rules no overlap out. Advisory reservations stay advisory: an overlap
withholds this unattended step, it does not deny anyone access to a file.

A self-opened pull request carries what authorized it. The recorded review
evidence names the policy, the participant, the gate command, how many changed
paths were cleared, the peers holding reservations at the time, and the branch,
both in the pull-request body and in the integration record kept in private
project state. `participant merge` remains operator-only under every setting,
and authentication is still the native `gh` CLI's own, with no added flag.

`participant retire` removes one lane: it refuses while a session is running or
the worktree is dirty, removes the worktree, invalidates that participant's
coordination credential, and drops its manifest entry. The branch is deleted
only when it adds no commits to the project base; otherwise the branch is kept
and named in the output. Message history is always preserved, so past handoffs
still resolve their sender.

A lane can also retire itself, which is the path to take when the lane is
finished rather than abandoned. The `retire` MCP tool releases the issues that
lane holds, declines the handoffs offered to it and tells each lane that had
handed it work, releases its advisory reservations and grants any key a peer was
queued for, removes its worktree when Git reports it clean, and invalidates its
credential. Unlike `participant retire` it keeps the manifest entry, marked with
the time it retired: `status` and `top` show the lane as `retired AGE ago`, the
JSON views carry `retired_at`, and the service neither wakes it nor names it in
a work offer. A lane whose worktree is dirty keeps it, and the changed paths are
reported in the tool result. A lane that holds ready work is refused before
anything is released, naming those issues: ready work stays claimed until
`agent-parley merge LANE` lands it, or the lane offers it to a peer. Return
that lane to service with the same
`participant add NAME` command that created it, which restores its worktree on
its own branch; the next launch registers a fresh credential.

Mutations and reports run from the assigned lane. The owner pauses offered work
until acceptance, decline, or cancellation. Use `issue decline`, `issue cancel`,
and `issue release` explicitly; release does not close a GitHub issue. Partial or
blocked reports require `--remaining` instead of `--evidence`, and `--backlog`
states how many units of work the claim still has left.

`up` starts the detached service; `down` stops its verified process and retains
state. Default state is `~/.local/state/agent-parley`, mode 0700. Logs are in
`server.log`, with the previous window in `server.log.1`; repeated per-lane
`undecided` and `unanswered` entries are coalesced to one per minute. Set `AGENT_PARLEY_HOME` or pass `--home` for another private root.
Set `AGENT_PARLEY_PORT` before first initialization to override port 8876.

`server.json` names a service that answered: `up` publishes it only once the
new process reports itself ready, and removes a record left by a process that
is gone. A start that fails, on an occupied port or a process not ready within
25 seconds, publishes a record with `state: failed`, `failed_at` and the count
of consecutive `failures`, which names no process. A lane launch runs `up`
first, so a lane never starts against a service that is not there, and a hook
that falls back to the in-process decision asks for the service back when the
recorded one has gone or failed, never waiting for the answer. It asks at most
once a minute, and after repeated failed starts the wait doubles per failure
up to 16 minutes. A relaunch stamp dated in the future, as a backward clock
step leaves, does not hold the next request off. A machine that has never started a service is left alone: the first start
belongs to the launch or to the operator.

### Keeping the service across reboots

The service is a user process and does not survive a reboot. Where the machine
should bring it back without a launch, a user-level systemd unit does it.
Write `~/.config/systemd/user/agent-parley.service`:

```ini
[Unit]
Description=Agent Parley coordination service
After=default.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=%h/.local/bin/agent-parley up
ExecStop=%h/.local/bin/agent-parley down

[Install]
WantedBy=default.target
```

Then enable it, and allow it to run while nobody is logged in:

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-parley.service
sudo loginctl enable-linger "$USER"
```

The unit is optional. `up` returns once the service answers and the process it
started is detached, which is why the unit is a `oneshot` that remains after
exit. It starts the same command an operator runs, so the state directory, the
port and the logs are unchanged, and `agent-parley down` still stops the
service by hand. Adjust `ExecStart` to the path `which
agent-parley` reports when the installation is not in `~/.local/bin`, and set
`Environment=AGENT_PARLEY_HOME=...` in the `[Service]` section for a private
root other than the default.

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

`make check` runs `claude plugin validate plugins/agent-parley --json` as part
of the policy gate and refuses any reported error, so a declared component path
the runtime loader cannot resolve fails here rather than at install time. The
gate is not run with `--strict`, because the manifest declares the `protocol`
field that the compatibility contract reads and the client reports every field
it does not recognise as a warning. Exactly one warning path, `protocol`, is
tolerated; a second unknown field fails. A machine without the Claude client
installed reports the step as skipped rather than passing it silently.

The validator reports commands but never lists skills, so the same gate checks
separately that every directory under `plugins/agent-parley/skills` carries a
`SKILL.md` declaring both a name and a description.

Repository installation does not imply public directory approval. For Claude,
validate `plugins/agent-parley` with `claude plugin validate`, then use the
[community submission form](https://platform.claude.com/plugins/submit).
The official catalog is curated separately; see
[Claude's guide](https://code.claude.com/docs/en/plugins).

For Codex, follow [OpenAI's submission guide](https://developers.openai.com/plugins/deploy/submission).
This is a skills-only plugin. Submission requires a verified publisher, listing
and policy URLs, a skill bundle, and review cases. `make codex-bundle` builds
the skill bundle the portal accepts. `make release-artifacts` builds the same
archive into `dist/release`, so every published release carries
`agent-parley-VERSION-codex-skills.zip` as an asset with its hash in
`SHA256SUMS`, and the portal step downloads a released file instead of
building one locally. The Codex listing is live; the Claude
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
from the verified `dist/release` bundle, so the plugin archive, the Codex
submission archive, the exported requirements file, the changelog, the release
notes and the checksum manifest are never uploaded. Authentication uses PyPI Trusted Publishing over OIDC: the
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
