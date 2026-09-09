# Operations

## Install and upgrade

`make install` installs a package snapshot in uv's user executable directory.
If needed, run `uv tool update-shell` and open a new terminal. `make install-dev`
installs an editable checkout. `sudo env "PATH=$PATH" make install-system`
installs into `/usr/local/bin`, with its environment under `/opt/agent-bridge`.
Each user's runtime state remains private.

Before upgrading from 0.2, finish both sessions and run `agent-bridge down` using
the old installation. Preserve the entire state directory: it contains worktrees,
not just cache data. Install 0.3 and relaunch each lane. First startup imports mail
into a separate database and retains the original. Never run old and new versions
against the same state directory concurrently.

Upgrading between 0.3 releases needs no action: the store upgrades in place on
first use, keeping every message, claim and lease. Stop running sessions first,
as always, and do not point an older installation at an upgraded state
directory afterwards.

## Daily use

```sh
agent-bridge status
agent-bridge top
agent-bridge participant list
agent-bridge issue list
agent-bridge issue claim 42
agent-bridge issue offer 42 --to codex --summary "commit, checks, remaining work"
agent-bridge issue accept 42 --offer-id CURRENT_OFFER_ID
agent-bridge report --state ready --summary "Result" --evidence "Checks and results"
```

`issue offer --to` names another participant in the same project.

`issue list` prints each owner's session state and the age of its last
checkpoint, so a stalled lane is visible. Reclaiming that work still needs the
owner to release it, or an explicit offer and accept.

`agent-bridge top` watches every participant live: session state, event age,
branch with a `!` when a lane left its assigned branch, issues owned and
handoffs pending, unread and unacknowledged mail, held leases with the age of
the oldest, delivered context, denials against retained hook events, and served
MCP calls with rejections. The header carries server health and the project's
denial rate. The view is read-only and makes no model call; `q` leaves it.
Use `--once`, or pipe it, for one plain snapshot instead of a live view, and
`--interval` to change the redraw period. Session state follows the recorded
session process, not the session lock, so watching a lane never blocks a
launch. Denial counts cover the whole retained event log, the current file and
the one rotated file together, so reaching the byte cap does not reset a total;
served-call counts cover the most recent 2000 events per project.

## Participants, providers and accounts

```sh
agent-bridge provider list
agent-bridge credentials add account-1 --config-home ~/.claude-account-1
agent-bridge participant add claude-1 --provider claude --credentials account-1
agent-bridge run claude-1 --task "Work on issue 44"
agent-bridge provider add vendor --adapter claude --executable claude \
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
native CLI once. Agent Bridge stores directory paths and
variable names; it never stores tokens or keys, and rejects `--env` values whose
names look like credentials.

## Recovery and teardown

```sh
agent-bridge status
agent-bridge participant restore claude-1
agent-bridge participant retire claude-1
```

A lane that ends a session on the wrong branch, or on a detached HEAD, blocks
only its own participant; every other lane still launches. `status` prints the
actual branch whenever it differs from the assigned one.

`participant restore` returns one lane to its bridge branch. It refuses while
that participant has a running session, refuses on an uncommitted change, and
refuses when the current branch holds commits the bridge branch does not,
printing the command that keeps them. It never resets, cleans, stashes, or
force-switches, so no committed or uncommitted work is discarded.

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
state. Default state is `~/.local/state/agent-bridge`, mode 0700. Logs are in
`server.log`. Set `AGENT_BRIDGE_HOME` or pass `--home` for another private root.
Set `AGENT_BRIDGE_PORT` before first initialization to override port 8876.

Worktrees start at a captured commit and persist. Ignored environment files,
dependencies and untracked configuration are not copied. Set up each worktree
as needed. Review and integrate branches separately, then run the target repo's
combined verification gate.

## Plugins

Install the CLI first, then add the repository marketplace:

```sh
claude plugin marketplace add suneel944/agent-bridge
claude plugin install agent-bridge@agent-bridge-local
codex plugin marketplace add suneel944/agent-bridge
codex plugin add agent-bridge@agent-bridge-local
```

Both plugins provide `coordinate`; the launcher supplies MCP and native hooks.
Avoid installing the same skill from both personal and repo marketplaces. Start
a new native session after updates.

Repository installation does not imply public directory approval. For Claude,
validate `plugins/agent-bridge` with `claude plugin validate`, then use the
[community submission form](https://platform.claude.com/plugins/submit).
The official catalog is curated separately; see
[Claude's guide](https://code.claude.com/docs/en/plugins).

For Codex, follow [OpenAI's submission guide](https://developers.openai.com/plugins/deploy/submission).
This is a skills-only plugin. Submission requires a verified publisher, listing
and policy URLs, a skill bundle, and review cases. Neither catalog submission
has been made. CI builds artifacts; it does not submit review forms.

## Releases

Update package/plugin versions together and add a changelog entry. Run `make check`
and `make release-artifacts`. After review and merge, an annotated `vVERSION` tag
triggers release CI. It reruns the gate, creates a draft, uploads assets, downloads
and verifies their checksums, then publishes. Failed verification leaves a draft.

Download assets together and run `sha256sum --check SHA256SUMS`. The wheel needs
no third-party runtime packages. Development tools and the independent MCP test
client are locked in `uv.lock`.
