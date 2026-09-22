# Changelog

## [0.12.0](https://github.com/suneel944/agent-parley/compare/v0.11.0...v0.12.0) (2026-09-22)


### Features

* close the 0.12.0 autonomy gaps across the coordination loop (#381) ([#349](https://github.com/suneel944/agent-parley/issues/349)) ([#350](https://github.com/suneel944/agent-parley/issues/350)) ([#351](https://github.com/suneel944/agent-parley/issues/351)) ([#353](https://github.com/suneel944/agent-parley/issues/353)) ([#355](https://github.com/suneel944/agent-parley/issues/355)) ([#356](https://github.com/suneel944/agent-parley/issues/356)) ([#358](https://github.com/suneel944/agent-parley/issues/358)) ([#359](https://github.com/suneel944/agent-parley/issues/359)) ([#360](https://github.com/suneel944/agent-parley/issues/360)) ([#361](https://github.com/suneel944/agent-parley/issues/361)) ([#364](https://github.com/suneel944/agent-parley/issues/364)) ([#367](https://github.com/suneel944/agent-parley/issues/367)) ([#370](https://github.com/suneel944/agent-parley/issues/370)) ([#371](https://github.com/suneel944/agent-parley/issues/371)) ([#372](https://github.com/suneel944/agent-parley/issues/372))
* give an observed-complete claim a terminating transition (#380) ([#369](https://github.com/suneel944/agent-parley/issues/369))
* reclaim landed lane worktrees and branches (#375) ([#357](https://github.com/suneel944/agent-parley/issues/357))

### Bug fixes

* derive one lane state every status column reports from (#376) ([#362](https://github.com/suneel944/agent-parley/issues/362))
* group problems rows and prescribe remedies a lane can take (#377) ([#363](https://github.com/suneel944/agent-parley/issues/363))
* keep a lane's client identity across a new session id (#373) ([#352](https://github.com/suneel944/agent-parley/issues/352))
* stop a drifted service before it records the drift (#378) ([#365](https://github.com/suneel944/agent-parley/issues/365))
* time each hook decision and degrade a lane once (#374) ([#354](https://github.com/suneel944/agent-parley/issues/354))
* withdraw an orphan marker when the lane returns (#379) ([#368](https://github.com/suneel944/agent-parley/issues/368))

## [0.11.0](https://github.com/suneel944/agent-parley/compare/v0.10.0...v0.11.0) (2026-09-20)


### Features

* connect authorized work dispatch, verification, and recovery ([#323](https://github.com/suneel944/agent-parley/issues/323)) ([#326](https://github.com/suneel944/agent-parley/issues/326)) ([#327](https://github.com/suneel944/agent-parley/issues/327)) ([#328](https://github.com/suneel944/agent-parley/issues/328)) ([#332](https://github.com/suneel944/agent-parley/issues/332)) ([#333](https://github.com/suneel944/agent-parley/issues/333)) ([#334](https://github.com/suneel944/agent-parley/issues/334)) ([#335](https://github.com/suneel944/agent-parley/issues/335)) ([#336](https://github.com/suneel944/agent-parley/issues/336))

### Bug fixes

* close the 0.11.0 native work loop against live clients (#341) ([#332](https://github.com/suneel944/agent-parley/issues/332)) ([#333](https://github.com/suneel944/agent-parley/issues/333)) ([#335](https://github.com/suneel944/agent-parley/issues/335)) ([#336](https://github.com/suneel944/agent-parley/issues/336)) ([#340](https://github.com/suneel944/agent-parley/issues/340)) ([#342](https://github.com/suneel944/agent-parley/issues/342)) ([#343](https://github.com/suneel944/agent-parley/issues/343)) ([#344](https://github.com/suneel944/agent-parley/issues/344))
* preserve native coordination across launch and recovery (#339) ([#336](https://github.com/suneel944/agent-parley/issues/336))

## [0.10.0](https://github.com/suneel944/agent-parley/compare/v0.9.1...v0.10.0) (2026-09-17)


### Features

* add version, show verbs, --json everywhere and one repo flag (#317) ([#270](https://github.com/suneel944/agent-parley/issues/270))
* close the 0.10.0 milestone across coordination, listing and startup ([#144](https://github.com/suneel944/agent-parley/issues/144)) ([#250](https://github.com/suneel944/agent-parley/issues/250)) ([#252](https://github.com/suneel944/agent-parley/issues/252)) ([#254](https://github.com/suneel944/agent-parley/issues/254)) ([#255](https://github.com/suneel944/agent-parley/issues/255)) ([#263](https://github.com/suneel944/agent-parley/issues/263)) ([#264](https://github.com/suneel944/agent-parley/issues/264)) ([#265](https://github.com/suneel944/agent-parley/issues/265)) ([#267](https://github.com/suneel944/agent-parley/issues/267)) ([#268](https://github.com/suneel944/agent-parley/issues/268)) ([#273](https://github.com/suneel944/agent-parley/issues/273)) ([#274](https://github.com/suneel944/agent-parley/issues/274)) ([#302](https://github.com/suneel944/agent-parley/issues/302))
* deliver coordination to a lane whose CLI raises no hooks (#318) ([#269](https://github.com/suneel944/agent-parley/issues/269))
* keep a project-wide decision log that every lane can search (#316) ([#266](https://github.com/suneel944/agent-parley/issues/266))
* let a lane wait for its next mail over MCP (#319) ([#260](https://github.com/suneel944/agent-parley/issues/260))
* let an operator read the mail of a lane from the main checkout (#321) ([#283](https://github.com/suneel944/agent-parley/issues/283))
* recommend the next issue for a lane with the reason for each place (#320) ([#262](https://github.com/suneel944/agent-parley/issues/262))

### Bug fixes

* reap the launcher a wake starts so the service sheds zombies (#314) ([#301](https://github.com/suneel944/agent-parley/issues/301))
* report no age for a lane that has recorded no activity (#313) ([#312](https://github.com/suneel944/agent-parley/issues/312))

### Performance

* resolve a project directory from a cached root index (#315) ([#303](https://github.com/suneel944/agent-parley/issues/303))

## [0.9.1](https://github.com/suneel944/agent-parley/compare/v0.9.0...v0.9.1) (2026-09-15)


### Bug fixes

* bring the coordination service back after a reboot or a drift exit (#307) ([#298](https://github.com/suneel944/agent-parley/issues/298))
* make the service report its lifecycle and bound a served decision (#308) ([#299](https://github.com/suneel944/agent-parley/issues/299)) ([#300](https://github.com/suneel944/agent-parley/issues/300))
* ship the plugin manifests inside the wheel so doctor reads a protocol (#306) ([#297](https://github.com/suneel944/agent-parley/issues/297))

## [0.9.0](https://github.com/suneel944/agent-parley/compare/v0.8.0...v0.9.0) (2026-09-15)


### Bug fixes

* complete the 0.9.0 stability milestone with a matching command surface and documentation (#291) ([#251](https://github.com/suneel944/agent-parley/issues/251)) ([#253](https://github.com/suneel944/agent-parley/issues/253)) ([#261](https://github.com/suneel944/agent-parley/issues/261)) ([#280](https://github.com/suneel944/agent-parley/issues/280)) ([#281](https://github.com/suneel944/agent-parley/issues/281)) ([#282](https://github.com/suneel944/agent-parley/issues/282)) ([#284](https://github.com/suneel944/agent-parley/issues/284)) ([#285](https://github.com/suneel944/agent-parley/issues/285)) ([#289](https://github.com/suneel944/agent-parley/issues/289))
* fall back in-process when the service answers without a status line (#272) ([#271](https://github.com/suneel944/agent-parley/issues/271))

### Performance

* answer a served hook call from a shell client and start Python only on the fallback path (#278) ([#256](https://github.com/suneel944/agent-parley/issues/256))
* build a status frame from one store connection and one issue snapshot per project (#277) ([#258](https://github.com/suneel944/agent-parley/issues/258))
* import asyncio, curses and the dashboard only on the commands that use them (#275) ([#259](https://github.com/suneel944/agent-parley/issues/259))
* read a lane's branch from the worktree HEAD file instead of a git subprocess (#276) ([#257](https://github.com/suneel944/agent-parley/issues/257))

## [0.8.0](https://github.com/suneel944/agent-parley/compare/v0.7.0...v0.8.0) (2026-09-15)


### Features

* complete the 0.8.0 milestone with WSL checks, a selectable forge, state archives, a served hook client and OpenCode and Amp adapters (#249) ([#142](https://github.com/suneel944/agent-parley/issues/142)) ([#166](https://github.com/suneel944/agent-parley/issues/166)) ([#171](https://github.com/suneel944/agent-parley/issues/171)) ([#172](https://github.com/suneel944/agent-parley/issues/172)) ([#184](https://github.com/suneel944/agent-parley/issues/184)) ([#235](https://github.com/suneel944/agent-parley/issues/235))
* follow one lane's coordination events as a stream (#245) ([#164](https://github.com/suneel944/agent-parley/issues/164))
* forecast a reservation collision from co-change history before a lane starts (#248) ([#233](https://github.com/suneel944/agent-parley/issues/233))
* generate shell completion for commands, participants, providers and issues (#240) ([#163](https://github.com/suneel944/agent-parley/issues/163))
* list every lane, claim and store problem on one triage screen (#246) ([#234](https://github.com/suneel944/agent-parley/issues/234))
* record an advisory token, call and hour budget per lane and mark it when crossed (#244) ([#170](https://github.com/suneel944/agent-parley/issues/170))
* spill an oversized report, message or offer to an attachment and pass a reference (#247) ([#167](https://github.com/suneel944/agent-parley/issues/167))
* warn a lane when an operator edit lands on a path it has reserved (#243) ([#232](https://github.com/suneel944/agent-parley/issues/232))

### Bug fixes

* rotate a lane's durable report log instead of growing it without bound (#237) ([#230](https://github.com/suneel944/agent-parley/issues/230))

### Performance

* cut the lifecycle hook's import cost so every tool call clears faster (#238) ([#229](https://github.com/suneel944/agent-parley/issues/229))

## [0.7.0](https://github.com/suneel944/agent-parley/compare/v0.6.0...v0.7.0) (2026-09-14)


### Features

* deliver an operator message or offer at a time or on a condition (#225) ([#160](https://github.com/suneel944/agent-parley/issues/160))
* export coordination metrics as Prometheus text or JSON (#222) ([#155](https://github.com/suneel944/agent-parley/issues/155))
* fit top to the terminal, page its rows and shape it from the keyboard (#223) ([#151](https://github.com/suneel944/agent-parley/issues/151))
* integrate, select, table and gate lanes as one coordination surface (#239) ([#141](https://github.com/suneel944/agent-parley/issues/141)) ([#158](https://github.com/suneel944/agent-parley/issues/158)) ([#162](https://github.com/suneel944/agent-parley/issues/162)) ([#168](https://github.com/suneel944/agent-parley/issues/168)) ([#169](https://github.com/suneel944/agent-parley/issues/169))
* let the operator offer an issue to a lane with issue assign (#226) ([#165](https://github.com/suneel944/agent-parley/issues/165))
* offer unclaimed and shed-able work to a fit idle lane (#224) ([#145](https://github.com/suneel944/agent-parley/issues/145))

### Bug fixes

* keep the diagnostic path open when coordination fails (#221) ([#213](https://github.com/suneel944/agent-parley/issues/213))
* refuse a store behind the running code in the doctor report (#219) ([#215](https://github.com/suneel944/agent-parley/issues/215))
* report the launcher version of the code that runs (#218) ([#216](https://github.com/suneel944/agent-parley/issues/216))
* retain the cause of a coordination outage past its recovery (#220) ([#217](https://github.com/suneel944/agent-parley/issues/217))

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
