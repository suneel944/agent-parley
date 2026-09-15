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
| `polled` | a launcher-owned thread reads the mailbox on an interval and publishes it to `STATE/PROJECT/NAME-delivery.md`, which the lane's prompt tells it to read each turn | any adapter missing `SessionStart`, `UserPromptSubmit`, `PreToolUse` or `Stop`; `amp` today | deny a tool call, hold a turn open, or guarantee the lane reads the file; budget threshold notices are not carried, and a missing required guard still refuses the launch |

`AGENT_PARLEY_DELIVERY_SECONDS` sets the interval for a polled lane, 20s by
default. Delivery is recorded in that lane's event log like a served
checkpoint, so `agent-parley top` reports its delivered context in `CONTEXT`.

## Accounts

`claude`, `codex`, `gemini`, `opencode` and `amp` use their native accounts. The
`deepseek`, `kimi` and `grok` presets carry no endpoint, so their base URL and
key must be exported in the launching shell; the launcher refuses to start when
a required variable is unset rather than falling back to another account.
Coordination state records variable names and config directories, never
credential values.

Credential profiles point a provider's config-home variable at a separate
directory, so one provider can run under several logins. Up to 32 participants
per project.

```sh
agent-parley credentials add account-2 --config-home ~/.claude-account-2
agent-parley run claude-2 --provider claude --credentials account-2
```

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
configuration directory your own sessions use. `gemini`, `opencode` and `amp`
each receive a lane-private copy of their native configuration that lives in
Agent Parley's state directory, never in the repository, and is rebuilt on every
launch and removed by `participant retire`. `amp` is refused at launch until Amp
can raise the required guards.

[Operations](operations.md#other-agent-clis) records what each CLI supports,
where its MCP and hook configuration lives, and which behaviour is verified
against a stub rather than a live session.
