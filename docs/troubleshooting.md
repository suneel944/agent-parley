# Troubleshooting

First-run failures, each with the message Agent Parley prints, the cause in
one line and the command that fixes it. The messages are copied from the
source and a test keeps them in step with it. The reference pages hold the
detail: [Running lanes](lanes.md), [Monitoring](monitoring.md),
[Commands](commands.md) and
[Compatibility and `doctor`](operations.md#compatibility-and-doctor).

## Starting a lane

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Service:` reads `not running` in `agent-parley status` | The coordination service is a user process and does not survive a reboot or a `down`. | Run `agent-parley up`. `agent-parley run` starts it too. To bring it back after a reboot, see [Keeping the service across reboots](operations.md#keeping-the-service-across-reboots). |
| `Install and sign in to the native` CLI named for the lane's provider, then `first.` | The provider's command is not on your path. Agent Parley drives the vendor's own CLI and keeps its sign-in. | Install that CLI, sign in to it in your own terminal, and run the command again. [Providers](providers.md) names the CLI for each provider. |
| `Run this from an assigned agent worktree, not the main checkout` | The command acts as a lane and found no lane for the directory it ran in. The message names an active lane's worktree, or says the project has no active lane yet. | Run it from the named worktree, or start a lane with `agent-parley run claude`. `agent-parley participant list` shows the lanes and where each one works. |

## Reading `status`

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Forge:` line says `open issues unavailable` | `status` could not read the open issues, usually because the repository has no GitHub `origin` remote or `gh` is missing or signed out. The reason follows in parentheses. | Nothing is broken: coordination works without a forge, and claims on closed issues are only not hidden. To read issues, give the repository a GitHub `origin` remote, or install and sign in to `gh`. |

## Reading `doctor`

`agent-parley doctor` prints one row per component. A state that blocks
compatibility prints in capitals. The remedy for a mismatch is in the verdict
line under the rows. A setup gap or a matcher drift prints its remedy on a
line of its own, led by the component name.

| Symptom | Cause | Fix |
| --- | --- | --- |
| A row reads `MISMATCH` | A plugin speaks a protocol this build does not accept. | Run `agent-parley plugins install`, then restart any lane already running on the old plugin. |
| A row reads `STALE` | The installed plugin is not the launcher's version. | Run `agent-parley plugins install`. |
| The `approvals` row reads `setup gap` | A lane the service resumes unattended has no recorded bridge tool approval. | Run `agent-parley approval resume --bridge-tools on`, or resume the lane in your own terminal. |
| The `projects` row reads `ROOT GONE` | A project names a root checkout that no longer exists. | Restore the checkout, or remove the project with `agent-parley gc --project` and `--apply` once the service has retired its lanes. |
