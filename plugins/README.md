# Agent Bridge plugins

This bundle contains Claude Code and Codex manifests and the shared `coordinate`
skill. Install the Agent Bridge executable first. Given the wheel and locked
requirements from the same release:

```sh
uv tool install ./agent_bridge-0.3.0-py3-none-any.whl \
  --with-requirements ./requirements.txt
```

Extract the plugin archive and run these commands from its extracted root.

Claude Code:

```sh
claude plugin marketplace add .
claude plugin install agent-bridge@agent-bridge-local --scope user
```

Codex:

```sh
codex plugin marketplace add .
codex plugin add agent-bridge@agent-bridge-local
```

Install from only one marketplace per client. If already installed through a
personal marketplace, continue using that source or remove that installation
before switching. Keep the extracted directory available for local plugin updates.
Start a new session after installation. Use `/agent-bridge:coordinate` in Claude
Code or select the plugin's `coordinate` skill in Codex.

Launch working sessions through `agent-bridge run PARTICIPANT` in separate
terminals, one per participant; `agent-bridge run claude` and
`agent-bridge run codex` are the defaults. The plugin does not create hooks,
copy credentials, move an existing session into a worktree, or grant permissions.
