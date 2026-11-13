# Merge forecast

Two lanes that edit the same lines find out when the second pull request
fails to merge. The merge forecast finds out while both are still working,
and `agent-parley merge plan` recommends the order to land them in. Both are
advisory: the coordinator never merges, rebases, pauses or claims anything.

## What the service reads

Each supervision cycle runs one `merge forecast` stage in the service's
poll, timed beside the other stages in the poll record. It reads:

1. The head of the base checkout, which is the branch every lane merges back
   into, and every branch head under `refs/heads/` and
   `refs/remotes/origin/` in one `git for-each-ref` call. A branch that
   exists only as `origin/<branch>` is read from there; a local branch of
   the same name wins.
2. For each lane that is not retired and whose branch exists, the fork point
   (`git merge-base`) and the paths it changed since then (`git diff
   --name-only`). Both are reused unchanged while neither the lane's head
   nor the base moved. When Git cannot answer, the lane keeps its previous
   reading for that cycle, or is left out when it has none; the failure is
   never cached, so the next cycle reads it again.
3. For each pair of lanes whose changed paths share at least one path, the
   result of `git merge-tree --write-tree` on the two heads. A pair whose
   paths are disjoint cannot conflict line by line and is never merged. A
   pair's result is reused while neither head moved.
4. For each conflicted file, the merged blob with its conflict markers, from
   which the line range of every conflicting hunk is read, at most eight
   files and eight ranges per pair.

New merges in steps 3 and 4 spend at most 64 Git calls per cycle, counting
each `merge-tree` and each `cat-file`; a merge starts only while its worst
case, one merge and eight files, still fits. A pair over that bound is
tested on a later cycle and keeps the conflict an earlier cycle predicted
for it, so its lanes are not mailed again. The first merge Git cannot
answer ends the cycle's merging, and that pair and every pair after it are
recorded as failed: they are not retried until either head or the base
moves, so a Git older than 2.38, which has no `--write-tree`, costs one
failed call per change rather than one per pair per cycle. The forecast
then predicts no new conflict and `merge plan` refuses.

`git merge-tree --write-tree` needs no checkout and moves no reference,
index or worktree. The tree and blob objects it writes, and the scratch
commits `merge plan` writes, go to a temporary object store under
`merge-forecast-objects/` in the project state directory: Git runs with
`GIT_OBJECT_DIRECTORY` pointing there and the repository's own object
store, from `git rev-parse --git-common-dir`, as
`GIT_ALTERNATE_OBJECT_DIRECTORIES`. The temporary store is removed when the
cycle or plan ends, so nothing is written into the target repository.
Uncommitted edits in a lane's worktree are not part of its branch and are
not forecast; a lane's reservations and the base checkout's own edits are
covered by the reservation and base-checkout warnings instead.

The reading is published as `merge-forecast.json` in the project state
directory with the wall seconds the stage took.

## What lanes and the operator see

`status` prints, under each lane in a predicted conflict:

```text
    merge with codex predicted to conflict in src/api.py lines 12-18,40-44; nothing was merged
```

`top` shows the same line as a `!` note under the lane's row. The line
ranges number the lines of the merged file with its conflict markers, as a
person resolving the conflict would see it.

Each lane of a newly predicted pair is mailed once, subject `Merge conflict
predicted with <peer>`, naming both branches, the files and ranges, and
`agent-parley merge plan`. The notice is keyed by the time the pair was
first predicted, so a later cycle, a retried cycle or a head that moves
while the pair still conflicts never repeats it. A pair that stops
conflicting and later conflicts again is new and is mailed again. With
supervision prompts disabled the forecast is still published and nobody is
mailed.

## `merge plan`

```sh
agent-parley merge plan [--repo PATH] [--json]
```

Every lane with committed work beyond the default branch, and every open
pull request in the service's cached pull request record whose head branch
is available locally, is a candidate. A pull request whose head branch is a
lane's branch is that lane's candidate. Pull requests whose head branch is
not available locally are listed as not planned.

Pairwise conflicts between candidates come first. The plan reuses the
published forecast's lane readings and merge results, and spends at most 64
Git calls on pairs it has no result for, as a cycle does; a pair over that
bound counts as not conflicting.

Landing is simulated against the default branch as it stands. At each
position the waiting candidates are tried in order of how many other
waiting candidates they conflict with, fewest first, then an open pull
request before a lane without one, then a lane reporting ready, then by
name. The first that merges cleanly onto the simulated branch takes the
position, and its merge becomes the simulated branch through an unreachable
scratch commit written with a fixed identity. Candidates that merge cleanly
onto nothing left are listed last with the files they would conflict in.

Deferring the candidate that conflicts with the most others keeps the count
low: if lane A conflicts with both B and C while B and C merge cleanly,
landing B then C leaves one conflict, A's, where landing A first leaves two.

Each position carries a reason, such as `merges cleanly onto main and
conflicts with nothing still waiting` or `predicted to conflict with main
after the positions above in src/api.py lines 12-18; rebase it after codex,
gemini land`. `--json` prints the plan as a snapshot document of kind
`merge_plan` with `branch`, `base`, `order`, `conflicts` and `skipped`.

The plan claims nothing, merges nothing, moves no reference, index or
worktree, and writes objects only into the temporary object store. It
refuses when Git cannot read the base checkout, name its object store or
test a merge.

## Cost

A cycle in which no head moved costs two Git calls: `rev-parse` of the base
and one `for-each-ref`. A cycle after heads move costs two calls per moved
lane, one `rev-parse --git-common-dir` when any merge runs, one
`merge-tree` per overlapping pair with a moved head, and one `cat-file` per
conflicted file. On a project with eight lanes all editing one ten-line
file, seven adjacent pairs conflicting, the test suite pins the first cycle
at 54 Git calls (2 `rev-parse`, 1 `for-each-ref`, 8 `merge-base`, 8 `diff`,
28 `merge-tree`, 7 `cat-file`) and the next cycle with nothing moved at 2.
