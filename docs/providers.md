# Providers and accounts

A provider states which native CLI drives a participant and how that CLI reaches
a model. Every provider names one of the adapters, and the adapter decides how
that CLI is handed its MCP server, its coordination prompt and its hooks. Two of
them take a published plugin, which is why two plugin installations cover every
model-endpoint preset. The full configuration reference is in
[Operations](operations.md#participants-providers-and-accounts).

| Provider | Native CLI it drives | Plugin that carries `coordinate` |
| --- | --- | --- |
| `claude` | `claude` | Claude Code |
| `codex` | `codex` | Codex |
| `copilot` | `copilot` | none; the launcher writes that lane's files |
| `gemini` | `gemini` | none; the launcher writes a private settings overlay |
| `opencode` | `opencode` | none; the launcher writes a private config directory and plugin |
| `amp` | `amp` | none; the launcher writes a private settings file, and refuses to launch until Amp raises thread start and idle events |
| `deepseek`, `kimi`, `grok` | `claude` or `codex`, vendor endpoint | that adapter's plugin |
| your own, via `agent-parley provider add` | the adapter you name | that adapter's plugin |

`agent-parley provider list` prints each definition with `unavailable_hooks`,
the lifecycle events that CLI cannot deliver: `PermissionRequest` for `gemini`,
`SessionEnd` for `opencode`, every event except `PreToolUse` and `PostToolUse`
for `amp`, none for the others. A launch refuses an adapter that cannot deliver
`SessionStart`, `PreToolUse` or `Stop` instead of running without those guards;
today that refuses `amp` in one sentence naming the missing events.

The same listing prints `delivery`, the path coordination takes into a lane on
that provider.

| `delivery` | How a lane is told | Which adapters | What it cannot do |
| --- | --- | --- | --- |
| `hooks` | a native lifecycle event returns bounded context at a turn boundary | `claude`, `codex`, `copilot`, `gemini`, `opencode` and every preset built on them | nothing further; this is the full path |
| `polled` | a launcher-owned thread reads the mailbox on an interval and publishes it to `STATE/projects/PROJECT_KEY/NAME-delivery.md`, which the lane's prompt tells it to read each turn | an adapter that lacks a delivery event (`SessionStart`, `UserPromptSubmit`, `PreToolUse` or `Stop`) but has every required guard; no shipped adapter qualifies today, and `amp`, which `provider list` reports as `polled`, is refused at launch | deny a tool call, hold a turn open, or guarantee the lane reads the file; budget threshold notices are not carried, and a missing required guard still refuses the launch |

`AGENT_PARLEY_DELIVERY_SECONDS` sets the interval for a polled lane, 20s by
default. Delivery is recorded in that lane's event log like a served
checkpoint, so `agent-parley top` reports its delivered context in `CONTEXT`.

### What each adapter does

| Capability | `claude` | `codex` | `copilot` | `gemini` | `opencode` | `amp` |
| --- | --- | --- | --- | --- | --- | --- |
| MCP registration | `--mcp-config` file | `-c mcp_servers.*` | profile `mcp-config.json` | overlay `mcpServers` | overlay `mcp` | overlay `amp.mcpServers`, literal token |
| Hooks | `--settings` | `-c hooks.*` | profile `settings.json` | overlay `hooks` | overlay plugin | overlay `amp.hooks`, tool events only |
| Task handed over | positional, interactive | positional, interactive | `-p` | `--prompt-interactive` | `--prompt` | refused before launch |
| Resume argument | `--resume ID` | `resume ID` | `--resume ID` | `--resume ID` | `--session ID` | `threads continue ID` |
| Rotation to a fresh session | yes | yes | yes | yes | yes | n/a |
| Service-resume opt-ins applied | yes | no | no | no | no | n/a |
| Refused-tool event | `PermissionDenied` | none | none | none | none | none |
| Blocked-on-approval source | hook | hook | hook | dialog watcher | hook | dialog watcher |
| Removed by `participant retire` | `-mcp.json` | nothing to remove | the lane's hooks in the profile, and the MCP entry once no lane hook remains | overlay file | overlay directory | overlay file |
| Forked-session rebind | Linux and macOS | Linux and macOS | Linux and macOS | Linux and macOS | Linux and macOS | Linux and macOS |
| Token reading | transcript `usage` | rollout `token_count` totals | `session.shutdown` totals | chat log message `tokens` | `opencode.db` session counters | not measured |

Every row is read from the adapter branches in `agent_parley/launch.py`, the
retire path and `agent_parley/process.py`; the token row is read from
`agent_parley/records.py`. A forked or resumed session started outside the
launcher is found by the working directory of its process, which Linux reads
from `/proc` and macOS from `lsof`.

### Token reading

`TOKENS` and token budgets read what each native client already wrote to
disk, under the config home the lane launched with. No vendor is asked.

- `claude` reads the `usage` of each assistant message in the session
  transcripts.
- `codex` reads the running total of the `token_count` events in the lane's
  rollouts.
- `copilot` reads the `session.shutdown` event in
  `session-state/<id>/events.jsonl` under `COPILOT_HOME` (default
  `~/.copilot`). The event carries the session total across resumes, and
  Parley sums its input and output tokens over every model. Copilot CLI
  keeps per-call usage only in its live event stream, so a session that is
  still running reads 0 until it ends.
- `gemini` reads `tokens.total` of each `gemini` message in the chat logs
  under `.gemini/tmp/<project>/chats` in `GEMINI_CLI_HOME` (default `~`).
  The project directory comes from `.gemini/projects.json`. A message that
  the client rewrites after an update is counted once.
- `opencode` reads the input, output, reasoning, cache read and cache write
  counters of each session in `opencode.db` under `$XDG_DATA_HOME/opencode`
  (default `~/.local/share/opencode`), or in the file `OPENCODE_DB` names. A
  subagent session is folded into its parent. `OPENCODE_CONFIG_DIR` does not
  move that database, so every OpenCode account in one lane directory shares
  it. Releases before the SQLite store kept JSON files, which are not read.
- `amp` keeps no local usage record, so an `amp` lane is not measured. Its
  `TOKENS` cell prints `n/a`, a token limit warns, and `budget enforce
  --tokens` refuses unless `--allow-unmeasured` is given.

`agent-parley doctor` lists the providers with and without a token reader on
its `tokens` line.

### How idle, blocked and prompt states are detected

A lane's state comes from a native hook when its client raises one, and
otherwise from the launcher's dialog watcher, which reads the lane's own
terminal, or from the supervision poll's liveness sample of the session
process. `status`, `top` and `problems` mark a state the watcher or the
liveness sample set as inferred, for example `blocked: approval 2m; inferred
by dialog watcher, no PermissionRequest hook`, and name the hook the client
lacks. `status --json` carries the same reading under each lane's
`provenance`: its `source`, whether it is `inferred`, and the `gap`.

For an adapter missing `PermissionRequest` or `Stop`, the poll reads the
dialog the watcher published on every pass while the lane is idle or blocked,
and records the lane blocked under that dialog's cause, so an approval or
question prompt still reaches `problems` as a row for the operator.

| Provider | Hooks it cannot raise | Delivery | Idle | Blocked on approval | Other prompts |
| --- | --- | --- | --- | --- | --- |
| `claude`, `codex`, `copilot` | none | `hooks` | `Stop` hook | `PermissionRequest` hook | dialog watcher |
| `gemini` | `PermissionRequest` | `hooks` | `Stop` hook | dialog watcher, read every poll | dialog watcher |
| `opencode` | `SessionEnd` | `hooks` | `Stop` hook | `PermissionRequest` hook | dialog watcher |
| `amp` | `SessionStart`, `UserPromptSubmit`, `PermissionRequest`, `Stop`, `SessionEnd` | `polled` | liveness sample | dialog watcher, read every poll | dialog watcher |

A hook-delivered lane idle at its prompt receives mail only when something
makes its client raise a hook, such as a wake the service sends; a polled lane
reads its delivery file on its own interval.

## Accounts

### The three names a lane carries

Provider, credential profile and participant are three separate registries that
accept the same-looking names, and a command that reads one of them never falls
back to another.

| Name | What it names | Defined by | Selected by |
| --- | --- | --- | --- |
| Provider | the native CLI a lane starts, the adapter that configures it, and any endpoint it rides | a built-in preset, or `agent-parley provider add` | `--provider` |
| Credential profile | one account of a CLI, recorded as a configuration directory and variable names | `agent-parley credentials add` | `--credentials` |
| Participant | one lane: its worktree, its branch and the identity peers address | `agent-parley run NAME`, or `agent-parley participant add NAME` | the name itself |

A participant is created by its first `run`; `participant add` only prepares one
in advance. The name must match `[a-z0-9][a-z0-9_-]{0,38}`, is unique within the
repository, and a project holds at most 32 participants.

`--provider` defaults to the participant name. `agent-parley run claude-2` with
no other flag therefore looks for a provider called `claude-2` and refuses with
`No provider named 'claude-2'; a new lane name needs --provider.` and
`next: agent-parley run claude-2 --provider claude`, even though the `claude`
preset exists. Name the provider whenever the participant is not named after
it.

### Which names are already taken

```sh
agent-parley participant list
agent-parley provider list
agent-parley credentials list
```

`participant list` prints one line per lane, and its last field is the account:

```
claude-2: provider claude; identity claude-2; account-2
```

A trailing `default account` means that lane uses the CLI's own login, so a
profile you expected to be bound is not. `provider list` and `credentials list`
print the two other registries; `provider show NAME` prints one definition with
the lifecycle events its adapter cannot deliver, and `credentials show NAME`
prints one account profile with every recorded override reported by name only.

### Which account a provider reaches

`claude`, `codex`, `copilot`, `gemini`, `opencode` and `amp` use their native
accounts, and their sign-in stays with the native CLI. The `deepseek`, `kimi`
and `grok` presets carry no endpoint, so their base URL and key must be exported
in the launching shell; the launcher refuses to start when a required variable
is unset rather than falling back to another account. Coordination state records
variable names and config directories, never credential values.

The service-resume opt-in `approve_bridge_tools` (see
[Lanes](lanes.md#requiring-your-own-decision)) is carried into each CLI's own
per-server allow, scoped to the `agent_parley` MCP server and nothing else:

| Adapter | How a recorded `approve_bridge_tools` reaches the session |
| --- | --- |
| `claude` | `permissions.allow` in the `--settings` the launch passes |
| `codex` | `-c mcp_servers.agent_parley.default_tools_approval_mode="approve"`, from Codex CLI 0.153.0 |
| `copilot` | `--allow-tool agent_parley` |
| `gemini` | `trust: true` on the `agent_parley` server in the lane's settings overlay |
| `opencode` | `permission` `agent_parley_*: allow` in the lane's `opencode.json` overlay |
| `amp` | none; Amp offers no per-server approval the launcher can pass |

A lane on any adapter but `amp` that the service could resume, with no opt-in
recorded, gets the same standard-error warning and `setup gap: bridge tool
approval not recorded` wake result as a `claude` lane. An opt-in the CLI
cannot carry is never dropped silently: `approval resume --bridge-tools on
--participant NAME` refuses it for an `amp` lane, a project-wide opt-in is
named on standard error when such a lane launches and under `approvals` in
`doctor`, and a `codex` lane whose `codex --version` is older than 0.153.0 or
unreadable starts without the override and says so. `auto_mode` is the
`claude` CLI's own auto permission mode; it applies to `claude` lanes only,
and `approval resume --auto-mode on --participant NAME` refuses any other
adapter by name.

Each preset names one configuration-home variable. A credential profile's
`--config-home` is applied to that variable for the launch, which is what makes
a second account of the same provider possible.

| Provider | Variable a profile sets | Variables the launch requires from your shell |
| --- | --- | --- |
| `claude` | `CLAUDE_CONFIG_DIR` | none |
| `codex` | `CODEX_HOME` | none |
| `copilot` | `COPILOT_HOME` | none; a profile is required for this adapter |
| `gemini` | `GEMINI_CLI_HOME` | none |
| `opencode` | `OPENCODE_CONFIG_DIR` | none |
| `amp` | `AMP_SETTINGS_FILE` | none |
| `deepseek`, `kimi` | `CLAUDE_CONFIG_DIR` | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` |
| `grok` | `CODEX_HOME` | `OPENAI_BASE_URL`, `OPENAI_API_KEY` |

Those variable names are the preset definitions in `agent_parley/roster.py`, and
a launch refuses with `Export these before launching:` and the missing names
rather than starting on another account. `provider add --require-env` adds the
same requirement to a definition of your own, and the same flag on
`credentials add` adds it to one account.

Three adapters are handed a lane-private copy of their native configuration
instead of command-line arguments, so the launcher sets one more variable itself
for that launch only: `GEMINI_CLI_SYSTEM_SETTINGS_PATH` for `gemini`,
`OPENCODE_CONFIG_DIR` for `opencode` and `AMP_SETTINGS_FILE` for `amp`, each
built from the file or directory the profile selected. A `copilot` lane instead
gets `mcp-config.json` and `settings.json` written into the `COPILOT_HOME` its
profile names, which is why that adapter refuses to launch without a profile
rather than writing this lane's hooks into the configuration directory your own
sessions use. Give every `copilot` lane its own profile; two lanes on one
profile refuse each other's tool calls, because both lanes' hooks run in both
sessions.

### Run a second account of the same provider

Define the profile, sign in to it once with the native CLI, then launch a new
participant that names both the provider and the profile:

```sh
agent-parley credentials add account-2 --config-home ~/.claude-account-2
CLAUDE_CONFIG_DIR=~/.claude-account-2 claude
agent-parley run claude-2 --provider claude --credentials account-2
```

`credentials add` creates the directory with owner-only permissions and records
its path. It records no token: the second line is the native CLI performing its
own sign-in inside that directory, and it is the only step that touches an
account. Use that provider's own variable from the table above in place of
`CLAUDE_CONFIG_DIR` for another CLI.

The launch prints the binding it used:

```
claude-2 (claude, account-2): /path/to/lane
```

and `agent-parley participant list` reports the same binding afterwards. If
either prints `default account`, the profile was not bound; see the next
section, because a relaunch with the flag will not change it.

### Provider and profile are recorded once the participant exists

The provider and profile are recorded when the lane is created, and a later
`run` or `participant add` for the same name with a different pair is refused:

```
Participant claude-2 already uses provider claude with the default account.
```

A default-account lane runs on the client's own default configuration home on
every launch and resume. A configuration-home variable such as
`CLAUDE_CONFIG_DIR` or `CODEX_HOME` exported by the shell that runs `run`, or
by the one that started the service, is removed from the lane's environment;
only a credential profile selects another account.

Passing the same pair again is accepted and changes nothing. There is no command
that rebinds a lane, because the lane's configuration, its registered identity
and its native session were all built for the account it was created with. The
route is to retire that participant and add it again with the flags you meant:

```sh
agent-parley participant stop claude-2
agent-parley participant retire claude-2
agent-parley run claude-2 --provider claude --credentials account-2
```

`retire` refuses while a session is running, refuses while the lane's
checkpoint lock is busy, and refuses a lane with uncommitted changes, so commit
or preserve the work first; it never discards work. Ignored files in the
worktree are deleted with it only after you confirm them by name, or with
`--yes`. Commits
the lane made are kept on its branch, and it says so. It also refuses a lane
whose ready work still awaits integration. Its issue claims return to the pool,
its reservations are released and granted to queued peers, and mail it has not
read or acknowledged is marked superseded. Messages are preserved.
The replacement lane is a fresh worktree on the next free lane branch, so it
does not continue the retired branch: merge that branch, or branch from it with
Git, if the new lane should carry that work.

### `provider add` defines a CLI, not an account

`provider add` is for a native CLI that no preset ships, or for an endpoint
override of one that does. It requires `--adapter` and `--executable`, and a
definition that reuses a preset name shadows that preset until
`provider remove` restores it. It is not how a second account is added; a
credential profile is.

Shell aliases and wrapper functions are not consulted either. The launcher
resolves a provider's executable on `PATH`, so an alias such as
`alias claude-p2='CLAUDE_CONFIG_DIR=... claude'` is invisible to it and cannot
be passed to `--provider`. A credential profile expresses exactly what that
alias expresses, because the launch sets the provider's configuration-home
variable to the profile's directory.

`run` checks for that executable before it registers anything. When the
provider's CLI is not on `PATH`, the launch is refused with no lane added to
the roster and no worktree or branch created, and the refusal's `next:` line
is the same `agent-parley run` command to repeat once the CLI is installed and
signed in.

Removing a definition leaves participant references intact. Redefine that name
before relaunching a lane that uses it. A removed provider override immediately
reveals its built-in preset, if any.

## Gemini

`agent-parley run gemini` starts Gemini CLI with a lane-private MCP and hook
overlay while preserving native system settings and authentication. Existing
explicit provider definitions keep their adapter until you update them: a local
`gemini` definition that rides `claude` or `codex` through a vendor endpoint
shadows the preset and keeps working unchanged, and `provider remove gemini`
reveals the native preset again.

## OpenCode

`agent-parley run opencode` starts OpenCode with a lane-private copy of its
configuration directory, selected with `OPENCODE_CONFIG_DIR`, that adds the MCP
server under `mcp` and one plugin under `plugin/` that runs the checkpoint hook
command for each supported plugin event. The launch path is verified against a
stub executable and the exact hook command; a live OpenCode session has not been
exercised, see [Operations](operations.md#other-agent-clis).

## Amp

`agent-parley run amp` builds a lane-private copy of Amp's `settings.json`,
selected with `AMP_SETTINGS_FILE` and `--settings-file`, that adds the MCP
server under `amp.mcpServers` and one `amp.hooks` entry per tool event that runs
the checkpoint hook command. Amp raises no thread start or idle event, so
`SessionStart` and `Stop` cannot run and the launch is refused rather than
started unguarded; the overlay, hook translation and `threads continue ID`
resume are verified against a stub executable only, see
[Operations](operations.md#other-agent-clis).

## Other agent CLIs

Six adapters cover the native configuration contracts. `claude` and `codex` take
MCP servers, the coordination prompt and lifecycle hooks as command-line
arguments. `copilot` reads them from files instead, so Agent Parley writes
`mcp-config.json` and `settings.json` into that lane's own Copilot
configuration directory; a `copilot` lane therefore requires a credential
profile, and the launcher refuses without one rather than writing hooks into the
configuration directory your own sessions use. Give every `copilot` lane its
own profile; two lanes on one profile refuse each other's tool calls.
`gemini`, `opencode` and `amp`
each receive a lane-private copy of their native configuration that lives in
Agent Parley's state directory, never in the repository, and is rebuilt on every
launch and removed by `participant retire`. `amp` is refused at launch until Amp
can raise the required guards.

[Operations](operations.md#other-agent-clis) records what each CLI supports,
where its MCP and hook configuration lives, and which behaviour is verified
against a stub rather than a live session.
