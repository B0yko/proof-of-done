# How it works

This page walks the Stop hook's own logic in the order it runs, at the level of detail needed
to predict what it will do with a given transcript. For the merged-config data model, see
`docs/config.md`; for the Claude Code JSONL shape it parses, see `docs/transcript-format.md`.

## 1. Claim detection (`claims.py`)

The claim detector runs on the agent's final message alone — it never looks at the transcript.
It is pure text analysis with the standard library's `re` module, nothing else.

1. **Mask.** Fenced code blocks, inline code, block-quote lines, quoted user text (straight or
   curly double quotes, on one line, up to 200 characters), and `**`/`__` emphasis markers are
   blanked out to same-length filler, so a claim inside a code block never matches and every
   later character offset still indexes the *original* message.
2. **Split into units.** The masked message splits into sentences (on `.`/`!`/`?` plus
   whitespace, not inside numbers or file names), Markdown bullet items, and table rows; each
   unit further splits into clauses on `;`, em/en dashes, `(...)` parentheticals, and a handful
   of conjunctions (`, but`, `, however`, `, although`, `, and`, ...). Markdown structure is not
   special-cased beyond this — a bullet, a table cell and a checkbox line (`✅ Tests`,
   `**Tests:** 42 passed`) are each just a unit like any sentence.
3. **Match per rule.** Every enabled rule's compiled `claims` patterns run against every clause.
4. **Reject on:**
   - **negation** — a negator (`not`, `n't`, `never`, `no`, `without`, `unable to`, `could not`,
     `did not`, `have not`, `cannot`, `nor`) within three tokens before the claim's predicate;
     `no`/`without` immediately followed by `errors`/`failures`/`warnings`/`issues`/`problems`
     *strengthens* the claim instead ("Build succeeds without errors" is still a claim);
   - **hedge or condition** — `should`, `would`, `might`, `may`, `could`, `expect`, `likely`,
     `probably`, `hopefully`, `once`, `if`, `unless`, `assuming`, `seems`, `appears`, `I think`,
     `I believe`, `after you`, `to confirm`, appearing before the predicate ends (or anywhere in
     the clause for `probably`/`likely`/`hopefully`/`I think`/`I believe`);
   - **future tense** — `will`, `'ll`, `going to`, `next step`, `about to`;
   - **a question** — the clause ends with `?`;
   - **an instruction to the user** — the clause opens with an imperative (`run`, `try`,
     `execute`, `check`, `verify`, `confirm`, `make sure`, `please`, `you can`, `you should`, ...);
   - **a non-finite predicate** — `to pass`, `make ... pass`, `get ... to pass`.

   `"not verified"`, `"untested"`, and `"I did not run X"` never produce a claim under this
   ruleset — negation plus a predicate that itself denies action.
5. Surviving matches become `Claim(rule_id, claim_type, quote, span)` objects, in message order.
   The quote is sliced from the *unmasked* original text.

**Prefilter.** Before any of the above, `claims.prefilter(message, keywords)` checks whether the
lowercased message contains *any* enabled rule's keyword. `keywords` is the union of every
enabled rule's own list (`tests`: test, spec, pass, green, suite; `lint`: lint, ruff, eslint,
flake8, clippy, pylint, biome, prettier, format, issue; and so on per rule — see
`docs/config.md`'s generated table for the full evidence-command side, and `defaults.yaml` for
the keyword lists themselves). A message with no keyword match short-circuits before the
transcript is even read.

Custom rules must declare their own non-empty `keywords`, and a test enforces that every
labelled claim in the templated and adversarial fixture sets actually contains one of its rule's
keywords — a held-out prefilter miss (a real claim phrased so the keyword list does not catch
it) is reported in the evaluation's failure analysis and is explicitly never "fixed" by loosening
the prefilter to match the held-out set.

Detection is **English-only**: hedge words, negators, instruction verbs and claim phrasings are
all English literals and patterns. A claim made in another language is silently not detected —
not blocked, not warned about, just invisible to this tool.

## 2. Evidence engine (`evidence.py`)

`build_events` walks a parsed session's steps once, in order, producing three kinds of event:
edits, foreground commands (with their parsed shell segments and results), and subagent calls.
This event list is built once per session and reused across every claim in every stop attempt
that session has — judging N claims never re-walks the transcript N times.

For each claim instance, and each enabled rule sharing its claim type:

1. **Anchor.** The last edit event before the stop that touches a path matching the rule's
   `relevant_files` and not its `ignore_files`. Edit sources: the edit tools (`Edit`, `Write`,
   `MultiEdit`, `NotebookEdit`); Bash writes whose target resolves (`sed -i`, `>`/`>>` to a file,
   `tee`, `mv`, `cp`, `rm`, `touch`, `ln`, `truncate`, `dd of=`, `rsync`, `install`, `patch`,
   `git apply`, `git mv`, `git rm`); formatters (`ruff format`, `black`, `prettier --write`,
   `cargo fmt`, `gofmt -w`, ...); whole-tree git/archive operations (`git checkout`, `git reset
   --hard`, `git stash pop`, `tar -x`, `unzip`, ...); and subagent tool calls, by default (see
   ADR 6 for why the last three are treated conservatively). No anchor means "session start."
