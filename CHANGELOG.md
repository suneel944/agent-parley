# Changelog

## [0.1.0] - 2026-09-10

First release of Agent Parley.

### Added

- Run several native coding-agent CLIs side by side on one Linux user, each in
  its own Git worktree, each keeping its own vendor authentication.
- Issue ownership with atomic claims, explicit offer and accept handoffs, and
  recorded dependencies between issues. No timeout and no process exit moves an
  issue.
- Advisory file reservations that report the blocking owner and the reason.
- Bounded peer messaging with idempotent sends, paged bodies and explicit
  acknowledgement.
- Native lifecycle hooks that deliver coordination state into a running session,
  refuse a branch switch inside an assigned lane, and record every enforcement
  decision.
- `agent-parley status` and a live `agent-parley top` dashboard over that
  recorded history.
- A plugin bundle carrying the shared `coordinate` skill for Claude Code and
  Codex.

The coordination runtime uses only the Python 3.12 standard library.
Coordination state is kept outside the target repository, and no vendor CLI is
launched with an authentication or permission bypass.
