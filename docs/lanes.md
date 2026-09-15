# Running lanes

Everything an operator does to a lane from the base checkout: starting it,
steering it, holding it, integrating it and sending it for review. The command
surface itself is listed in [Commands](commands.md), and the deeper reference
for each area is in [Operations](operations.md).

## Starting a lane

From a committed, clean checkout, one terminal per agent:

```sh
agent-parley run claude
agent-parley run codex
```

The first run registers the repository, creates that participant's worktree and
branch, starts the coordination service, and hands you the native CLI. Prompt
it exactly as you always do.

A new name creates its own lane, so a second account of the same provider, or
another provider, is one more terminal:

```sh
agent-parley credentials add account-2 --config-home ~/.claude-account-2
agent-parley run claude-2 --provider claude --credentials account-2
```

Which native CLI a name drives, and how an account is selected, is in
[Providers](providers.md).

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

## Steering a lane

Steer one lane without taking over its terminal:

```sh
agent-parley say claude-2 "Rebase onto main before you open the pull request."
```

The message lands in that lane's inbox beside peer traffic, so the agent reads
it at its next checkpoint. It comes from `operator`, a command-line identity: no
participant can be named `operator`, and no MCP tool sends as it, so an agent
cannot write in its name. Repeating the same message delivers nothing further,
and `--ack` asks the lane to acknowledge it.

A steer can also wait for the moment it is useful:

```sh
agent-parley say claude-2 "Pick up 18 next." --when-released 17
agent-parley say claude-2 "Wrap up for today." --at 18:00
agent-parley mail pending   # recorded, not delivered yet
```

The item waits in coordination state and is delivered by the supervision poll
that already watches every project: no scheduler process, and no read-only view
ever delivers mail. A stopped service delivers nothing and loses nothing.
`--after`, `--unless-reported` and a bounded `--every ... --until ...` repeat
are described in
[Operations](operations.md#delivering-a-message-or-an-offer-later).

## Holding, stopping and restarting

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
session is alive, refuses a dirty worktree naming the paths, replays the
recorded `init` command and launches the same provider and account as before.
None of the four releases a claim: ownership still moves only through an
explicit release or an accepted handoff, and all four land in the event log.

## Integrating one lane

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

## Integrating several lanes

With several lanes finished, integrate them as a set instead of deciding the
order by hand:

```sh
agent-parley participant merge --all              # every lane reported ready
agent-parley participant merge --group rewrite    # one group of the plan
agent-parley participant merge --all --preview    # the ordered plan only
```

Both order the lanes from the dependency edges already recorded, so a lane whose
issue waits on another is merged after the lane holding that issue. A cycle is
refused and named, never quietly ordered. Every candidate is preflighted with
the same conditions `--preview` reports, and each merge then runs through the
single-lane path, so nothing is integrated on easier terms than it would be
alone.

A group is admitted whole or not at all: one refused member leaves the group
unmerged. Execution is ordered rather than atomic, so a merge or a gate failure
part way through stops the run, leaves the earlier merge commits in place and
reports what was integrated, what refused and what was not attempted. Nothing is
reset or reverted.

A bulk merge only ever considers lanes that report ready, and its plan names
the prerequisites that lie outside the selected set with the ledger's account
of each, because narrowing a selection never lifts a recorded dependency.
Groups whose every member is reported ready are marked by `plan show` and
`status`, and counted in the `top` header, so you learn a set is integrable
without asking each lane. A reported state is a lane's own account, never review
or independent verification.

## One command for many lanes

With eight lanes, ending the day should not be eight commands. `say`,
`participant stop`, `participant pause`, `participant resume`,
`participant merge`, `participant pr` and `issue assign` take a lane selector in
place of the positional name:

```sh
agent-parley say "Wrap up for today." --all
agent-parley participant stop --provider claude
agent-parley participant merge --outcome ready --yes
agent-parley participant resume --idle
agent-parley participant pr --drifted
```

`--all` selects every lane and the other filters narrow it, so they combine.
Each filter reads the same lane facts `status` reports: the provider driving a
lane, the lane's own latest reported outcome, whether its checkout sits on the
branch it was assigned, and whether the lane reads as `idle` rather than active.
A positional name and a selector together are refused.

Every bulk run prints the lanes it matched and what will happen to each, then
asks once for the whole set; `--yes` skips that one question. A selector that
matches nothing does nothing and says so. Independent operations continue past
a lane that refuses, printing its refusal beside it, and the closing tally names
what was done and what refused. Integration is different: it keeps the ordered
stop-on-failure contract above, so the first refusal leaves every later lane
unattempted. The exit status is non-zero when any lane refused or failed.

`issue assign` carries one offer, so a selector stands in for its lane only
while it matches a single lane; a wider match is refused and names what it
matched.

## Requiring a gate before a merge

A repository can require its own command to pass before any merge. The command
is recorded in coordination state, not in the repository:

```sh
agent-parley verify set 'make check'   # require it from now on
agent-parley verify show               # report what is required
agent-parley verify set ''             # remove the requirement
```

With one configured, `participant merge` runs it in the base checkout first and
streams the command's output, refusing the merge on a non-zero exit and
reporting the exit status. It runs as an argument list, never through a shell,
and no flag skips it. It reports the base checkout as it stands before the
merge, which is not a claim about the merged result. In a `--all` or `--group`
run it also runs after each member, so a set whose halves pass alone but fail
together is caught; a failure there leaves the merge commit present and visibly
unverified.

## Requiring your own decision

A repository can also require your own recorded decision before a lane's work
leaves its worktree:

```sh
agent-parley approval set merge pr   # refuse both until a decision exists
agent-parley approval show           # report what is required
agent-parley approval set            # require no approval again
agent-parley approve claude-2        # record that you approved its report
agent-parley reject claude-2 'Needs a test for the retry path'
```

With the requirement set, `participant merge` and `participant pr` refuse until
`approve` records a decision on that lane's current ready report, and the
refusal names both the report and the command that grants it. The decision is
bound to that report, the lane's exact commit, its branch and base, and the
verification and pull-request settings in force, so new commits, a further
report, a retargeted base or a changed gate each need a new decision; the
binding is rechecked immediately before the merge or the push. A decision that
cannot be read refuses integration rather than allowing it. A rejection
delivers your reason to the lane as operator mail and the lane keeps working:
only these two commands are gated. `status` shows `awaiting approval`,
`approved` or `rejected` beside a ready report, `top` counts the lanes awaiting
one, and `history --kind approval` lists the decisions with the rest of the
chain.

`approve` and `reject` run from the base checkout and refuse to run inside an
assigned worktree, so no lane records the approval of its own work through
these commands. That is this tool's command-line boundary and not an
operating-system one: a program running as you can write coordination state
directly. The decision also records that a human decided, not that the code is
correct; the verification command, the attribution scan and GitHub's own
checks all still run.
