# Commands

Use `agent-parley COMMAND --help` for arguments, and `agent-parley` with no
arguments for the command list, grouped as coordination, policy,
observability and lifecycle. `--home DIR` selects private state globally;
repository commands accept `--repo PATH` unless noted below. Issue mutations,
reports and lane mail resolve identity from the current lane. `say` and
`issue assign` act as `operator` from any checkout of the repository.

`--repo` is the repository selector everywhere, `status` and `top` included.
Both still accept `--project` for one release; it is undocumented in their
help and will be removed.

`top` is the dashboard: every lane at once, redrawn, or one frame with
`--once` or `--json`. `watch NAME` is the event stream: one lane's
coordination events as they are recorded. Reading history follows one shape
in all three places that keep it — `history`, `events` and `state` each show
on standard output and export to a file.

## Command reference

| Command | Purpose |
| --- | --- |
| `--version`, `-V` | Print the installed version and exit. |
| `version` | Print the installed version and the state directory in use. |
| `up` | Start the local coordination server. |
| `down` | Stop the server while retaining state and worktrees. |
| `completion SHELL` | Print a `bash`, `zsh` or `fish` completion script generated from the installed command tree. |
| `status` | Show server health, whether the running service is behind the installed code, and one table per project; `NAME` reports one lane in full, and `--repo`, `--provider`, `--outcome`, `--drifted`, `--pending`, `--idle`, `--since`, `--over-budget` and `--issue` narrow the rows. |
| `setup PATH` | Register a repository from committed HEAD. |
| `run NAME` | Launch a lane; supports `--provider`, `--credentials`, `--repo`, and `--task`. |
| `top` | The dashboard of every lane; `--once` prints a snapshot, `--interval` sets refresh seconds, `--provider`, `--repo`, `--participant` and `--since` filter it, `--sort`, `--reverse` and `--columns` shape it. |
| `metrics` | Export the live counters and gauges as Prometheus text or `--json`; `--output` writes a file atomically and `--every` rewrites it. |
| `report` | Record `--state`, `--summary`, and required `--remaining` or `--evidence`; `--idempotency-key` makes a retry safe. |
| `say NAME TEXT` | Send as `operator`; `--ack` requests acknowledgement and `--key` controls deduplication. |
| `say NAME TEXT --after 30m` | Record the message for later; `--at 18:00`, `--when-released N` and `--unless-reported` set the trigger, and `--every 1h --until 18:00` records a bounded repeat. |
| `issue list` | Show claims, dependencies and handoff offers; each offer carries the offering lane's head commit, the reservations that move with it and its remaining work. |
| `issue show NUMBER` | Show one issue: its owner, deadline, attempts, blockers, pending offer, the reservations its owner holds and its recorded history. |
| `issue next` | Rank the unclaimed, unblocked issues this lane could take next, each with the reason for its place: the plan group already under way, the issues it unblocks, the peer reservations and forecast collisions its likely paths run into, and the provider it declares. It claims nothing; `--limit` bounds the list. |
| `issue claim NUMBER` | Claim an available issue from this lane. |
| `issue release NUMBER` | Release ownership without closing the GitHub issue. |
| `issue offer NUMBER --to NAME --summary TEXT` | Pause work and offer ownership explicitly; `--when-released N` records it until that issue is released. |
| `issue accept NUMBER --offer-id ID` | Accept the current offer addressed to this lane; the offered reservations move with the issue. |
| `issue decline NUMBER --offer-id ID` | Decline the current offer addressed to this lane. |
| `issue cancel NUMBER` | Cancel this lane's pending handoff offer. |
| `issue assign NUMBER NAME` | Offer an issue to a lane as `operator`; `--reason` travels with the offer and `--unassign` withdraws one no lane accepted. |
| `issue block NUMBER --on NUMBER` | Record an advisory issue dependency. |
| `issue unblock NUMBER --on NUMBER` | Remove a recorded dependency. |
| `plan apply PATH` | Record a TOML work order as advisory dependencies; `plan diff PATH` previews it. |
| `plan show` | Print the applied plan as a tree with owners; `--json` prints it for scripts. |
| `doctor` | Report launcher, plugin, store and running-service versions and their fit; non-zero exit on a mismatch. |
| `problems` | List every lane, claim and store condition that needs an operator, oldest first, with the command that clears each; `--ack-after` sets the acknowledgement age, `--json` prints it for scripts, exit 1 when any row exists. |
| `problems ack ID` | Record your own acknowledgement of one message a lane left unanswered. It clears that condition and nothing else: no ownership moves, no reservation is released and no lane is woken. |
| `issue ... --idempotency-key KEY` | Retry any transition safely; the repeat returns the first result. |
| `participant list` | List the project's lanes and their identities. |
| `participant show NAME` | Show one lane: its branch, worktree, provider, account profile, advisory budget, current claims, reported outcome and last coordination. |
| `participant add NAME` | Create a lane with an optional provider and credential profile. |
| `participant restore NAME` | Restore the assigned branch while preserving work. |
| `participant retire NAME` | Retire a lane that is no longer working while preserving recoverable work. |
| `participant pause NAME` | Refuse a lane's calls and tool use; keep its session and claims. |
| `participant resume NAME` | Let a paused lane act again. |
| `participant stop NAME` | End a lane's session from the base checkout; keep its claims. |
| `participant restart NAME` | Start a stopped lane again from a clean worktree. |
| `participant merge NAME` | Run the configured gate and merge; `--preview` only inspects. |
| `participant pr NAME` | Push the lane branch and open or locate its pull request. |
| `participant budget NAME` | Show or set the lane's advisory `--tokens`, `--calls` and `--hours` limits; `0` removes one. Crossing a limit marks the lane and stops nothing. |
| `... --all --provider N --outcome S --drifted --idle --over-budget` | Select several lanes for one `say`, `issue assign`, `participant stop/pause/resume/pr/merge`; one plan and one confirmation, `--yes` to skip it. |
| `approve NAME` | Record your approval of a lane's current ready report. |
| `reject NAME REASON` | Record a rejection and deliver the reason to the lane. |
| `approval show` | Show which steps require a recorded approval first. |
| `approval set [STEP ...]` | Require an approval before `merge`, `pr`, both, or none. |
| `provider list` | List built-in presets and local overrides. |
| `provider show NAME` | Show one provider definition with the hooks its adapter cannot serve. |
| `provider add NAME` | Define a provider; warn when shadowing a built-in preset. |
| `provider remove NAME` | Delete a local definition, restoring a shadowed preset. |
| `provider budget NAME` | Show or set the advisory limits every lane on that provider inherits. |
| `credentials list` | List native account profiles. |
| `credentials show NAME` | Show one profile with every recorded value redacted: the config home, the override names and the variables required from your shell. |
| `credentials add NAME` | Define a config home and environment requirements. |
| `credentials remove NAME` | Delete a profile definition, preserving native files and logins. |
| `branch show` | Show the prefix new lane branches are created under. |
| `branch set PREFIX` | Set that prefix; existing lanes keep their branch. |
| `forge show` | Show the issue tracker this project coordinates over. |
| `forge set NAME` | Select `github`, `beads` or `null`; only `github` opens pull requests. |
| `resources show` | Show the named resources lanes may reserve. |
| `resources set NAMES` | Declare them; an empty string accepts any well-formed name. |
| `deadlines show` | Show this project's deadline and attempt defaults. |
| `deadlines set` | Set `--claim`, `--offer`, `--ack` windows and `--attempts`. |
| `budget show` | Show the advisory token, call and hour limits every lane of this project inherits. |
| `budget set` | Set `--tokens`, `--calls` and `--hours` project defaults; a budget informs and does not gate. |
| `verify show` | Show the project's configured pre-merge command. |
| `verify set COMMAND` | Set that command; an empty string removes it. |
| `init show` | Show the command every new lane runs before it starts. |
| `init set COMMAND` | Set that command; an empty string removes it. |
| `mail thread ID` | Read this lane's messages in a thread; `--after-id` pages forward. |
| `mail search QUERY` | Search this lane's mail with an optional `--limit`. |
| `mail list` | List this lane's mail newest first, with the same `--limit` as a search and no query to write. |
| `mail ... --as NAME` | Read `mail show`, `mail thread`, `mail search` and `mail list` for one lane from the main checkout, so an operator opens a message `problems` cites without changing directory. It reads only: nothing is sent, acknowledged or marked read for that lane. |
| `mail send NAME TEXT` | The same command as `say`, under `mail` with the other mail verbs; every `say` flag applies. |
| `decide TEXT` | Record one decision every registered lane can read; `--subject` names it and `--key` deduplicates it. |
| `decision list [QUERY]` | List or search the decisions recorded for this project; `--since` bounds their age and `--limit` the page. |
| `mail pending` | List operator messages and offers recorded but not delivered. |
| `mail cancel ID` | Remove one recorded operator item before it is delivered. |
| `history issue N` | List every record that touched an issue, with each holding; `--output FILE` exports the same reading as one JSON document. |
| `history participant NAME` | List everything one lane filed. |
| `history claim ID` | Follow one claim to the pull request that ended it. |
| `events show` | Print the retained records as JSON Lines on standard output. |
| `events export` | Export JSON Lines; filter by `--participant` and `--since`, or write `--output FILE`. |
| `state export --output PATH` | Write the whole state directory, or one `--project ROOT`, as one tar archive with a hashed manifest and no credentials. |
| `state show PATH` | List an archive's projects, participants, issue counts and export time without importing it. |
| `state import PATH` | Restore an archive into an empty state directory; `--merge` adds projects beside existing ones and refuses a collision. |
| `watch NAME` | Follow one lane's coordination events as a stream; `--since` widens the backlog, `--kind` narrows it, `--json` prints JSON Lines. The agent's conversation is never shown. |

