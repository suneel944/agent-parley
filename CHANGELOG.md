# Changelog

## [0.6.0](https://github.com/suneel944/agent-parley/compare/v0.5.0...v0.6.0) (2026-09-14)


### Features

* apply and show a plan file that records issue order and parallel groups (#207) ([#157](https://github.com/suneel944/agent-parley/issues/157))
* make writing coordination operations retry-safe across CLI and MCP (#206) ([#156](https://github.com/suneel944/agent-parley/issues/156))
* mark a lane idle when it holds unanswered mail and serves no calls (#202) ([#143](https://github.com/suneel944/agent-parley/issues/143))
* measure and show every second a lane spends idle or waiting (#203) ([#147](https://github.com/suneel944/agent-parley/issues/147))
* print every read-only command as JSON for scripts and other agents (#197) ([#150](https://github.com/suneel944/agent-parley/issues/150))
* query the ownership history of an issue, a lane or a claim (#205) ([#154](https://github.com/suneel944/agent-parley/issues/154))
* record a deadline and a retry budget on claims, offers and acknowledgements (#204) ([#152](https://github.com/suneel944/agent-parley/issues/152))
* refuse a mismatched launcher, plugin or store protocol at the boundary (#208) ([#159](https://github.com/suneel944/agent-parley/issues/159)) ([#199](https://github.com/suneel944/agent-parley/issues/199))
* refuse assistant attribution in a lane's branches, commits and pull requests (#198) ([#146](https://github.com/suneel944/agent-parley/issues/146))
* reserve named resources such as ports and databases, not only paths (#201) ([#149](https://github.com/suneel944/agent-parley/issues/149))

## [0.5.0](https://github.com/suneel944/agent-parley/compare/v0.4.0...v0.5.0) (2026-09-14)


### Features

* pause, resume, stop and restart a lane from the base checkout (#196) ([#153](https://github.com/suneel944/agent-parley/issues/153))
* run a recorded initialization command in every new lane before the agent starts (#195) ([#148](https://github.com/suneel944/agent-parley/issues/148))

### Bug fixes

* correlate completion reminders with the current lane pull request (#190) ([#177](https://github.com/suneel944/agent-parley/issues/177))
* include pending handoff offers in the recipient wake backlog (#191) ([#181](https://github.com/suneel944/agent-parley/issues/181))
* preserve successful issue release results when reminders fail (#192) ([#178](https://github.com/suneel944/agent-parley/issues/178))
* preserve the last resumable session after a failed launch (#189) ([#175](https://github.com/suneel944/agent-parley/issues/175))
* refresh recorded evidence when pushing an existing pull request (#193) ([#176](https://github.com/suneel944/agent-parley/issues/176))
* retain first-read and first-acknowledgement timestamps on retries (#187) ([#179](https://github.com/suneel944/agent-parley/issues/179))
* take a consistent event snapshot across log rotation (#194) ([#180](https://github.com/suneel944/agent-parley/issues/180))
* translate Copilot hook payloads before coordination enforcement (#188) ([#173](https://github.com/suneel944/agent-parley/issues/173))

## [0.4.0](https://github.com/suneel944/agent-parley/compare/v0.3.0...v0.4.0) (2026-09-13)

### Features

- Launch native Gemini CLI with a private MCP and hook settings overlay while
  preserving its native authentication and system policy.
  ([#118](https://github.com/suneel944/agent-parley/issues/118))
- Configure PR labels, milestone requirements and body templates per project;
  repositories can accept unlabelled issues.
  ([#115](https://github.com/suneel944/agent-parley/issues/115))
- Include recorded claim-window denials, reservations, conflict counts, gate
  results and an event-export digest when creating a lane's pull request.
  ([#140](https://github.com/suneel944/agent-parley/issues/140))
- Report native-process presence, recent activity and pending acknowledgement
  ages, with advisory reminders for waiting work.
  ([#130](https://github.com/suneel944/agent-parley/issues/130))
- Prompt claim holders when their work is released or a branch PR finishes;
  reminders do not transfer ownership.
  ([#129](https://github.com/suneel944/agent-parley/issues/129))
- Wake an eligible idle terminal or resume a recorded native session to inspect
  pending work. Preserve approval and partial-input guards, provide opt-outs,
  and limit attempts to three per backlog.
  ([#131](https://github.com/suneel944/agent-parley/issues/131))

### Bug fixes

- Protect event-file lifetimes during append, rotation and pruning, recheck
  rotation under the maintenance lock, and clean interrupted prune files.
  Append writers remain concurrent through a shared advisory lock.
  ([#105](https://github.com/suneel944/agent-parley/issues/105))
- Avoid reverse-DNS delays in local server startup and bound SQLite writer
  contention while retaining bounded reads. Add macOS to the CI gate and align
  operating and contributor documentation with the supported behavior.
  ([#119](https://github.com/suneel944/agent-parley/issues/119),
  [#120](https://github.com/suneel944/agent-parley/issues/120),
  [#123](https://github.com/suneel944/agent-parley/issues/123))

### Known limitations

- Copilot's native hook payloads still need translation before coordination
  guards can process them. Native CLI permissions remain separate.
  ([#173](https://github.com/suneel944/agent-parley/issues/173))
- Follow-ups cover failed-resume identity loss, stale evidence on existing PRs,
  reused-branch completion matching, reminder failures after issue release,
  receipt timestamp retries, snapshots during rotation, and waking for offers.
  See the [implementation audit](https://github.com/suneel944/agent-parley/blob/main/docs/releases.md#open-follow-ups-from-the-040-implementation-audit).

## [0.3.0](https://github.com/suneel944/agent-parley/compare/v0.2.0...v0.3.0) (2026-09-13)

### Bug fixes

- Preserve Copilot account settings, other MCP servers and existing hooks
  across launches; name the correct credentials command when a profile is
  missing. ([#108](https://github.com/suneel944/agent-parley/issues/108),
  [#92](https://github.com/suneel944/agent-parley/issues/92))
- Let Git network operations and merges finish without a kill timeout, stream
  verification output, and target the origin repository when creating PRs.
  ([#112](https://github.com/suneel944/agent-parley/issues/112))
- Fit the dashboard to terminal dimensions and retain missing-checkpoint
  warnings. ([#113](https://github.com/suneel944/agent-parley/issues/113))
- Wait briefly for checkpoint and issue locks, allow repeated Stop events after
  branch drift, permit exact renamed-branch repairs, and record inspection
  timeouts through the hook error contract. Apply command guards before
  ignoring child lifecycle state.
  ([#133](https://github.com/suneel944/agent-parley/pull/133),
  [#125](https://github.com/suneel944/agent-parley/issues/125))
- Bound SQLite writer contention, distinguish retryable errors, reconcile FTS5
  indexes and skip busy read telemetry. Commit reservation rebuilds and schema
  versions atomically so interrupted upgrades retain held leases.
  ([#98](https://github.com/suneel944/agent-parley/issues/98),
  [#133](https://github.com/suneel944/agent-parley/pull/133))
- Clean retired participant artifacts, preserve kept branches, exclude
  unreachable recipients, and require registration for lifecycle commands.
  ([#133](https://github.com/suneel944/agent-parley/pull/133))
- Add inbox receipt timestamps and pending-message filters within one response
  budget; validate offsets even for an empty inbox.
  ([#116](https://github.com/suneel944/agent-parley/issues/116))
- Add provider and credential removal, warn on preset overrides, and validate
  credential profiles before creating configuration directories.
  ([#117](https://github.com/suneel944/agent-parley/issues/117))
- Preserve safe Linux shutdown when Python has no pidfd wrappers and verify
  the project on Python 3.12, 3.13 and 3.14.
  ([#124](https://github.com/suneel944/agent-parley/issues/124))

### Documentation

- Explain inherited lane-token exposure and mitigation, document all commands
  and MCP tools, and update the installed coordination skill.
  ([#114](https://github.com/suneel944/agent-parley/issues/114),
  [#121](https://github.com/suneel944/agent-parley/issues/121),
  [#122](https://github.com/suneel944/agent-parley/issues/122))

## [0.2.0](https://github.com/suneel944/agent-parley/compare/v0.1.1...v0.2.0) (2026-09-11)

### Features

- Preview a lane merge and optionally require the repository's verification
  command before performing it.
  ([#60](https://github.com/suneel944/agent-parley/pull/60),
  [#65](https://github.com/suneel944/agent-parley/issues/65))
- Identify and stop lane sessions on macOS.
  ([#67](https://github.com/suneel944/agent-parley/issues/67))
- Open a PR from a lane's recorded report and claimed issues, using the native
  GitHub account. ([#70](https://github.com/suneel944/agent-parley/issues/70))
- Preserve pending work during setup and show claimed issue titles.
  ([#53](https://github.com/suneel944/agent-parley/issues/53),
  [#57](https://github.com/suneel944/agent-parley/issues/57))
- Mark reservations past their declared lifetime as stale without revoking
  them, and display usage already recorded by each native client.
  ([#71](https://github.com/suneel944/agent-parley/issues/71),
  [#73](https://github.com/suneel944/agent-parley/issues/73))
- Send operator messages from the CLI; thread and search a lane's coordination
  mail. ([#72](https://github.com/suneel944/agent-parley/issues/72),
  [#64](https://github.com/suneel944/agent-parley/issues/64))
- Add the Gemini endpoint preset and document other client configuration
  contracts. Native Gemini CLI support arrives later in 0.4.0.
  ([#74](https://github.com/suneel944/agent-parley/issues/74))
- Measure release eligibility from delivered product work.
  ([#63](https://github.com/suneel944/agent-parley/pull/63))

### Bug fixes

- Report a preserved stash by its full object ID so recovery identifies the
  exact entry. ([#80](https://github.com/suneel944/agent-parley/issues/80))

## [0.1.1](https://github.com/suneel944/agent-parley/compare/v0.1.0...v0.1.1) (2026-09-10)

### Documentation

- Add a square listing icon for the plugin directories. This release changes
  listing assets; it does not introduce a new runtime feature.
  ([#34](https://github.com/suneel944/agent-parley/pull/34))

## [0.1.0] - 2026-09-10

### Features

- Introduce separate Git worktrees for native coding-agent sessions on Linux,
  with each session retaining its own vendor authentication.
- Record atomic issue claims, explicit offer-and-accept handoffs and advisory
  dependencies. Timeouts and process exits never move ownership.
- Add advisory file reservations that identify conflicting owners and reasons.
- Provide bounded peer messaging with idempotent sends, paged bodies and
  explicit acknowledgements.
- Deliver coordination updates through lifecycle hooks, guard branch changes
  inside assigned lanes, and retain enforcement events.
- Add `agent-parley status`, the live `agent-parley top` dashboard, and the
  shared `coordinate` skill packaged for Claude Code and Codex.

### Archive status

This version is retained for historical reference. Use the
[latest release](https://github.com/suneel944/agent-parley/releases/latest)
for a current installation.

The 2026-09-10 audit found matching GitHub/PyPI wheels but different source
archives in five documentation, tooling and test files. Runtime package
contents match, and PyPI's source archive matches the original tag. Existing
assets and the tag are preserved; do not retry or rebuild this version.
See the [provenance audit](https://github.com/suneel944/agent-parley/issues/51).
