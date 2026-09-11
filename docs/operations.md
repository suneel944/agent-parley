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

## Participants, providers and accounts

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
The `deepseek`, `kimi`, `grok` and `gemini` presets need their vendor base URL
and key exported in the launching shell; the launcher refuses to start when a
required variable is unset, rather than falling back to another account.

A preset is named after the vendor whose models answer, not after that vendor's
own agent CLI. `gemini` starts the `codex` CLI against a Gemini endpoint and
requires `OPENAI_BASE_URL` and `OPENAI_API_KEY`; it does not start the Gemini
CLI. The same holds for `deepseek`, `kimi` and `grok`.

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

The launch path was exercised against a stub executable that records its
arguments and environment, not against a live Copilot session, so argument and
file handling are verified while live model behavior is not.

Gemini CLI, OpenCode and Amp remain uncovered, each for a different reason
recorded below. `agent-parley provider add` will store a definition naming one
of them, because the command is only resolved on `PATH` at launch, but the
resulting session fails inside the native CLI. There is no flag that makes it
work and none should be added.

| Agent CLI | MCP configuration | Lifecycle hooks | Per-account config home |
| --- | --- | --- | --- |
| Gemini CLI | `mcpServers` in `~/.gemini/settings.json`, or `gemini mcp add` | `hooks` in the same file | none published |
| Copilot CLI | `$COPILOT_HOME/mcp-config.json`, or `copilot mcp` | `hooks` in `$COPILOT_HOME/settings.json` | `COPILOT_HOME` |
| OpenCode | `mcp` in `opencode.json`, or `opencode mcp add` | JavaScript plugins only | `OPENCODE_CONFIG_DIR` |
| Amp | `--mcp-config`, or `amp.mcpServers` in its settings file | `amp.hooks` in its settings file | `--settings-file`, `AMP_SETTINGS_FILE` |

Copilot CLI is covered by the `copilot` adapter above. The other three are not.
Amp accepts `--mcp-config`, but it has no `--append-system-prompt`, and its
settings arrive through `--settings-file` rather than `--settings`, so two
thirds of the `claude` contract is rejected. OpenCode extends sessions through
JavaScript plugins rather than hook commands, so lane checkpoints and the
enforcement record would have no way to run. Gemini CLI publishes no variable
that relocates `~/.gemini`, so a credential profile cannot give it an account
of its own; `launch_environment` refuses a profile whose provider has no
`home_env` rather than sharing one login.

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

The pull request opens owned and classified. Your own GitHub account becomes
its assignee, and its change-type labels and milestone are read from the issues
the lane claims rather than chosen by the lane, so a participant never
classifies its own work. That metadata is resolved before the branch is pushed:
an issue carrying no change-type label refuses the whole command, naming the
issue and the labels the repository accepts, and claimed issues carrying
different milestones refuse it as well. A reported `ready` outcome is the
participant's own account, so an opened pull request still needs review and the
target repository's own gate.

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
and policy URLs, a skill bundle, and review cases. Neither catalog submission
has been made. CI builds artifacts; it does not submit review forms.

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
run rather than reaching a release. The App commits that to `main`, pushes the
annotated `vVERSION` tag, and dispatches `Release` for it.

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

The release workflow answers only to `workflow_dispatch`. It deliberately has
no tag trigger: publication must name a tag explicitly, so a tag pushed by any
other route publishes nothing. `Auto version` dispatches it by name after
pushing the tag it measured. Publishing an existing tag
is idempotent, so a rerun verifies the uploaded bytes again instead of failing.

Release notes are assembled from two sources so that no release needs hand
editing: `docs/release-overview.md` is a standing description of what the
project is and how to verify a download, and the bump step generates the
version section of `CHANGELOG.md` from the commits the eligibility measurement
counted. Maintenance, automation, build, refactor, documentation and test
commits never count, so the notes carry features, fixes and performance, and
the published notes and the decision to release describe the same work.
Rewrite `docs/release-overview.md` when the product description changes, not
when a version does.

Every GitHub release is titled `Agent Parley vVERSION`, minted by publication
rather than typed, so the releases page reads as one series. Releases published
before that title existed were renamed to match. Do not retitle a release by
hand: a page where one entry names the product and another shows a bare tag
reads as two different projects.

One-time owner setup, without which `Auto version` fails as soon as a push
warrants a version:
register a GitHub App under the owner account with repository permissions
Contents: read and write and Actions: read and write; install it on
`suneel944/agent-parley`; set the repository variable `RELEASE_BOT_APP_ID` to
the App ID; set the repository secret `RELEASE_BOT_PRIVATE_KEY` to a generated
private key in full PEM form; and add the App to the bypass actors of the
`main` branch ruleset so it can push the release commit and tag. Contents
covers the commit and the tag, Actions covers the `Release` dispatch. The App
holds no Pull requests or Issues permission, so it cannot open, approve or
merge anything.

To cut a release by hand in an emergency, `Release` still accepts a
`workflow_dispatch` with an existing tag, and reruns the same verification.

After the GitHub release is published and its uploaded bytes have been verified
against the local checksums, the workflow publishes the distribution to PyPI. It
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
rejected with `400 Bad Request` after the release is already published. The
step deliberately runs last, because a version published to PyPI can never be
re-uploaded or replaced, so it must not run before the GitHub release is
confirmed good. Rerunning the workflow against an existing tag stays safe:
files already on the index are skipped rather than treated as a failure.

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