## Machine-readable output

Every read-only command above also accepts `--json` and prints exactly one JSON
document, so a script, a shell prompt or another agent reads coordination state
without parsing a table: `status`, `top`, `version`, `issue list`,
`issue show`, `issue next`, `participant list`, `participant show`, `mail thread`,
`mail search`, `mail list`, `mail pending`, `decision list`, `approval show`,
`verify show`, `init show`, `branch show`, `forge show`, `state show`,
`provider list`, `provider show`, `credentials list` and `credentials show`.
The commands that change something print their outcome the same way with
`--json`: `up`, `down`, `setup`, `run`, `say`, `decide`, `mail send`,
`mail cancel`, `approve`, `reject` and `problems ack`. `top --json` prints one
frame and exits.
The document carries the identifiers the table abbreviates — offer, message and
thread IDs — with every time in RFC 3339, and no credential value. A pending
handoff carries its structured fields there too: the offering lane's head
commit, the reservations that move on acceptance, and the remaining work.
Field names are documented in
[Operations](operations.md#machine-readable-output) and carry the same stability
promise as the flags. `events export` and `watch --json` stay JSON Lines,
because each is a stream rather than a snapshot.

## MCP tools

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
| `search_decisions` | Search decisions any lane recorded for this project, whoever sent or received them; an empty query lists the newest and `since` bounds their age. |
| `read_attachment` | Page an attachment a message, report or offer named; only its writer and its addressees may read it. |
| `next_issues` | Rank the unclaimed, unblocked issues this lane could take next, with the reason for each; `limit` bounds the list. It claims nothing, so the chosen issue is still taken by an explicit claim. |

`send_message` also takes a `decision` flag. A message marked that way is
additionally recorded in the project's decision log, which every registered
lane searches with `search_decisions`, so a third lane learns an agreement it
was never addressed in. Nothing else widens: mail without the flag stays
readable by its sender and its recipients alone, and a decision obeys the same
body cap and attachment rules as any other message.

Inbox rows include `read_ts` and `ack_ts`. Fetching changes neither. Both
filters can be combined; `unacknowledged` selects messages that requested an
acknowledgement and have not received it. The result budget is 8,192 UTF-8
bytes.

## What does not fit in a message

A message body above 4,096 UTF-8 bytes, a report `--evidence` above 4,096 or a
handoff summary above 2,048 is neither refused nor truncated: the whole body is
kept as an attachment under the private state directory and the record carries
the first bounded slice ending with `[attachment message-12: 20480 bytes]`. The
peer's checkpoint notice stays within its 1,536-byte budget and ends with that
reference. Nothing is delivered whole automatically; the reader calls
`read_attachment` for 2,048 characters at a time, or prints it with
`agent-parley mail show ID --full` and `agent-parley report show ID --full`. One
attachment is capped at 65,536 bytes, a lane holds at most 1 MiB of them, and an
attachment is removed when its record is pruned or its offer is declined,
cancelled or released.
