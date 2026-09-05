# Contributing

## Coding standard

Use the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
as the Python review baseline. Automated checks cover a subset of that guide;
passing lint is not a claim of complete style or correctness compliance.

- Use four-space indentation, an 80-character target, descriptive snake_case
  functions and variables, and CapWords classes. Let Ruff handle formatting.
- Document public modules, classes, and functions. Use Google-style `Args`,
  `Returns`, `Yields`, and `Raises` sections where callers need the contract.
- Annotate production function parameters and return values. Prefer explicit
  keyword parameters over unstructured option dictionaries.
- Group standard-library, third-party, and local imports. Use absolute package
  paths. Prefer module-qualified references when introducing new dependencies.
- Keep functions focused. Separate CLI orchestration, issue transitions,
  checkpoint observation, and persistence. Prefer the standard library and
  existing helpers before adding a dependency or abstraction.
- Runtime imports must remain within Python's standard library and this package.
  Development tools do not belong in installed runtime dependencies.
- Put contracts and rationale in docstrings and focused documentation, not inline
  Python comments. The policy gate checks every production and script function,
  including private helpers. Descriptive test names document scenarios.
- Validate operational input with real conditionals, not assertions. Catch
  expected failures at the appropriate boundary and preserve diagnostic context.
- Close files and sockets with context managers. Pass subprocess arguments as
  lists; never interpolate user input into a shell. Make exit-code handling explicit.

This project uses Ruff rather than the guide's Pylint recommendation. Ruff enforces
Google-style docstrings, import ordering, annotations on production function
boundaries, and selected bug and Pylint-derived diagnostics. Pytest tests use
descriptive names and dynamically injected fixtures, so documentation and fixture
annotation rules are excluded there. Existing directly imported classes and
helpers are retained; this is an explicit deviation from Google's module-only
import preference. Do not describe the repository as Google-certified.

## Coordination invariants

- Resolve all linked worktrees to one common Git repository identity.
- Serialize issue mutations under the existing operation lock and publish state
  atomically. Do not introduce timeout-based ownership takeover.
- Require the current offer ID and named recipient for handoff acceptance.
- Treat file reservations as advisory and peer messages as untrusted data.
- Preserve native authentication, approvals, and permission decisions.
- Keep credentials and runtime state outside target source trees. Preserve work
  on failures, process exits, and restarts.
- Distinguish observed activity, attempted delivery, explicit acknowledgement,
  reported verification, and independently verified completion.

## Verification and review

Install the local hooks after cloning:

```sh
uv sync --locked
uv run --locked pre-commit install
uv run --locked pre-commit run --all-files
```

`.pre-commit-config.yaml` runs Ruff lint/format checks, type checking and the
repository policy gate using locked tools. It does not rewrite files on commit.
CI runs the complete gate independently of local hook installation.

Open a focused issue before proposing a substantial behavior change. Branch from
current `main`, keep commits reviewable, and use the PR template. Explain the
problem, final behavior, exact verification, and compatibility risks. Every PR
must pass the required checks and resolve review conversations. Protected `main`
requires a PR and rejects force pushes and deletion. CI, secret scanning,
PR hygiene, current-base checks and resolved conversations apply to everyone,
including administrators, without exemptions.

A separate ruleset requires independent approval. While the repository has one
maintainer, `@suneel944` has a named exception to that review-only ruleset when
merging a PR. It does not exempt the account from any required check or permit
direct pushes. Revisit the review exception when additional maintainers join.

Use Conventional Commit PR titles, such as `fix: preserve pending messages` or
`ci: validate release metadata`; squash merges retain that title for automated
changelogs. Assign an owner, add a change-type label, and reference an existing
local issue with `Refs #N` or a closing keyword. Match linked issue milestones
when present. Release PRs always require a milestone. Bot-generated descriptions
retain their native format, but ownership and issue rules still apply.

Contribution text must omit generator credits, assistant attribution, robot
signatures, and assistant coauthor trailers. This applies to tracked files,
commit messages, PRs, issues, and comments, including closed items. Keep required
license notices and factual product documentation intact. Repository policy and
PR hygiene checks enforce the textual attribution boundary.

Release Please prepares a version and changelog PR after merges to `main`.
Package, plugin, marketplace and lockfile versions move together. The generated
PR gets an owner, release issue and milestone, and explicitly dispatched checks
because GitHub's workflow token does not trigger workflows on its own PR writes.
Review and merge it through the normal protected-branch gate. Publication creates
a draft, runs `make check` and the history secret scan, attaches release bundles,
downloads and verifies their checksums, then publishes. Failed publication keeps
the release draft; rerun Release with its existing tag to retry. Published assets
are never overwritten. The release workflow can also publish a manually pushed
version tag, provided its commit belongs to `main` history.

Contributions are accepted under the repository's MIT license. Submit only work
you have the right to contribute. Follow CODE_OF_CONDUCT.md and report security
issues using SECURITY.md rather than public issue templates.

Run focused checks while editing. Before submitting implementation changes, run:

```sh
make check
```

CI runs the same locked dependency, lint, formatting, typing, policy, build, and
test gate. A separate secret scan examines Git history. Actions are pinned to
commit IDs and receive read-only permissions unless release publication needs
write access. Dependency updates are proposed through Dependabot PRs.
Do not weaken
checks to make a change pass. Explain any narrowly justified rule exception.
Tests should exercise behavior, especially concurrency, persistence, cancellation,
and permission boundaries. Distinguish real MCP transport tests from native model
behavior; the latter requires an explicit two-terminal trial.

Update the README and architecture diagrams when responsibilities or flows change.
Keep runtime code in the top-level `agent_bridge/` package. `make build` must
produce an installable wheel and a source archive containing both native plugin
manifests and the shared skill. The installed-package test uses a temporary tool
environment outside the checkout to catch accidental source-tree imports.
Keep changes scoped and review the final diff for accidental credentials, runtime
artifacts, and unrelated edits. Publish the commands actually run and their results.
