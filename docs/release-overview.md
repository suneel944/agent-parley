Agent Parley runs several native coding-agent CLIs side by side on one Linux
user. Each participant works in its own Git worktree, keeps its own vendor
authentication, and coordinates through a local service instead of a shared
directory convention.

What the coordination runtime provides:

- Issue ownership with atomic claims, explicit offer and accept handoffs, and
  recorded dependencies between issues.
- Advisory file reservations that report the blocking owner and its reason.
- Bounded peer messaging with idempotent sends, paged bodies, and explicit
  acknowledgement.
- Native lifecycle hooks that deliver coordination state into a running session
  and record every enforcement decision.
- `agent-parley status` and a live `agent-parley top` dashboard over that
  recorded history.

The runtime uses only the Python 3.12 standard library. Coordination state is
kept outside the target repository, and no vendor CLI is launched with an
authentication or permission bypass.

Install from PyPI with `uv tool install agent-parley`, or from the attached
wheel. `docs/operations.md` covers daily use, and `docs/architecture.md` covers
module boundaries and the reasoning behind them.

Each release attaches the wheel, the source distribution, the plugin bundle, a
pinned `requirements.txt`, the changelog and `SHA256SUMS`. Verify the download
with `sha256sum --check SHA256SUMS` before use.