2. **Exemption.** If the rule declares `exempt_if_only_edited` and every edit event in the
   session matches those globs, the claim is supported outright (reason `supported`, flagged
   `exempt`) — a whole-tree or subagent edit never matches, so it always falls through to normal
   evidence instead.
3. **Candidates.** Every foreground command segment strictly after the anchor (at or before the
   stop) whose normalized argv matches the rule's evidence prefixes (or, for `fixed`/`verified`,
   any other enabled rule's commands plus `execution_commands`), and is not disqualified.
4. **Verdict, from the *last* candidate** (see ADR 5 for why the last one, not the best one), in
   this reason precedence:

   | Reason | Meaning |
   |---|---|
   | `no_command` | No candidate exists anywhere in the session. |
   | `stale` | A candidate exists, but only before the anchor. |
   | `background_only` | The only candidates after the anchor ran in the background. |
   | `no_result` | The latest candidate has no result recorded (also the Stop-time flush-race case, see below). |
   | `failed_exit` | The latest candidate was interrupted, timed out, or exited non-zero. |
   | `empty_run` | Its output matched an `empty_output` pattern (ran, but tested nothing). |
   | `failed_output` | Its output matched a `fail_output` pattern despite a zero exit. |
   | `masked_inconclusive` | Its exit status was masked (piped without `pipefail`, `\|\|`, `; true`, `set +e`) and no `success_output` pattern matched. |
   | `superseded_by_failure` | The latest candidate is a *partial* run, but a full run after the anchor already failed. |
   | `supported` | None of the above — the latest candidate ran, succeeded, and its output does not read as a failure or an empty run. Flagged `partial` when the deciding candidate was itself a partial run. |

5. **Several rules, one claim type.** Rules whose `relevant_files` actually matched an edit this
   session are "responsible": if any rule is responsible, the claim is supported only if *every*
   responsible rule is; if none is responsible, any single rule's support is enough. The
   reported reason and the effective action (the strictest of `block`/`warn` among the
   determining rules) follow whichever rule decided the outcome, in rule order.

**Command matching**, in `shell.py`: `shlex`-tokenize each Bash call (heredoc bodies stripped
first, continuations joined), split into segments on `\n`, `&&`, `||`, `;`, `|`, unwrap `bash -c`
/ `sh -c` / subshells recursively, strip leading env assignments and wrappers (`cd X &&`, `env`,
`time`, `timeout`, `uv run`, `poetry run`, `npx`, `pnpm exec`, ...), then prefix-match the
normalized tokens against a rule's `evidence.commands`. A segment is **masked** when its own exit
status does not decide the command's overall status: piped into another command without
`pipefail`, followed by `||`/`;`/newline with more commands (unless `set -e` is active), or under
an explicit `set +e`. A command is **disqualified** as a candidate if it carries an
`exclude_args` token (`--collect-only`, `--dry-run`, `--watch`, ...), or if the same Bash call
defines a shell function or alias named like the evidence program, or prepends to `PATH` —
closing the most obvious ways to fake a result (`pytest(){ echo 5 passed; }`, `alias pytest=true`,
`PATH=./fake:$PATH pytest`). `read_only_commands` (`cat`, `grep`, `git status`, ...) never count
as evidence for any rule, however they might otherwise match a prefix.

**Background commands never count** as evidence in v0.1: `run_in_background: true`, a command
Claude Code moved to the background on timeout, and a later task-notification reporting the
command's real exit status are all classified as background and are never read as a successful
foreground run. Interrupted and timeout-killed runs are `failed_exit`.

**One status for several `&&`-joined commands.** A Bash tool result carries exactly one status
for the *whole* call, e.g. one status for `ruff check . && pytest -q`. If the call succeeded,
every executed segment succeeded. If it failed, only the *last* segment of the trailing bare
`&&` chain is known to own that status — an earlier `&&` segment's own status is genuinely
ambiguous: it may have run and failed, or never run at all because the chain already
short-circuited on something before it. Such a segment is judged the same way an already-
`masked` one is: accepted only when the output confirms its own `success_output` pattern,
`masked_inconclusive` otherwise (a `fail_output`/`empty_output` match still counts regardless,
since both read the shared output text rather than the ambiguous exit code). A trivial trailing
segment that could not itself explain a failure (`echo`, `true`, `:`, or a configured
`read_only_commands` entry) is skipped when finding the "last" segment, so `pytest -q && echo
ok` failing still attributes the failure to `pytest`, not to the `echo` that never got to run.
A `;`, `||`, `|` or backgrounding break in the chain stops this reasoning at that point — those
cases are already covered by the ordinary `masked` rule above.

