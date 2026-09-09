# Operations

## Install and upgrade

Installing needs no clone. `uv tool install git+https://github.com/suneel944/agent-parley`
tracks the default branch; a release wheel URL pins an exact version, and the
wheel needs no third-party runtime packages either way.

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
agent-parley report --state ready --summary "Result" --evidence "Checks and results"
```

`issue offer --to` names another participant in the same project.

`issue list` prints each owner's session state and the age of its last
checkpoint, so a stalled lane is visible. Reclaiming that work still needs the
owner to release it, or an explicit offer and accept.

`agent-parley top` watches every participant live: session state, event age,
branch with a `!` when a lane left its assigned branch, issues owned and
handoffs pending, unread and unacknowledged mail, held leases with the age of
the oldest, delivered context, denials against retained hook events, and served
MCP calls with rejections. The header carries server health and the project's
denial rate. The view is read-only and makes no model call; `q` leaves it.
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
needed to prepare a roster in advance. The participant name is what peers
address; the provider decides which native CLI starts and which endpoint it uses.
The `deepseek`, `kimi` and `grok` presets need their vendor base URL and key
exported in the launching shell; the launcher refuses to start when a required
variable is unset, rather than falling back to another account.

Credential profiles point a provider's config-home variable at a separate
directory so one provider can run under several accounts. Define one profile per
subscription; there is no limit besides the 32-participant project cap, and the
accounts need no relationship to each other. Sign in to each directory with the
native CLI once. Agent Parley stores directory paths and
variable names; it never stores tokens or keys, and rejects `--env` values whose
names look like credentials.

## Recovery and teardown

```sh
agent-parley status
agent-parley participant restore claude-1
agent-parley participant merge claude-1
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
claude plugin install agent-parley@agent-parley-local
codex plugin marketplace add suneel944/agent-parley
codex plugin add agent-parley@agent-parley-local
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

Update package/plugin versions together and add a changelog entry. Run `make check`
and `make release-artifacts`. After review and merge, an annotated `vVERSION` tag
triggers release CI. It reruns the gate, creates a draft, uploads assets, downloads
and verifies their checksums, then publishes. Failed verification leaves a draft.

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
step deliberately runs last, because a version published to PyPI can never be
re-uploaded or replaced, so it must not run before the GitHub release is
confirmed good. Rerunning the workflow against an existing tag stays safe:
files already on the index are skipped rather than treated as a failure.

Publishing needs one manual step that only the repository owner can take, and
it must be done before the first tag. PyPI has no `agent-parley` project yet,
and a trusted publisher cannot be added to a project that has no releases, so
add a *pending* publisher instead: PyPI, account settings, Publishing, "Add a
new pending publisher", GitHub, with PyPI project name `agent-parley`, owner
`suneel944`, repository `agent-parley`, workflow `release.yml`, and the
environment name left empty because the workflow declares no environment. The
first successful run creates the project and converts the pending publisher
into a normal one. Nothing is added to repository secrets.

A pending publisher does not reserve the name until it is first used, so
register it before tagging. The earlier candidate `agent-bridge` is permanently
unavailable: PyPI strips separators when comparing names, and an unrelated
`agentbridge` project already holds that form.

Download assets together and run `sha256sum --check SHA256SUMS`. The wheel needs
no third-party runtime packages. Development tools and the independent MCP test
client are locked in `uv.lock`.
