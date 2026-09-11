# Release operations

The approved version is **0.2.0**, published on 2026-09-11. Preparation and
publication remain separate. Every push to `main` measures release eligibility
and opens or updates a proposal when the delivered product work warrants one;
merging that proposal and publishing an existing tag stay manual actions, and
neither a merge nor a tag push publishes anything. Both workflows are active.

## Audited release history

The read-only audit on 2026-09-10 downloaded the GitHub bundles and PyPI package
files, verified both GitHub checksum manifests, and compared the package bytes.
It predates 0.2.0, so the table below records the versions that existed then and
says nothing about the current approved release.

| Version | Disposition | Evidence |
| --- | --- | --- |
| 0.1.0 | Preserve as archival; do not retry publication | Wheels match across services. Source archives differ in five files; PyPI's copies match the original tag. |
| 0.1.1 | Approved baseline and supported retry | Both package hashes match across GitHub and PyPI. Neither file is yanked. |
| 0.1.2 | Retired incident record; never reuse | Absent from PyPI at audit time. GitHub retains a draft, assets and the original tag. |
| Earlier version labels | Withdrawn preparation history | No corresponding release remains in the current GitHub or PyPI inventories. Reworded commits retain their original source trees. |

The initial source archive differs in `CHANGELOG.md`, `docs/operations.md`,
`docs/release-overview.md`, `scripts/release_artifacts.py`, and
`tests/test_release_artifacts.py`. The archive file lists are identical; package
runtime contents are unchanged. This is a provenance defect in the GitHub
archive, not a reason to overwrite either service's published files. The
original tag and PyPI archive remain the source provenance for that release.

| File | SHA-256 |
| --- | --- |
| Initial wheel, both services | `0a904c5a87983ba664724757fa8c8d3822bcb4a1744522010700008893843879` |
| Initial source archive, PyPI | `b75164c598a94ba70410e1b67af8655b717b1b14d3a402884afbb9608bc607bb` |
| Initial source archive, GitHub | `5e82c8f562c22f54ede4519b2928aa1bf78ec6057742d06796b4e837b23fe4c7` |
| Approved wheel, both services | `d6807a9cf203d402853e6e14c2bd5f97c7d0815b79ba241861bd39ec95a5b7a6` |
| Approved source archive, both services | `5b9a5e15bd4fd3c6765cdfd6d9d12f13932d47c5ab6e33670b217526ff02111a` |

## Migration to curated commit messages

Published tags keep their original commit IDs. Rewording their ancestors changes
the corresponding release commits in the curated branch even though all their
file trees remain identical.

| Tag | Original commit | Equivalent curated commit |
| --- | --- | --- |
| v0.1.0 | `1221ae6acbe4a0398fb5f3e51db4fc670395b0e1` | `d8aea19fa65ac5d70a60705bc44f9728bc113619` |
| v0.1.1 | `5a45a2d076531936a653e31873fd176d29b232e2` | `2c406150c9f559d5e975d88b645effbb449cf46d` |

Only the approved release has an operational mapping in
`.github/release-history.json`. Validation requires its exact original commit,
an identical full Git tree at the mapped commit, and mapped ancestry of the
workflow's main revision. Unmapped tags still require ordinary ancestry.
Publication continues to check out the original tag commit and reuse its
verified assets. The initial archival release is not enabled for retries.
The same history file records the retired version. Both the policy gate and
publication validation reject reusing it, even if a proposal changes every
version marker to that number.

A root `last-release-sha` in `release-please-config.json` limits Release
Please's commit scan to the mapped approved release. A bootstrap setting would
not work: Release Please still finds the original GitHub release, so it does
not enter bootstrap mode. The policy gate and the preparation command require
that boundary to equal the mapping recorded for the version in the manifest, and
to be absent when that version was never migrated. Retiring it is automated:
the `Retire the migration scan boundary on the proposal` step of `Prepare
release` runs `python3 -m scripts.release_publish boundary` against the open
proposal and commits the boundary that proposed version requires, so a proposal
never arrives carrying a stale one. The 0.2.0 proposal was the first to advance
past the migrated release, and the field is now absent from the configuration.
Keep the historical mapping as provenance. New tags follow normal ancestry and
do not require additional mappings.

Replacing protected main remains a separate repository-policy operation.
A squash merge can install the code but cannot remove the old commit history.
Do not move published tags, merge the old lineage into the curated branch, or
disable ancestry and artifact checks to make a cosmetic rewrite pass.

## Incident and recovery

After the 0.1.1 release, commit `436c2b5` described build tooling with a `fix:`
title. Release Please included that commit in a 0.1.2 proposal. PR #46 rolled
version markers back to 0.1.1, but did not remove that commit from unreleased
history. The next preparation run created PR #47 with the same change, approved
it with the workflow identity, enabled automatic merging, and published 0.1.2
again. Reverting metadata alone could not stop the automation.

The attempted `last-release-sha` workaround was inside `packages["."]`, although
Release Please supports that option only at the configuration root. A permanent
root override would also pin history scanning to an old boundary. The repair
removed it and the workflow's approval/merge code. The later history migration
uses a root-only boundary with an explicit mapping and a stale-boundary gate,
as described above. Preparation is now manual and uses `skip-github-release`,
so it cannot create release tags or GitHub releases.
A separate guard compares the approved tag with main's package code, plugin
files and `[project]` metadata before invoking Release Please. Workflow changes,
release scripts and development tooling alone cannot open another release PR,
even if an older commit used a `fix:` title. Other packaging-only releases need
an explicitly authored version PR when there is a real distribution change.