## 3. Suggested command (`suggest.py`)

Tried in order, first hit wins:

1. The most recent command the agent itself ran in this session (any outcome, before the stop)
   that matches the deciding rule's evidence and is not disqualified — a non-partial match beats
   a partial one regardless of recency. Rendered as its display text with wrappers and env
   assignments kept, `cd` segments dropped, and pipes/redirections stripped (`cd app && uv run
   pytest -q 2>&1 | tail -5` renders as `uv run pytest -q`).
2. The rule's configured `suggest` string.
3. Project-ecosystem detection through an injectable file probe rooted at the project root, first
   hit wins: `pyproject.toml`/`pytest.ini` → `pytest` (`uv run pytest` if `uv.lock` exists); a
   `package.json` script → `npm test`/`npm run <script>` (or `pnpm`/`yarn`, by lockfile);
   `go.mod` → `go test ./...`; `Cargo.toml` → `cargo test`/`clippy`/`check`/`build`; a Makefile
   target → `make <target>`. `fixed`/`verified` reuse the `tests` detection; `deployed` has none.
4. Otherwise: `(no <type> command found — run it in the foreground)`.

## 4. Block message (`message.py`)

One numbered entry per unsupported claim, each with the quote (cut to 80 characters), the claim
type, a plain-language reason phrase naming step numbers and file/command names, and a `Run:`
line (command cut to 120 characters; paths cut to 80). The whole message is capped at 20 lines
and 1,800 characters — entries are dropped from the end and replaced with a single `…and N more
unsupported claims.` line when it would not fit. The configured skip token is scrubbed from the
rendered text everywhere it would otherwise appear (`[skip token]`), so an agent cannot see the
literal bypass string echoed back in a block reason. `warn` mode renders the identical body under
a `proof-of-done (warn):` header instead of `proof-of-done:`, as a `systemMessage` rather than a
blocking `reason`.

## 5. Loop safety and the tamper check (`hook.py`, `tamper.py`, `counters.py`)

- **Skip token.** `#skip-proof` (configurable) anywhere in the user's *latest real typed prompt*
  disables checks for that turn. Tool results, meta entries, hook feedback (including this
  hook's own prior block reason), task notifications, local-command output, compaction summaries,
  and the agent's own text never count — an agent cannot manufacture a skip by echoing the token
  back in its message.
- **Consecutive-block counter**, per `sha256(session_id:agent_id)`, in
  `<data dir>/counters/`: resets to 0 whenever the Stop payload's `stop_hook_active` is false;
  reaching `max_blocks_per_turn` (default 2, intentionally below Claude Code's own 8-block Stop
  cap) allows the stop and emits a warning naming the still-unsupported claims instead of
  blocking again; an allowed stop resets the counter. Files are written atomically (temp file +
  `os.replace`) and pruned after 7 days.
- **Tamper check**, run before `enabled`/`mode`/any rule `action` from a config file is applied:
  scans this session's own edit events for writes to `.proof-of-done.yaml`, the user config file,
  the `PROOF_OF_DONE_CONFIG` file, or a `.claude/settings*.json` file whose written text mentions
  `PROOF_OF_DONE` or `enabledPlugins`. For each tampered *config* file, only the downgrades it
  is responsible for are undone (`enabled: false`, `mode: warn`, a rule's `action` lowered
  toward `warn`/`off`, a raised `max_blocks_per_turn`) — anything else that file changed still
  applies. A tampered *settings* file additionally makes the hook ignore `PROOF_OF_DONE=off` and
  `PROOF_OF_DONE_MODE=warn` for the rest of the session. **Tampering alone never blocks a stop**;
  it only prevents a downgrade from taking effect, and a user-visible note names the edited file.
- **Stop-time flush race.** If the last tool call in the transcript has no result yet (the
  transcript file can lag the in-memory conversation), the hook waits 50 ms and re-reads once;
  if the result is still missing, a `no_result` verdict on exactly that call is downgraded to
  `warn` rather than blocking on what might just be a timing artifact.
- **Resumed/forked sessions.** A transcript whose first line's `parentUuid` points at an entry
  no line in the file defines is a resumed or forked session whose earlier history lives
  elsewhere; every block in that case downgrades to `warn`, since the evidence the hook can see
  is known to be incomplete.

## 6. Fail-open list

See ADR 4 for the complete list and the reasoning; in short, every unreadable transcript, corrupt
JSONL line, invalid config, oversized transcript, missing/too-old Python, or internal exception
results in exit 0 with at most a short `systemMessage` — never a hang, never a crash the user
sees, never a block caused by the tool's own failure.
