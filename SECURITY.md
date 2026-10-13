# Security policy

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/suneel944/agent-parley/security/advisories/new).
If private reporting is unavailable, email suneel944@gmail.com. Do not disclose
credentials, private messages or exploit details in a public issue.

Include the affected version, operating system, reproduction steps, expected
boundary, and observed impact. Share a minimal synthetic example whenever possible.
The maintainer will assess the report and coordinate disclosure; no response-time
or bounty commitment is offered. Security fixes target the latest released version.

## Trust boundaries

- The service is for one trusted OS user on one host. Worktrees are not sandboxes;
  a process with that user's filesystem access can read local credentials.
- HTTP is loopback-only and authenticated. Never expose it through a public proxy.
  The health credential cannot call tools; lane credentials are project-scoped.
- Notifications are off by default. When on, coordination details leave the
  host to the configured Telegram chat or SMTP recipients. With
  `AGENT_PARLEY_INBOUND=telegram`, the service long-polls the Telegram Bot API
  and accepts status queries and decision answers only from the configured
  chat, with a passcode of at least 12 characters held as a salted hash. Five
  wrong passcodes in ten minutes lock the path for an hour. A decision answer
  can press an option on a native prompt still on screen, and an option that
  widens a permission needs a confirming tap. Treat the bot token, chat and
  passcode as credentials. They are stored owner-only in `notify.json` in the
  state directory.
- Peer messages and handoff summaries are untrusted input, not instructions with
  authority over native permissions or repository rules.
- Reservations prevent conflicting grants among participants, and a lane's
  native hook refuses that lane's own write into a path a peer reserved
  exclusively; a shared reservation only produces a notice. They
  do not block writes from anything outside the lane's agent. Issue ownership is cooperative, not GitHub authorization.
- Back up private state before upgrades. Worktrees contain real user changes.
  Do not attach the state directory, identity files, or unredacted logs to reports.

See [Architecture](docs/architecture.md) for resource limits and tested boundaries.
