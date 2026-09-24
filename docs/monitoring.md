# Monitoring

Every reading here is read-only: no lock is taken, no vendor is asked, no model
call is made and nothing in coordination state moves. The field-by-field
reference for the same views is in [Operations](operations.md#reading-status).

## Three presence states

A lane is reported in one of three states, and they are not interchangeable:

| State | What it means | What it is not |
| --- | --- | --- |
| `active` | The lane served a coordination call inside the configured interval. | — |
| `idle` | The session process is alive, but nothing was served inside the inactivity threshold. | Not a lost lane, and not an error. |
| `stopped` | The recorded session process is gone. | Not merely a quiet lane. |

A live lane past the inactivity threshold therefore reads `idle`; only a dead
process reads `stopped`. The distinction is what lets `problems`, the wake
path and the fitness check tell a lane that is thinking from a lane that is no
longer there. Every state only reports: nothing is revoked, no claim is released
and no ownership moves.

The send result keeps the older operator wording for the dead case. A message
addressed to a `stopped` lane is summarised as `queued for NAME (unreachable)`,
and one addressed to an `idle` lane as `queued for NAME (idle; wake
requested)`. A presence row written before this release still carries
`unreachable` and is read as `stopped`.

`idle` names the oldest waiting item and how long it has waited, so a lane that
is quiet with an empty inbox reads differently from one sitting on unread mail.

## A second session in a lane

A hook event from a session other than the lane's recorded one is recorded as
`session_mismatch` with the arriving `session_id`, the lane's
`recorded_session_id` and the arriving native `pid`, and it changes nothing.
When the recorded session's process has exited and the event comes from a live
native process, the new session is adopted as if it had sent `SessionStart`, so
a lane whose start event was lost follows its real session again. Any other
second session, such as a headless `claude -p` started inside the worktree, is
named in the lane's `status` detail and as a `second session` row in
`problems` while its process runs, or for ten minutes when its process cannot
be told apart from the lane's.

## A lane held by a native dialog

A native client sometimes stops on a screen of its own: a usage limit, a tool
permission prompt, a hook trust review. The client process stays alive and runs
no hook, so without the screen a held lane reads as a quiet lane. The launcher
owns the client's terminal, so it reads that screen and reports it. The activity
column then shows `dialog: ` and the dialog's name, the activity record carries
a `dialog` entry with the last screen lines, a wake addressed to the lane is
refused with `manual attention required`, and you get one notification with the
screen text. A recognized usage limit also records the provider capacity as
exhausted with the reset instant the screen names, so the lane is parked with
that reason and restored when the reset passes. A screen that names no reset
is probed on a doubling backoff capped at one hour; an accepted probe or a
later tool call clears the exhaustion. No lane on a dialog is ever
reported as `working` or `starting`.

Five dialogs are recognized, recorded from `claude` CLI 2.1.270 and Codex CLI
0.153.4: `usage-limit`, `question`, `hook-review`, `directory-trust` and
`tool-permission`. Only the bottom of the screen, where a client draws its
dialog, is read. A framed dialog counts only while it shows at least two
numbered options and its own footer (`Esc to cancel`, `Enter to select` and
similar), and a usage limit counts only on the client's own notice line. Text
that merely quotes a dialog, in scrollback or in the agent's output, never
parks the lane. Once a dialog is answered, by the launcher or by a key you type
in the lane's terminal, the next screen output releases the lane.

`directory-trust` is the screen that asks whether you trust the folder. Codex
records that trust for the repository root, so trusting a lane's worktree also
covers the shared project. `question` is the client asking you a question
through its own picker. The notification and the `dialog` entry name the
question and every option it offers, and `status` lists the lane as held by
that dialog. To let a lane answer questions without you, set a standing reply;
it is typed into the picker's free-text option:

```json
{"participants": {"claude": {"answer_questions": "Use your best judgement."}}}
```

The reply is one line of printable text of at most 500 characters, set per
participant or under `supervision` for the whole project. Answering any other
dialog is
your decision, so nothing is answered until you say which option to press. Name
the option's own text, not its position, because the clients reorder options
between versions:

```json
{
  "supervision": {"dialogs": {"hook-review": "review hooks"}},
  "participants": {
    "claude": {"dialogs": {"tool-permission": "yes"}}
  }
}
```

A participant entry overrides the project entry for that dialog name. An
unanswered dialog, a dialog whose configured option the screen does not offer,
an answer the screen survives twice, and any prompt that is not a recognized
dialog and holds the screen for 30 seconds are all escalated to you instead. The
keystrokes sent are the ones you would press on an option the client itself
offered; no permission check is skipped and no bypass flag exists.

## A lane waiting for a tool approval

A permission prompt is also visible without the screen: the client runs its
`PermissionRequest` hook while it waits, and that hook names the tool it is
asking about. The lane then reports `waiting for approval` followed by that tool
name, and its activity record carries the same `dialog` entry the launcher
publishes, naming the tool and the instant the wait began. A repeated request for
the same tool keeps that instant, so `status` shows how long the prompt has stood
unanswered rather than how recently the client asked again. A wake addressed to
the lane is refused with `busy:approval`, which names the prompt without counting
the refusal as a failed wake, and no fit check passes the lane, so work is never
offered to a client that is holding a question for you. Once the prompt has
waited past the project's stall interval, `problems` reports it under `waiting on
approval` with the tool and the age.

A resumed session asks again for permission to use this bridge's own MCP tools,
and a service-driven resume has nobody at the keyboard to answer. `claude` 2.1.270
carries per-tool approval in its own settings, so a launch can allow that one MCP
server there, and only when you record the opt-in:

```json
{
  "supervision": {"approve_bridge_tools": true},
  "participants": {
    "claude-2": {"approve_bridge_tools": false}
  }
}
```

The default is off and changes nothing about the client's configuration. With it
on, the launch adds two native permission rules: `mcp__agent_parley`, which
allows this bridge's own coordination tools, and
`Bash(<interpreter> -m agent_parley.cli *)`, which allows the exact interpreter
and module the protocol prompt orders every lane to run for `issue claim`,
`issue list`, `report` and the other CLI commands. Both are spelled from the
same string the prompt prints, so they cannot drift apart. Nothing else is
allowed: no file tool, no other shell command, no other MCP server, no bypass
flag and no weakened decision. A lane launched before you recorded the opt-in
still draws the shell prompt for that command; its watcher reads the opt-in
again while the prompt holds the screen and answers `Yes` when the prompt's
command begins with that interpreter and module and chains no second command.
A prompt for any other command escalates as before. A participant entry
overrides the project entry, so one lane can stay fully interactive.

A lane the supervisor launches or resumes runs in the client's default
permission mode; no permission mode is passed. A lane you started by hand and
switched to the client's auto mode does not carry that choice into a
supervisor-driven resume, so a resumed lane parks on the first shell command
outside your own allow list and the two rules above. Record that command in
the client's own permission settings to keep such a lane moving. Codex CLI
0.153.4 has no per-tool approval surface of its own — its approval settings are
whole-session policies — so its launch is left untouched and its prompts are
reported for you to answer.

## `status`

`status` prints the server line, the code line, the state directory and then one
table per project, a row per participant: ownership, activity and outcomes.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/suneel944/agent-parley@main/docs/assets/screenshot-status.svg" width="880" alt="agent-parley status listing issue owners, a pending handoff, and a lane on the wrong branch">
</p>

`Code: stale` says the running service is older than the installed code. The
service is up and answering, so nothing reports it as down, but it is serving
the build it was started from rather than the one on disk: restart it with
`agent-parley down && agent-parley up` before reading the next frame as current.
`Code: ok` says the running service matches the checkout.

Narrow the table by appending a participant name, which reports that lane in
full instead of as a row, or by filters that combine:

```sh
agent-parley status claude-1          # one lane, the whole reading
agent-parley status --drifted         # lanes off their assigned branch
agent-parley status --pending         # unread mail, offers or stale leases
agent-parley status --idle            # live lanes past the inactivity threshold
agent-parley status --outcome blocked --provider codex
```

`--drifted` and `--pending` exit non-zero when a lane matches, so a shell gate
fails on drift without parsing the table. Columns shrink to the terminal, and a
redirected stream receives every column instead.

A pending handoff is reported with the offering lane's head commit, the
reservations that move with it and the remaining work from its last report, in
the table and in `status --json`.

## `top`

`top` is the live view: every lane at once, refreshed in place, `q` quits.

```sh
agent-parley top --provider codex
agent-parley top --provider claude --provider codex
agent-parley top --sort IDLE --reverse
agent-parley top --project payments --participant codex
agent-parley top --columns PARTICIPANT,STATE,ISSUES,IDLE
```

With a dozen lanes open the whole table is rarely what you want. `--provider`
narrows the view to the participants driven by one provider, and the header
counts only the rows it shows.

`top` fits the terminal it is given. Every column is as wide as the widest value
in the frame, and a terminal too narrow for the whole set drops the
lowest-priority columns in a fixed order and names them under the header instead
of clipping every cell. Rows past the fold are paged, never dropped: the footer
reads `rows 1-8 of 31`. `--once` prints at the width of the terminal and at the
full width of the table when the output is a pipe, so a captured file keeps
every column intact.

The same choices are reachable from the live view with single keys:

| Key | Does |
| --- | --- |
| `j`, `k`, down, up | Select the next or previous lane, paging the table. |
| `enter` | Show every recorded field of the selected lane, unclipped. |
| `s` | Order by the next column. |
| `r` | Reverse the order. |
| `f` | Narrow to participants, comma separated; empty clears. |
| `o` | Narrow to projects, comma separated; empty clears. |
| `c` | Choose the columns shown; empty shows all. |
| `P` | Show the `problems` rows in place until any key returns. |
| `?` | Show the key map and the column legend. |
| `q` | Leave. The view never writes state. |

A lane that drifted from its branch, holds a stale lease, had a call rejected,
owns an overdue issue or lost its session process is drawn in colour where the
terminal offers it and in bold where it does not. Each of those also prints its
own marker in the table, so a monochrome pipe reads exactly the same.

`running; no hooks` means the session needs relaunching to obtain checkpoint
reporting. `top --once` prints full detail.

`TOKENS` is the session total that lane's own native client already wrote to
disk. No vendor is asked, no key is read and no price is applied, so it is a
relative signal between refreshes rather than billed spend, and the cell is
blank when nothing could be read.

## Idle time is a number, not an impression

`top` carries an `IDLE` column: how long each lane went without coordination
activity inside the window, with the project total and the worst lane in the
header. `status` prints the same figure and, under it, every pending item with
the seconds it has already waited — a message before its first read, an
`ack_required` message before acknowledgement, a handoff offer before an answer,
a `ready` report before integration. Every figure comes from records the runtime
already keeps, so it reports observed coordination inactivity and never claims
to know what the native client was doing inside a turn.

## `problems`

One screen says what needs you now. `agent-parley problems` lists, oldest first,
every condition an operator should act on: a lane stalled or inactive past its
supervision threshold, a claim past its deadline, a handoff offer with no
answer, a message awaiting acknowledgement past `--ack-after`, a lane whose
branch drifted or whose worktree is dirty with no recent activity, a lane over
its advisory budget, a store schema behind the code, and a service that is down.

Each lane contributes one row per cause, not one row per item: a lane sitting
on twenty unacknowledged messages is a single row carrying that count and the
age of the oldest message. The remedy follows the lane's state, so a lane
parked on a native prompt is pointed at its own client instead of being sent
more mail it cannot read, an active lane is never told to leave its session,
and a dirty worktree is named by path with its changed files rather than
offered a retirement that would drop the lane's claims. Rows the supervision
service already handles — the wakes, the deliveries, the reclaims — say what
that loop has attempted and what it does next, and the report closes by
counting operator rows against service rows.

An empty list exits zero with one line saying so; any row exits 1, so a shell
or a cron can notice. `--json` prints the same rows, and `P` in `top` shows
them in place. The view reads the same snapshot `status` prints and moves
nothing itself.

## `doctor`

An out-of-date install says so before it costs a turn. The launcher, the plugin,
the store and the running service each state a version, and a mismatch is
refused at the boundary with both numbers and the one command that fixes it —
when a lane starts, when a hook runs, and when a served call arrives.

```sh
agent-parley doctor
agent-parley doctor --json
```

`doctor` prints four components: the launcher's version and protocol, the
protocol each shipped plugin manifest declares, the store's schema against the
schema this build writes, and the `service` component, which compares the
running coordination service against the code installed here. A service started
from an older build is reported stale with the restart that clears it, rather
than reported ready. `doctor` exits non-zero on a mismatch, so a script can gate
on it. It reads only, and prints no credential.

## `metrics`

`top` is a screen. `metrics` prints the same counters and gauges as text a
monitoring stack reads: the Prometheus text exposition format by default,
labelled by project, participant and provider, and `--json` for scripts.
`--provider` and `--since` narrow it exactly as they narrow the table:

```sh
agent-parley metrics
agent-parley metrics --json
agent-parley metrics --output /var/lib/node_exporter/parley.prom --every 30
```

`--output` writes the frame by atomic rename, so the textfile collector of
`node_exporter` never reads a partial one, and `--every` rewrites it on that
interval until you interrupt it. There is no HTTP endpoint and no new port: the
file is the interface. The command reads the records `top` reads, holds no lock
and writes no coordination state, and its `tokens` counter is what the native
client counted rather than billed spend, exactly as the `TOKENS` column is.

## `watch`

One lane can be followed as a stream. `agent-parley watch NAME` prints one line
per coordination event as it happens: `claim issue 42`,
`denied git (branch_switch)`, `mail from codex-2 (thread t12)`, `report ready`,
`call send_message ok`, `session ended`. It starts from the most recent twenty
events, follows the store and the event log from there, and stops on `q` or on
interrupt; `--since 1h` widens the backlog and `--kind` narrows it. When
standard output is a pipe the lines are plain, and `--json` prints one event
object per line, so `watch NAME --json | jq` works. It never shows the agent's
conversation: the stream is coordination only, and the native client's
transcript stays in that client.

```sh
agent-parley watch codex-2
agent-parley watch claude-1 --since 1h --kind claim --kind denied
agent-parley watch claude-1 --json | jq .description
```
