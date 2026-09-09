# Catalog submission

This document prepares the public plugin directory submissions for the
Agent Bridge plugin bundle. It records what each catalog asks for, what is
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

Community submissions go through the
[community submission form](https://platform.claude.com/plugins/submit). The
official catalog is curated separately; the plugin documentation at
[Claude's guide](https://code.claude.com/docs/en/plugins) is the authoritative
description of manifest fields and directory expectations.

In practice a submission needs a publicly reachable marketplace source, a
plugin manifest that passes `claude plugin validate`, and identifying metadata
on that manifest: name, version, description, author, homepage and repository.
The repository must carry a license and a way to report problems.

### Codex plugin submission

Codex submissions follow
[OpenAI's submission guide](https://developers.openai.com/plugins/deploy/submission).
Agent Bridge is a skills-only plugin, so the submission consists of the skill
bundle rather than a hosted service. That guide requires a verified publisher,
a listing URL, a policy URL, the skill bundle itself, and review cases that a
reviewer can execute against the bundle.

Both forms change over time. Re-read them at submission time and treat the
lists above as a preparation aid, not as a transcription of the current form
fields.

## Pre-submission checklist

Each item below is verifiable from a clean checkout. Run the command and
confirm the stated expectation before opening either form.

- Plugin manifest is valid and complete.
  `claude plugin validate plugins/agent-bridge`
- Marketplace manifest is valid.
  `claude plugin validate .`
- Package, plugin and marketplace versions move together, no prohibited
  attribution text is present, and runtime dependencies remain empty.
  `uv run --locked python scripts/check_policy.py`
- The `coordinate` skill carries YAML frontmatter with `name` and
  `description`.
  `head -5 plugins/agent-bridge/skills/coordinate/SKILL.md`
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

Captured on 2026-09-09 with Claude Code 2.1.266 at version 0.3.2 of the
plugin.

Plugin manifest:

```text
$ claude plugin validate plugins/agent-bridge
Validating plugin manifest: /home/suneel/Code/Personal/agent-bridge/plugins/agent-bridge/.claude-plugin/plugin.json

✔ Validation passed
```

Marketplace manifest:

```text
$ claude plugin validate .
Validating marketplace manifest: /home/suneel/Code/Personal/agent-bridge/.claude-plugin/marketplace.json

⚠ Found 1 warning:

  ❯ description: No marketplace description provided. Adding a description helps users understand what this marketplace offers

✔ Validation passed with warnings
```

Both commands exited zero. The marketplace warning is the only validator
finding and is addressed in the next section.

## Metadata gaps to close before submitting

None of these block the validator, and none are changed by this document.
They are listed so the owner can decide which to fix before a listing is
reviewed.

- Marketplace description is absent. `.claude-plugin/marketplace.json` has
  only `name`, `owner` and `plugins`, which is what the validator warns
  about. A directory listing reads better with a sentence describing the
  marketplace itself.
- Listing descriptions are short. The marketplace plugin entry is 57
  characters and the Claude plugin manifest description is 79. The Codex
  `interface.longDescription` is 96 characters. These are accurate but give a
  reviewer little to judge; a fuller paragraph explaining isolated worktrees,
  advisory reservations and explicit handoffs would serve the listing better.
- No keywords or category for Claude. `pyproject.toml` has no `keywords`
  field and the Claude plugin manifest has no category, so the plugin is hard
  to surface by search. The Codex manifest already sets `interface.category`
  to `Productivity`.
- No license field on either plugin manifest. The repository is MIT licensed
  through `pyproject.toml` and `LICENSE`, but a reader of the plugin manifest
  alone cannot see that.
- No policy URL. The Codex submission expects one. The repository currently
  offers `SECURITY.md` and `CODE_OF_CONDUCT.md`; the owner must decide which
  URL is presented as the policy link, or publish a page that covers data
  handling for the coordination runtime.
- No listing imagery. `docs/assets/` holds the README banner; a directory
  listing may ask for an icon or screenshots sized to its own requirements.

The skill frontmatter itself is in good shape: `name` is `coordinate` and the
`description` is 211 characters covering both what the skill does and when to
use it.

## Steps only the repository owner can perform

The remaining work is distribution rather than engineering, and it is bound to
accounts and to a legal identity that an agent does not hold.

- **Publisher verification.** The Codex submission requires a verified
  publisher. Verification proves control of an identity or a domain and cannot
  be delegated.
- **Signing in and submitting.** Both forms are account-bound. Submitting
  requires the owner's authenticated session, and the submission becomes a
  statement made by that account.
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
`plugins/agent-bridge/skills/coordinate/SKILL.md` instructs. They are written
so a catalog reviewer can run them against a checkout with the Agent Bridge
executable installed.

### 1. Status inspection from a session that is not bridge-managed

- **Setup.** Install the executable, install the plugin, then start an
  ordinary Claude Code or Codex session in a repository checkout without
  launching it through `agent-bridge run`.
- **Action.** Invoke the `coordinate` skill and ask for the current bridge
  status.
- **Expected result.** The session runs `agent-bridge status`,
  `agent-bridge participant list` and `agent-bridge issue list`, then explains
  that setup and native hooks require launching through the bridge in separate
  user terminals, one terminal per participant. It does not launch a nested
  interactive agent from a tool call and does not silently move the existing
  session into a worktree.

### 2. Claim before working on a numbered issue

- **Setup.** Launch two participants in separate terminals, for example
  `agent-bridge run claude --repo /path/to/repository` and
  `agent-bridge run codex --repo /path/to/repository`. Pick an issue number
  that no participant owns.
- **Action.** From one lane, ask the skill to start work on that issue.
- **Expected result.** The session runs `agent-bridge issue claim NUMBER`
  before editing anything. `agent-bridge issue list` in the other terminal
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
  `agent-bridge issue offer NUMBER --to PARTICIPANT --summary "commit, checks,
  remaining"`, and stays paused while the offer is pending. The recipient
  reads the current offer identifier from `agent-bridge issue list` and runs
  `agent-bridge issue accept NUMBER --offer-id ID`. An identifier from a
  cancelled or replaced offer is rejected rather than accepted. The handoff
  transfers neither file reservations nor message acknowledgement.

### 5. Reporting an outcome without claiming verification

- **Setup.** Either participant owning an issue, mid-task.
- **Action.** Ask the session to record a partial outcome, then a ready
  outcome.
- **Expected result.** The partial report is sent as
  `agent-bridge report --state partial --summary "result" --remaining "..."`
  and is refused without `--remaining`; the ready report requires
  `--evidence`. The session presents the report as an agent claim rather than
  as independent verification, and does not infer merge or push authority from
  issue ownership.

### Reservation behavior during any case

File reservations announced over MCP are advisory. A reviewer who reserves a
path should see peers stop making overlapping edits when a conflict is
reported, but nothing on disk is locked and no filesystem permission changes.
Issue claims do not reserve files.
