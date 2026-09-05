# Changelog

## [0.3.2](https://github.com/suneel944/agent-bridge/compare/v0.3.1...v0.3.2) (2026-09-05)


### Automation

* enforce contribution text policy and restore logo delivery ([#14](https://github.com/suneel944/agent-bridge/issues/14)) ([843c953](https://github.com/suneel944/agent-bridge/commit/843c953f0f67b206321ffa709ee7ed1519c4b2e6))

## [0.3.1](https://github.com/suneel944/agent-bridge/compare/v0.3.0...v0.3.1) (2026-09-05)

### Changed

- Enforce PR ownership, change labels, issue references, milestones and titles.
- Prepare version and changelog PRs automatically; publish verified artifacts
  after their merge, with retry support for interrupted draft publication.
- Display the PNG logo in the README.
- Update development dependencies and pinned GitHub Actions to current stable
  releases; adapt the independent integration client to MCP 2 and HTTPX 2.
- Allow SQLite writers one second for transient contention while retaining the
  bounded failure deadline for locks that remain held.

### Automation

* enforce PR hygiene, automate releases and refresh dependencies ([#9](https://github.com/suneel944/agent-bridge/issues/9)) ([5566366](https://github.com/suneel944/agent-bridge/commit/55663669789e9fff75b8e3f2559e05990c301738))

## [0.3.0] - 2026-09-05

### Changed

- Replaced the mail backend with an in-house SQLite WAL engine and six scoped
  MCP tools. The installed package has no third-party runtime dependencies.
- Bound identity to the MCP connection, removing credentials and repeated
  project/agent arguments from model context.
- Made conflicting reservation batches atomic: conflicts grant no paths.
- Added idempotent sends, bounded inbox/body pagination, and explicit byte limits.
- Capped checkpoint notices at 1,536 UTF-8 bytes and suppressed unchanged notices.
- Added injected-byte counters without claiming tokenizer or billing savings.
- Added read-only legacy migration into a separate database and Linux pidfd
  process identity checks. Stop 0.2 services and sessions before upgrading.
- Shortened the README and separated architecture and operational documentation.

### Added

- Concurrency, authentication, migration, retry and context-budget tests using
  an independent MCP client and temporary local state.
- Static type and documentation-policy gates, secret scanning, pinned CI actions,
  contributor and security policies, issue/PR templates, and the MIT license.
- A repeatable local benchmark for storage latency and protocol size.

### Compatibility

- Linux with pidfd support is required; macOS is not supported by this version.
- Native sessions must be relaunched after upgrading. No live session is replaced
  automatically. Existing mail, worktrees and the original database are preserved.
- Public directory submissions remain separate from repository releases.

## [0.2.0] - 2026-09-05

First published release of Agent Bridge.

### Added

- Claude Code and Codex launchers with separate persistent Git worktrees and a
  shared repository identity.
- Authenticated local messaging, acknowledgements, and advisory file reservations.
- Atomic issue claims across worktrees, explicit handoff acceptance and decline,
  cancellation, stale-offer rejection, and persistent ownership after restarts.
- Native lifecycle checkpoints for coordination notices and observed activity.
- Status and progress reports that distinguish activity, ownership, verification
  evidence, pending mail, and unfinished work.
- Claude Code and Codex plugins sharing the `coordinate` skill.
- Python wheel and source packages, user and administrator installation targets,
  and `python -m agent_bridge` support.
- Google-style documentation and configured style checks, plus integration tests
  for real MCP transport, concurrent claims, process exits, and installed packages.
- Tagged release automation with plugin bundles, locked runtime requirements,
  SHA-256 checksums, and verification of downloaded assets before publication.

### Changed

- Moved runtime code to the top-level `agent_bridge/` package.
- Made `make install` install a package snapshot; editable installation is now
  explicitly available through `make install-dev`.

### Requirements and limitations

- Python 3.12+, Git, uv, and separately installed, authenticated native clients.
  Installation may download dependencies; the wheel is not a standalone binary.
- Linux integration is tested. macOS support has not been validated in this release.
- The plugins provide a coordination skill; the launcher configures worktrees,
  MCP, and lifecycle hooks. Start new sessions to load installed plugin skills.
- Reservations are advisory. Hooks do not wake already-idle sessions. Reports are
  agent-provided evidence, not independent proof of completion.
- All-user installation requires administrator privileges. No automatic merging,
  pushing, issue closure, or timeout-based ownership takeover is performed.
- The test suite reports upstream transport deprecation warnings. Live model
  compliance requires a separate two-terminal trial.

[0.2.0]: https://github.com/suneel944/agent-bridge/releases/tag/v0.2.0
[0.3.0]: https://github.com/suneel944/agent-bridge/releases/tag/v0.3.0
