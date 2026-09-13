# Changelog

## Unreleased

### Fixes

- Preserve Copilot account settings, other MCP servers and existing hooks
  across lane launches (#108).
- Let Git network operations and merges finish without a kill timeout, stream
  verification output, and pin pull requests to the origin repository (#112).
- Fit the live dashboard to terminal dimensions, mark hidden participants,
  and retain the missing-checkpoint warning (#113).
- Guard sub-agent tool commands before ignoring child lifecycle state (#125).
- Add inbox receipt timestamps and pending-message filters under one response
  budget, and validate body offsets even when the inbox is empty (#116).
- Add provider and credential removal, warn on preset overrides, and validate
  credential profiles before creating their configuration directories (#117).
- Document inherited lane-token exposure and mitigation, list all commands and
  MCP tools, and update the installed coordination skill (#114, #121, #122).
- Run the required CI gate on Python 3.12, 3.13 and 3.14 and preserve safe
  Linux shutdown on Python builds without pidfd wrappers (#124).

- Wait briefly for checkpoint and issue locks without queuing session launches
  (#101).
- Allow a repeated Stop after branch drift, and permit exact repair when the
  assigned branch was renamed (#102, #126).
- Report Git inspection timeouts through the hook failure contract and retain
  an activity update and event record (#107).
- Distinguish retryable SQLite contention from permanent operational errors,
  reconcile FTS5 triggers at startup, and let reads skip busy telemetry writes
  (#103, #104).
- Keep reservation rebuilds and their schema version in one transaction so an
  interrupted upgrade preserves held leases (#106).
- Remove retired participant artifacts and explain how to preserve a kept
  branch before reusing its participant name (#109).
- Exclude the operator and revoked participants from addressable rosters and
  reject messages they cannot receive (#110).
- Require registration for verification, retirement, and merge commands;
  `verify show` reads without creating project state (#111).

## [0.2.0](https://github.com/suneel944/agent-parley/compare/v0.1.1...v0.2.0) (2026-09-11)


### Features

* gate a lane merge on the repository's verification command ([#66](https://github.com/suneel944/agent-parley/issues/66)) ([86eef2a](https://github.com/suneel944/agent-parley/commit/86eef2a8b8e22bdc6579b73c60158d2e18d5af6a)), closes [#65](https://github.com/suneel944/agent-parley/issues/65)
* identify and stop lane sessions on macOS ([#68](https://github.com/suneel944/agent-parley/issues/68)) ([ff0660a](https://github.com/suneel944/agent-parley/commit/ff0660aafd389dea88a87ef53eb03411ccda112d)), closes [#67](https://github.com/suneel944/agent-parley/issues/67)
* measure release eligibility from delivered product work ([#63](https://github.com/suneel944/agent-parley/issues/63)) ([3c74059](https://github.com/suneel944/agent-parley/commit/3c740599606672c7e26e0ae612900de6a2618dee))
* name claimed issues instead of showing a bare number ([#58](https://github.com/suneel944/agent-parley/issues/58)) ([93b6ad6](https://github.com/suneel944/agent-parley/commit/93b6ad6e6f91fefecd178503acdeed7ca7d8b018)), closes [#57](https://github.com/suneel944/agent-parley/issues/57)
* open a pull request from a lane ([#75](https://github.com/suneel944/agent-parley/issues/75)) ([a01cd70](https://github.com/suneel944/agent-parley/commit/a01cd7012cca905406893feb8d0c7a1ba8abd14b)), closes [#70](https://github.com/suneel944/agent-parley/issues/70)
* preserve pending work instead of refusing to start ([#54](https://github.com/suneel944/agent-parley/issues/54)) ([c953cf3](https://github.com/suneel944/agent-parley/commit/c953cf3ca2b0c670f83f50e31c11fe65651d2317)), closes [#53](https://github.com/suneel944/agent-parley/issues/53)
* preview a lane merge before performing it ([#60](https://github.com/suneel944/agent-parley/issues/60)) ([6b5cf29](https://github.com/suneel944/agent-parley/commit/6b5cf29a128d9084021bf48c75537affe17302ea))
* report a lease past its declared time to live as stale ([#78](https://github.com/suneel944/agent-parley/issues/78)) ([0d1f5c1](https://github.com/suneel944/agent-parley/commit/0d1f5c1b8ca984acfa8382def934678555801fc2)), closes [#71](https://github.com/suneel944/agent-parley/issues/71)
* report what each lane's own client recorded for its session ([#79](https://github.com/suneel944/agent-parley/issues/79)) ([ef44a2b](https://github.com/suneel944/agent-parley/commit/ef44a2b9fa721d515d4ce433d91ffaa04f799d2f)), closes [#73](https://github.com/suneel944/agent-parley/issues/73)
* send a lane an operator message from the command line ([#76](https://github.com/suneel944/agent-parley/issues/76)) ([929c255](https://github.com/suneel944/agent-parley/commit/929c2555ed62b5eb017bed9e1d7fd76e1803adb4)), closes [#72](https://github.com/suneel944/agent-parley/issues/72)
* ship a gemini preset and document the other agent clients ([#77](https://github.com/suneel944/agent-parley/issues/77)) ([8dfea4f](https://github.com/suneel944/agent-parley/commit/8dfea4f84fb852da4e939361bc20c1d265956020)), closes [#74](https://github.com/suneel944/agent-parley/issues/74)
* thread and search a lane's coordination mail ([#69](https://github.com/suneel944/agent-parley/issues/69)) ([65b625a](https://github.com/suneel944/agent-parley/commit/65b625af2a0d9f4fb7f36e609ba735a8a875e190)), closes [#64](https://github.com/suneel944/agent-parley/issues/64)


### Bug fixes

* name the preserved stash entry by its full object name ([#81](https://github.com/suneel944/agent-parley/issues/81)) ([b4f3a05](https://github.com/suneel944/agent-parley/commit/b4f3a05c38eb6593368aa1bce5ab26f2a6e0d960)), closes [#80](https://github.com/suneel944/agent-parley/issues/80)

## [0.1.1](https://github.com/suneel944/agent-parley/compare/v0.1.0...v0.1.1) (2026-09-10)


### Documentation

* add a square listing icon for the plugin directories ([#34](https://github.com/suneel944/agent-parley/issues/34)) ([84ed038](https://github.com/suneel944/agent-parley/commit/84ed0382f1cfbd928d60c924e5cd5719779a6637))

## [0.1.0] - 2026-09-10

### Features

* Run several native coding-agent CLIs side by side on one Linux user, each in its own Git worktree, each keeping its own vendor authentication
* Issue ownership with atomic claims, explicit offer and accept handoffs, and recorded dependencies between issues; no timeout and no process exit moves an issue
* Advisory file reservations that report the blocking owner and the reason
* Bounded peer messaging with idempotent sends, paged bodies and explicit acknowledgement
* Native lifecycle hooks that deliver coordination state into a running session, refuse a branch switch inside an assigned lane, and record every enforcement decision
* `agent-parley status` and a live `agent-parley top` dashboard over that recorded history
* A plugin bundle carrying the shared `coordinate` skill for Claude Code and Codex

### Install

```sh
uv tool install agent-parley
```

### Verify

```sh
sha256sum --check SHA256SUMS
```