GitHub and PyPI were checked during recovery. Both retained valid 0.1.1 package
files with matching SHA-256 hashes. PyPI also contained an unwanted 0.1.2, and
GitHub showed 0.1.2 as latest. Recovery returned that release to a draft, preserving
its assets and tag, and restored 0.1.1 as GitHub's latest release. Restoring main
to 0.1.1 does not remove PyPI's records. Keep 0.1.1's files and tag intact. The
unwanted version was absent from PyPI at the later audit. Do not recreate
it: deleting a PyPI file permanently consumes its filename. If an unwanted
publication happens again, prefer recoverable yanking over deletion. Existing
users pinned exactly to a yanked version can still install it.

## A future intentional release

1. Confirm there is enough delivered product work to distribute. Eligibility is
   measured, not judged. The candidate step counts commits between the approved
   release and `main`, and a commit counts only when both of these hold: its
   title uses a releasing conventional type, `feat`, `fix` or `perf`, with or
   without a scope or `!`; and it touches at least one path under
   `agent_parley/` or `plugins/agent-parley/`. Workflow, script, test and
   documentation commits are therefore structurally incapable of counting, even
   with a releasing title, because they change nothing the package distributes.
   Each counted commit contributes the distinct issues its message names
   through `Refs`, `Fixes`, `Closes` or `Resolves`, so one issue delivered by
   three pull requests counts once; a counted commit that names no issue counts
   as one unit of its own. Fifty product features propose the next major
   version, ten product issues propose the next minor version, and a single
   commit titled `fix(urgent):` proposes the next patch version. That scope is
   the only mechanical marker for a patch release; nothing else reaches that
   rung. Below all three thresholds nothing is proposed and the step reports
   the counts it measured. Counting is local and repeatable: it reads Git
   history and nothing else, so `python3 -m scripts.release_publish candidate`
   answers the same question before any merge. Every push to `main` runs this
   step, so a warranted version proposes itself; dispatching Prepare release
   from `main` runs the same measurement on demand and reaches the same
   conclusion.
2. Review the changelog and version changes and merge the PR through required
   checks. A generated PR is a proposal, not release authorization. Previously
   consumed versions cannot be reused, including a deleted or yanked 0.1.2. A
   proposal that lands on an unavailable version advances to the next free
   version of the same kind instead of failing, so an urgent patch from 0.1.1
   proposes 0.1.3 without a manual correction. A version is unavailable when it
   is retired in `.github/release-history.json`, when the tag already exists
   locally or on `origin`, when a GitHub release exists for that tag including
   a draft, or when the package index already has it. Only a definite absence
   makes a version available; a check that fails to answer stops the run rather
   than proposing a number that may already be taken. The migration's root
   `last-release-sha` is retired without asking: preparation rewrites it on the
   proposal to the boundary that version requires, or removes it when that
   version was never migrated, so the policy gate never meets a stale one.
3. Create the corresponding version tag at the reviewed commit on `main`.
   Dispatch Release from `main` with that existing tag.
4. Check the workflow result and verify the published version. A release is
   complete only after the wheel and source archive on PyPI match the verified
   GitHub artifacts.

## Retry contract

The GitHub bundle is the canonical artifact set. It contains exactly the wheel,
source archive, plugin ZIP, requirements, changelog, release notes and SHA256SUMS.
The checksum manifest is uploaded last, and its digest is passed between jobs.
Paths, duplicate entries, extra files and hash mismatches stop publication.
An existing complete bundle is reused byte for byte, even when a subsequent
build would differ. Nothing uses `--clobber` or PyPI's `skip-existing` option.

For an interrupted first GitHub upload, rerunning may build the missing files.
Every existing asset must match before another upload occurs. A conflicting
partial draft requires investigation; automation will not overwrite it. For an
interrupted PyPI upload, matching existing files are retained and only missing
files are staged. HTTP errors other than 404 and unexpected API responses fail
closed. A deleted PyPI filename cannot be recovered by retrying.

PyPI and GitHub are separate services, so publication cannot be one atomic
transaction. If PyPI succeeds and the GitHub publication step fails, rerun the
same workflow: PyPI files are checked, no duplicate upload is attempted, and the
GitHub publication can finish. PyPI metadata propagation or service outages may
require a later retry. No error creates a new version automatically.

## Verification limits

`make check` includes real temporary Git repositories for tag resolution and
history and migration checks, plus simulated GitHub/PyPI failures for retry
and ordering checks. These are not live publication tests. Read-only checks
against the
existing 0.1.1 files verify its current hashes without uploading packages.
GitHub App permissions and PyPI trusted-publisher configuration must also allow
the respective workflows. A successful local gate cannot prove those external
permissions. Never publish a throwaway version just to test the workflow.

The migration audit also ran Release Please 17.6.0, the version locked by the
pinned action, against the published curated branch using read-only GitHub
requests and the proposed scan boundary. It stopped at the mapped release and
built zero release proposals. This exercises history discovery without opening
a PR, tagging a commit, dispatching workflows, or uploading packages.
