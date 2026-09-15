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

`status` prints the server line, the state directory, and then one table per
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
project's configured interval when no window is given; it measures
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

### Compatibility and `doctor`

Three numbers move independently: the launcher's package version, the wire
protocol a hook or served call speaks, and the store schema on disk. Several
package versions normally share one protocol, so equal package versions are not
the only compatible combination.

<!-- compatibility:start -->
| Launcher | Wire protocol | Store schema |
| --- | --- | --- |
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

The launcher version reported here is the version of the code that is running.
An editable install, which `make install-dev` creates, records its version once
at install time and keeps reporting it after the checkout has moved on, so the
project file beside the package is read wherever it exists and installation
metadata is used only for a package installed without one.

`doctor` prints the launcher version and protocol, the protocol each shipped
plugin manifest declares, and the store's schema against the schema this build
writes, then a verdict line. Each line carries the state this build puts that
component in, printed in upper case when the build does not accept it.

The store has four states. `absent` means no store has been created yet, which
is consistent because the service writes it at the current schema. `ok` means
the store matches this build. `needs migration` means the store predates this
build and has not been migrated: every process running this code queries
columns the older store does not have, so this is a mismatch, not merely an
older number. `unsupported` means a newer build wrote the store, which is
refused rather than downgraded.

The verdict names one command per distinct cause, because a store behind this
build and a plugin speaking another protocol need different commands. A behind
store is resolved by restarting the service, which migrates it; a newer store
by installing the build that wrote it; a plugin mismatch by reinstalling the
plugin. Its exit status is non-zero on a mismatch, so a
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
the store is usable and the service is up. Each row names the lane, the
condition, its age and the one command that clears it:

| Condition | When | Clears with |
| --- | --- | --- |
| `store` | The store schema is behind or ahead of this build. | The `doctor` remedy for that state. |
| `service` | The coordination server is not ready. | `agent-parley up` |
| `stalled` | A live lane holds mail older than `stalled_after` and served no call inside it. | `agent-parley run NAME --resume` |
| `inactive` | A live lane published no native activity inside `inactive_after`. | `agent-parley run NAME --resume` |
| `overdue claim` | A held issue is past its recorded deadline. | `agent-parley issue release NUMBER` |
| `unanswered offer` | A handoff offer has no answer yet. | `agent-parley issue cancel NUMBER`, or `issue assign NUMBER NAME --unassign` for an operator offer. |
| `awaiting acknowledgement` | A message needing acknowledgement has waited past `--ack-after`, which defaults to `stalled_after`. | `agent-parley run NAME --resume` |
| `branch drift` | The lane left its assigned branch. | `agent-parley participant restore NAME` |
| `dirty worktree` | The lane holds uncommitted work and is not active. | `agent-parley participant retire NAME` |
| `over budget` | The lane crossed an advisory token, call or hour limit. | `agent-parley participant budget NAME` |

An empty list prints one line saying so and exits zero; any row exits 1, so a
shell or a cron can gate on it. `--json` prints the same rows inside the shared
snapshot envelope with `count`. The `P` key in `top` shows the same rows in
place of the table until any key returns. Every command named is a suggestion:
the view revokes nothing, releases nothing and wakes nobody.

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
dependency cycle is refused before any edge is written. Groups name issues that
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
retry never gains authority the first call was denied.

Every `issue` transition and `report` accepts `--idempotency-key`. Over MCP the
same contract is carried by the optional `idempotency_key` argument on
`file_reservation_paths`, `release_file_reservations`, `acknowledge_message` and
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
project that records none gives a deadline only to the records that ask for one.

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

`agent-parley top` watches every participant live: session state, event age,
branch with a `!` when a lane left its assigned branch, issues owned and
handoffs pending, unread and unacknowledged mail, held leases with the age of
the oldest, delivered context, denials against retained hook events, served
MCP calls with rejections, and the tokens that lane's own native client
recorded. The header carries server health and the project's
denial rate. The view is read-only and makes no model call; `q` leaves it.

The table is fitted to the terminal rather than fixed. Each column is as
wide as the widest value in that frame and never narrower than its declared
minimum. When the set does not fit, columns are dropped in this order, and
the header names the ones that went:

```text
PROVIDER, EVENT, BRANCH, CONTEXT, CALLS, TOKENS, LEASES, IDLE, ISSUES,
DENIALS
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
`top` gains `!` and the stale count, `status` reports the stale share of a
lane's reservations, and a conflict names that holder as stale. The lease is
still held: nothing revokes it, reassigns it, or narrows what it blocks, and
only its owner releases it. Reading `!` as "an agent died holding this" is the
point; acting on it is the operator's decision, exactly as with a stalled
issue owner.

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

Two offers are built on that check, both advisory and neither moving ownership.
A lane that holds no claim and passes the check is offered, at its next
checkpoint, the unclaimed ledger issues no recorded dependency blocks — ordered
so the ones other owned issues wait on come first — together with the peers
holding more than one claim. A lane that holds more than one claim is told
which fit peers have been idle past the stall interval, so it can shed one.
Both messages count against the same 1,536-byte checkpoint budget as every
other injection and are delivered once per distinct offer: a lane whose
situation has not changed sees nothing new. Nothing is claimed for a lane,
`issue offer` remains the only transfer path, and the recipient still accepts
or declines.

Idleness here is observed coordination inactivity, which is not the same thing
as a live process or as provider capacity; the three are checked separately and
reported separately, because a quiet coordination channel alone does not prove
that a native turn is idle or that a lane can safely accept input.

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
| `kind` | The command reported: `status`, `top`, `metrics`, `issues`, `participants`, `history`, `mail_thread`, `mail_search`, `verify`, `init`, `resources`, `providers` or `credentials`. |
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
`report_age_seconds`, `injected_bytes`, `injections`, `claims`, `idle`,
`idle_seconds`, `idle_complete`, `waiting`, `wake` and `mail`, whose
`named_resources` array lists the named resources that lane holds. `claims`
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
`pending_ack`, `leases`,
`stale_leases`, `lease_age_seconds`, `injected_bytes`, `hook_events`,
`denials`, `calls`, `errors`, `tokens`, `idle_seconds`, `idle_complete` and
`prompt`. `tokens` is null when that
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
`offer` additionally carries `deadline_at`, `overdue` and `overdue_seconds`.
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
