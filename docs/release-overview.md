Agent Parley runs several native coding-agent CLIs side by side on one Linux
user. Each participant works in its own Git worktree, keeps its own vendor
authentication, and coordinates through a local service. The coordination
runtime uses only the Python 3.12 standard library, keeps its state outside the
target repository, and launches no vendor CLI with an authentication or
permission bypass.
