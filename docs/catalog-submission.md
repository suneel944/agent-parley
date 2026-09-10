# Catalog submission

This document prepares the public plugin directory submissions for the
Agent Parley plugin bundle. It records what each catalog asks for, what is
mechanically verifiable inside this repository and how to verify it, the
validator evidence captured for the current version, and the steps that only
the repository owner can carry out.

Repository installation does not imply public directory approval. Neither
catalog submission has been made. Nothing in this document submits a listing,
and continuous integration builds artifacts rather than filling review forms.
See the `## Plugins` section of `docs/operations.md` for the installation path
this listing would describe.

## What each catalog requires

### Claude plugin directory

Submissions go through the
[Console form](https://platform.claude.com/plugins/submit) or the
[claude.ai form](https://claude.ai/admin-settings/directory/submissions/plugins/new).
The directory surfaces in Claude Code as the `claude-plugins-official`
marketplace. [Anthropic's submission guide](https://claude.com/docs/plugins/submit)
is authoritative for the process, and
[Claude's plugin guide](https://code.claude.com/docs/en/plugins) for manifest
fields.

The form itself takes a GitHub link. The repository must be public, because
closed-source plugins are not accepted, and the manifest must pass
`claude plugin validate`. Identifying metadata belongs on the manifest: name,
display name, version, description, author, homepage, repository, license and
keywords. The repository must carry a license and a way to report problems.

Submitting is account-bound and role-gated. The Console form requires a
Developer, Admin or Owner role on a Console organization, which is the route
for an individual author. The claude.ai form requires a Team or Enterprise
organization with directory management access.

Once a listing is published, pushes to the repository are mirrored
automatically and rescreened. Updates do not need a new submission.

### Codex plugin submission

Codex submissions follow
[OpenAI's submission guide](https://developers.openai.com/plugins/deploy/submission)
and, for a plugin that already targets Claude Code,
[OpenAI's porting guide](https://developers.openai.com/plugins/guides/submit-claude-plugin).
Agent Parley is a skills-only plugin, so the submission consists of the skill
bundle rather than a hosted service. That path requires a verified publisher,
Apps Management write access, a listing URL, a policy URL, the skill bundle
itself, and review cases that a reviewer can execute against the bundle.

The upload is an archive, not a repository link, and the portal generates
`.codex-plugin/plugin.json` from `.claude-plugin/plugin.json`. The archive
keeps each skill directory with its `SKILL.md` and any scripts or assets it
references, and drops `.claude-plugin/marketplace.json`, `.agents/`, any MCP
configuration and any Claude-only settings. Skill text must be
provider-neutral: no instruction that depends on a Claude-specific feature,
and no assumption about which model reads it.

Both forms change over time. Re-read them at submission time and treat the
lists above as a preparation aid, not as a transcription of the current form
fields.

## Pre-submission checklist

Each item below is verifiable from a clean checkout. Run the command and
confirm the stated expectation before opening either form.

- Plugin manifest is valid and complete.
  `claude plugin validate plugins/agent-parley`
- Marketplace manifest is valid.
  `claude plugin validate .`
- Package, plugin and marketplace versions move together, no prohibited
  attribution text is present, and runtime dependencies remain empty.
  `uv run --locked python scripts/check_policy.py`
- The `coordinate` skill carries YAML frontmatter with `name` and
  `description`.
  `head -5 plugins/agent-parley/skills/coordinate/SKILL.md`
- The repository states its license.
  `head -1 LICENSE`
- The repository states a private vulnerability reporting route.
  `head -6 SECURITY.md`
- Package metadata names the author, license, description and project URLs.
  `sed -n '5,21p' pyproject.toml`
- The full repository gate passes, matching what continuous integration runs.
  `make check`
- The release bundle contains both native plugin manifests and the shared
  skill, so a reviewer downloading an artifact receives the whole plugin.
  `make build`

`make check` and `make build` write into the working directory, so run them on
a clean tree.

## Validation evidence

Captured on 2026-09-10 with Claude Code 2.1.267. The validator reads manifest
structure rather than the version string, so this evidence holds for any
release in which both manifests stay aligned with the package; `make check`
fails the build if they drift.

Plugin manifest:

```text
$ claude plugin validate plugins/agent-parley
Validating plugin manifest: /home/dev/agent-parley/plugins/agent-parley/.claude-plugin/plugin.json

✔ Validation passed
```

Marketplace manifest:

```text
$ claude plugin validate .
Validating marketplace manifest: /home/dev/agent-parley/.claude-plugin/marketplace.json

✔ Validation passed
```

Both commands exited zero with no warnings, and both still pass under
`--strict`, which turns unrecognized manifest fields into errors.

## Listing metadata

The manifests now carry the metadata a reviewer reads. Both plugin manifests
and the Claude marketplace entry describe isolated worktrees, advisory
reservations and explicit handoffs in a full paragraph. The Claude manifest
declares `displayName`, `license` and `keywords`; the marketplace entry adds
`category`, `author`, `homepage` and `repository`. The Codex manifest keeps
its listing copy in `interface`, and that copy names no specific model. The
marketplace is named `agent-parley`, so the documented install command is
`agent-parley@agent-parley`.

Both catalogs ask for public URLs that match the publisher identity. Use:

- Website and homepage: `https://github.com/suneel944/agent-parley#readme`
- Support: `https://github.com/suneel944/agent-parley/issues`
- Privacy policy:
  `https://github.com/suneel944/agent-parley/blob/main/docs/privacy.md`
- Terms: `https://github.com/suneel944/agent-parley/blob/main/docs/terms.md`

`docs/privacy.md` states that nothing leaves the machine. That claim rests on
the coordination server binding `127.0.0.1` only, on the client disabling
proxy handling for its loopback requests, and on the package declaring no
runtime dependencies. If any of those three change, the policy must change
with them in the same commit.

One gap remains, and it does not block the validator. There is no listing
imagery. `docs/assets/` holds the README banner; a directory listing asks for
an icon and may ask for screenshots sized to its own requirements.

The skill frontmatter itself is in good shape: `name` is `coordinate` and the
`description` is 211 characters covering both what the skill does and when to
use it.

## Steps only the repository owner can perform

The remaining work is distribution rather than engineering, and it is bound to
accounts and to a legal identity that an agent does not hold.

- **Publisher verification.** The Codex submission requires a verified
  publisher. Verification proves control of an identity or a domain and cannot
  be delegated.
- **Signing in and submitting.** Both forms are account-bound and role-gated.
  The Claude directory needs a Developer, Admin or Owner role on a Console
  organization, or a Team or Enterprise organization on claude.ai with
  directory management access; Codex needs Apps Management write access.
  Submitting requires the owner's authenticated session, and the submission
  becomes a statement made by that account.
- **Accepting catalog terms.** Each catalog attaches distribution terms to the
  listing. Only the owner can agree to them.
- **Publishing the policy and listing URLs.** The owner chooses which pages
  represent the plugin's policy and listing, and controls the hosting for
  them.
- **Responding to review.** Reviewer questions, requested changes and the
  final decision to publish or withdraw the listing all belong to the owner.

Everything a repository can do ahead of those steps is covered by the
checklist and the evidence above.

## Review cases

Each case is grounded in what
`plugins/agent-parley/skills/coordinate/SKILL.md` instructs. They are written
so a catalog reviewer can run them against a checkout with the Agent Parley
executable installed.

### 1. Status inspection from a session that is not launcher-managed

- **Setup.** Install the executable, install the plugin, then start an
  ordinary Claude Code or Codex session in a repository checkout without
  launching it through `agent-parley run`.
- **Action.** Invoke the `coordinate` skill and ask for the current
  coordination status.
- **Expected result.** The session runs `agent-parley status`,
  `agent-parley participant list` and `agent-parley issue list`, then explains
  that setup and native hooks require launching through Agent Parley in separate
  user terminals, one terminal per participant. It does not launch a nested
  interactive agent from a tool call and does not silently move the existing
  session into a worktree.

### 2. Claim before working on a numbered issue

- **Setup.** Launch two participants in separate terminals, for example
  `agent-parley run claude --repo /path/to/repository` and
  `agent-parley run codex --repo /path/to/repository`. Pick an issue number
  that no participant owns.
- **Action.** From one lane, ask the skill to start work on that issue.
- **Expected result.** The session runs `agent-parley issue claim NUMBER`
  before editing anything. `agent-parley issue list` in the other terminal
  then shows that participant as the owner. Ownership is reported separately
  from activity and from reported outcomes.

### 3. Refusing to take over another participant's claim

- **Setup.** The second lane from case 2, with the issue already owned by the
  first participant.
- **Action.** Ask the skill to work on the same issue number.
- **Expected result.** The session reports that another owner holds the claim
  and declines to proceed, offering instead to choose other authorized work or
  to negotiate a handoff. It does not act from the owner's lane by passing
  `--repo` or by changing directory, and it does not wait for a timeout to
  take ownership.

### 4. Explicit handoff with the current offer identifier

- **Setup.** The first participant owns the issue from case 2.
- **Action.** Ask the owner's session to hand the issue to the second
  participant, then ask the recipient's session to take it up.
- **Expected result.** The owner stops editing and runs
  `agent-parley issue offer NUMBER --to PARTICIPANT --summary "commit, checks,
  remaining"`, and stays paused while the offer is pending. The recipient
  reads the current offer identifier from `agent-parley issue list` and runs
  `agent-parley issue accept NUMBER --offer-id ID`. An identifier from a
  cancelled or replaced offer is rejected rather than accepted. The handoff
  transfers neither file reservations nor message acknowledgement.

### 5. Reporting an outcome without claiming verification

- **Setup.** Either participant owning an issue, mid-task.
- **Action.** Ask the session to record a partial outcome, then a ready
  outcome.
- **Expected result.** The partial report is sent as
  `agent-parley report --state partial --summary "result" --remaining "..."`
  and is refused without `--remaining`; the ready report requires
  `--evidence`. The session presents the report as an agent claim rather than
  as independent verification, and does not infer merge or push authority from
  issue ownership.

### Reservation behavior during any case

File reservations announced over MCP are advisory. A reviewer who reserves a
path should see peers stop making overlapping edits when a conflict is
reported, but nothing on disk is locked and no filesystem permission changes.
Issue claims do not reserve files.
