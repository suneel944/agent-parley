# Changelog

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
