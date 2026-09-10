# Release operations

The approved version is **0.1.1**. There is no package release associated with
the workflow repair. Preparation and publication are separate manual actions;
neither a merge nor a tag push triggers them. Keep both workflows disabled until
the repair is merged and the unwanted 0.1.2 publication is addressed.

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
removes it and the workflow's approval/merge code. Preparation is now manual and
uses `skip-github-release`, so it cannot create release tags or GitHub releases.
A separate guard compares the approved tag with main's package code, plugin
files and `[project]` metadata before invoking Release Please. Workflow changes,
release scripts and development tooling alone cannot open another release PR,
even if an older commit used a `fix:` title. Other packaging-only releases need
an explicitly authored version PR when there is a real distribution change.

GitHub and PyPI were checked during recovery. Both retained valid 0.1.1 package
files with matching SHA-256 hashes. PyPI also contained an unwanted 0.1.2, and
GitHub showed 0.1.2 as latest. Recovery returned that release to a draft, preserving
its assets and tag, and restored 0.1.1 as GitHub's latest release. Restoring main
to 0.1.1 does not remove PyPI's records. Keep 0.1.1's files and tag intact and
yank 0.1.2 through the PyPI project's release management page. Yanking is
recoverable; deleting a PyPI file permanently consumes its filename. Existing
users pinned exactly to a yanked version can still install it.

## A future intentional release

1. Confirm there are actual package changes to distribute. Enable Prepare
   release when a new proposal is wanted, then dispatch it from `main`.
2. Review the changelog and version changes and merge the PR through required
   checks. A generated PR is a proposal, not release authorization. Previously
   consumed versions cannot be reused, including a deleted or yanked 0.1.2.
3. Create the corresponding version tag at the reviewed commit on `main`.
   Enable Release and dispatch it from `main` with that existing tag.
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
history checks, plus simulated GitHub/PyPI failures for retry and ordering
checks. These are not live publication tests. Read-only checks against the
existing 0.1.1 files verify its current hashes without uploading packages.
GitHub App permissions and PyPI trusted-publisher configuration must also allow
the respective workflows. A successful local gate cannot prove those external
permissions. Never publish a throwaway version just to test the workflow.
