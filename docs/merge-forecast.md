# Merge forecast

Two lanes that edit the same lines find out when the second pull request
fails to merge. The merge forecast finds out while both are still working,
and `agent-parley merge plan` recommends the order to land them in. Both are
advisory: the coordinator never merges, rebases, pauses or claims anything.

## What the service reads

Each supervision cycle runs one `merge forecast` stage in the service's
poll, timed beside the other stages in the poll record. It reads:

1. The head of the base checkout, which is the branch every lane merges back
   into, and every branch head in one `git for-each-ref` call. A branch
   that exists only as `origin/<branch>` is read from there.
2. For each lane that is not retired and whose branch exists, the fork point
   (`git merge-base`) and the paths it changed since then (`git diff
   --name-only`). Both are reused unchanged while neither the lane's head
   nor the base moved.
3. For each pair of lanes whose changed paths share at least one path, the
   result of `git merge-tree --write-tree` on the two heads. A pair whose
   paths are disjoint cannot conflict line by line and is never merged. A
   pair's result is reused while neither head moved, and at most 64 new
   merges run per cycle; a pair over that bound is tested on a later cycle.
4. For each conflicted file, the merged blob with its conflict markers, from
   which the line range of every conflicting hunk is read, at most eight
   files and eight ranges per pair.

`git merge-tree --write-tree` needs no checkout. It writes unreachable tree
and blob objects into the object store, which `git gc` removes in time, and
moves no reference, index or worktree. Uncommitted edits in a lane's
worktree are not part of its branch and are not forecast; a lane's
reservations and the base checkout's own edits are covered by the
reservation and base-checkout warnings instead. Git older than 2.38 has no
`--write-tree`; the forecast then predicts no conflict and `merge plan`
refuses.

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

The plan reads Git objects only. It claims nothing, merges nothing and moves
no reference, index or worktree. It refuses when Git cannot read the base
checkout or test a merge.

## Cost

A cycle in which no head moved costs two Git calls: `rev-parse` of the base
and one `for-each-ref`. A cycle after heads move costs two calls per moved
lane, one `merge-tree` per overlapping pair with a moved head, and one
`cat-file` per conflicted file. Measured on a project with eight lanes all
editing one ten-line file, seven adjacent pairs conflicting: the first
cycle ran 53 Git calls (28 merges) in 0.12-0.15 seconds, and the next cycle
with nothing moved ran 2 calls in 0.003-0.004 seconds.
