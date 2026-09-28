# 6. Conservative edit anchors: formatters, whole-tree git operations, subagents

Status: accepted.

## Context

The anchor for a claim type is "the last event that changed a file matching this rule's
`relevant_files`." Three kinds of event are ambiguous about *which* files they touched, or
whether they touched any project file at all, and each is resolved toward treating more as an
edit rather than less — a claim wrongly marked unsupported costs the agent one extra command; a
claim wrongly marked supported ships a false "done."

## Decision

- **Formatters and fixers** (`ruff format`, `ruff check --fix`, `black`, `prettier --write`,
  `eslint --fix`, `cargo fmt`, `gofmt -w`, ...) count as edits to whatever path arguments they
  were given, or to the *whole project* when given none — `ruff format` with no arguments can
  rewrite any file the formatter's own config reaches, and `shell.py` cannot know that config
  without reading it.
- **Whole-tree git operations** (`git checkout <ref>`, `git switch`, `git restore`, `git pull`,
  `git merge`, `git rebase`, `git reset --hard`, `git stash` push/pop/apply, `git cherry-pick`,
  `git revert`, `git am`, `git clean`, `tar -x`, `unzip`) count as an edit to *every* file in the
  project. A `git checkout` can silently replace file content the transcript's own edit history
  said nothing about; treating it as a no-op anchor would let a stale test run from before the
  checkout still "support" a claim about code that just changed underneath it.
- **Subagent calls** count as an edit event by default (`subagent_calls_are_edits: true`), for
  every rule's `relevant_files`, when the subagent's own transcript is not being parsed for its
  edits (v0.1 does not parse edits made inside a subagent's own transcript — see the README's
  Limitations). The parent transcript only records that a subagent ran and what it reported back,
  not what it touched; the conservative reading is that it might have edited anything relevant.

## Consequences

- A false negative here (missing a real edit) would let a stale test run pass silently, which is
  the worse of the two failure directions for a tool whose whole point is catching stale
  claims — every ambiguous case above is resolved toward *more* anchors, not fewer.
- The corresponding cost: a command run right after a `git pull` that changed nothing relevant,
  or after a formatter run that only touched whitespace, is still treated as "before the anchor"
  and can produce an avoidable `stale` block. `subagent_calls_are_edits` and the whole-tree git
  list are both configurable per project for teams that find this too conservative for their
  workflow (`docs/config.md`).
- `exempt_if_only_edited` exists specifically to cut the false-positive cost for the common
  doc-only case: if every edit in the session matches those globs, the claim needs no evidence
  at all rather than demanding a test run for a README fix. Consistent with treating ambiguity
  conservatively, a whole-tree or subagent edit event never matches an exemption glob (there is
  no discrete path to check it against), so either one always forces the claim back onto normal
  evidence, even in a session that otherwise only touched documentation.
