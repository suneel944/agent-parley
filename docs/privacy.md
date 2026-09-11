# Privacy policy

Agent Parley is a command line tool and plugin published by Suneel Kaushik S.
It coordinates several coding agents working on one Git repository. This
policy describes what the software stores, where it stores it, and what it
transmits. It applies to the published package, to the Claude plugin and to
the Codex plugin, which share one coordination runtime.

Last updated 2026-09-11.

## What the software transmits

Coordination itself never leaves your machine.

The coordination server binds the loopback interface only, at
`127.0.0.1` on a port you control. The command line tool and the plugin skill
reach that server over `http://127.0.0.1:PORT` with proxy handling disabled,
so requests are not forwarded to an external host. The runtime declares no
third-party dependencies and uses only the Python standard library.

A few commands deliberately reach the Git host you already use, through the
native `gh` and `git` clients under your own sign-in. `participant pr` pushes
that lane's branch to `origin` and opens a pull request carrying the lane's
recorded report. Claiming an issue looks up its title and assigns it to your
account, releasing it unassigns it, and a lane's first ready report posts its
summary and evidence as a comment on the issues it claims. Agent Parley stores
no forge token and adds no flag that bypasses a repository rule, and when no
`gh` client, no GitHub remote or no network is available nothing is sent and
the local record is unchanged.

There is no analytics, no crash reporting, no license check and no account.
The publisher receives no data from your use of the software.

The coding agents you run alongside Agent Parley are separate products with
their own policies. Agent Parley does not change what those agents send to
their own providers, and it does not read or forward their credentials.

## What the software stores

Coordination state is kept outside your repositories, in a private state
directory created with mode `0700`. The default location is
`~/.local/state/agent-parley`, and `AGENT_PARLEY_HOME` overrides it. The tool
refuses to run if that directory is not private.

The directory holds the coordination record you create by using the tool:

- Participant names and the repositories and worktrees they were launched in.
- Filesystem paths to those repositories.
- Issue numbers, who claims them, and the offers exchanged when work is
  handed over, including the summary and evidence text written into an offer.
- Advisory file reservations, which are path names.
- Messages participants send each other through the tool.
- An append-only event log of coordination activity with timestamps.

Content is whatever you or your agents write into a summary, a message or a
report. Do not put secrets in those fields; they are stored as plain text in
files you own.

`agent-parley top` additionally reads the session records your native clients
already write under their own configuration directories, so it can report what
each lane's client counted for its session. Those files are read locally and in
place. Nothing from them is copied into coordination state, and no vendor is
asked for anything.

## Retention and deletion

The event log is pruned automatically. Entries older than fourteen days are
removed when a session starts or ends.

Everything else persists until you delete it. To remove all coordination
state, stop the server and delete the state directory:

```sh
agent-parley down
rm -rf ~/.local/state/agent-parley
```

Deleting the directory does not touch your repositories, your worktrees or
your commits.

## Children

The software is a developer tool and is not directed at children.

## Changes

Changes to this policy are published in this file, in the public repository,
with the date above updated.

## Contact

Report a privacy question or a security concern to suneel944@gmail.com, or
open an issue at https://github.com/suneel944/agent-parley/issues. Security
reporting instructions are in `SECURITY.md`.
