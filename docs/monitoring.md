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
Each row names the lane, the condition, how long it has held and the exact
command that clears it. An empty list exits zero with one line saying so; any
row exits 1, so a shell or a cron can notice. `--json` prints the same rows, and
`P` in `top` shows them in place. The view reads the same snapshot `status`
prints and moves nothing.

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
