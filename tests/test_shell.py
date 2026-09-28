"""Tests for `proof_of_done.shell`: segmentation, wrapper stripping, masking, matching and
bash edit extraction. Table-driven throughout; see docs/how-it-works.md for the behaviour
these encode."""

from __future__ import annotations

import pytest

from proof_of_done.shell import (
    EditTarget,
    ParseError,
    Redirect,
    Segment,
    bash_edits,
    disqualified,
    is_excluded,
    is_partial,
    is_read_only,
    match_prefix,
    matches_any,
    parse_command,
)


def segs(cmd: str, cwd: str | None = None) -> list[Segment]:
    return parse_command(cmd, cwd).segments


def programs(cmd: str, cwd: str | None = None) -> list[str]:
    return [s.program for s in segs(cmd, cwd)]


# ------------------------------------------------------------------------------------------
# Pre-pass: line continuations, heredocs, parse errors
# ------------------------------------------------------------------------------------------


def test_backslash_newline_continuation_joins_command() -> None:
    p = parse_command("pytest \\\n  -q")
    assert len(p.segments) == 1
    assert p.segments[0].argv == ["pytest", "-q"]


def test_backslash_newline_inside_single_quotes_is_literal() -> None:
    # Inside single quotes a backslash has no special meaning, so this is one multi-line word.
    p = parse_command("echo 'a\\\nb'")
    assert p.segments[0].argv == ["echo", "a\\\nb"]


def test_heredoc_body_stripped_operator_kept() -> None:
    cmd = "cat <<EOF\nhello\nEOF\n"
    p = parse_command(cmd)
    assert len(p.segments) == 1
    assert p.segments[0].program == "cat"


def test_heredoc_with_apostrophes_in_body() -> None:
    cmd = "cat <<'EOF'\nit's a test, don't worry\nEOF\necho after"
    p = parse_command(cmd)
    assert [s.program for s in p.segments] == ["cat", "echo"]


def test_heredoc_dash_strips_leading_tabs_on_terminator() -> None:
    cmd = "cat <<-EOF\n\t\tbody\n\tEOF\necho after"
    p = parse_command(cmd)
    assert [s.program for s in p.segments] == ["cat", "echo"]


def test_heredoc_followed_by_pipe_on_same_line() -> None:
    cmd = "cat <<EOF | tail\nbody line\nEOF\n"
    p = parse_command(cmd)
    assert [s.program for s in p.segments] == ["cat", "tail"]
    assert p.segments[0].masked is True


def test_unterminated_heredoc_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_command("cat <<EOF\nbody with no terminator\n")


def test_unbalanced_double_quote_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_command('echo "unterminated')


def test_unbalanced_single_quote_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_command("echo 'unterminated")


def test_bare_newline_acts_as_separator() -> None:
    p = parse_command("pytest -q\necho done")
    assert [s.program for s in p.segments] == ["pytest", "echo"]
    assert p.segments[0].masked is True


def test_newline_inside_double_quotes_is_literal() -> None:
    p = parse_command('echo "line one\nline two"')
    assert len(p.segments) == 1
    assert p.segments[0].argv == ["echo", "line one\nline two"]


# ------------------------------------------------------------------------------------------
# Masking
# ------------------------------------------------------------------------------------------

MASKING_CASES = [
    ("pytest | tail -5", 0, True),
    ("pytest | tail -5", 1, False),
    ("npm test || true", 0, True),
    ("npm test || true", 1, False),
    ("pytest; true", 0, True),
    ("pytest; true", 1, False),
    ("pytest || :", 0, True),
    ("set +e; pytest", 1, False),
    ("pytest; echo done", 0, True),
    ("pytest -q", 0, False),
    ("a && b", 0, False),
    ("a && b && c", 0, False),
    ("a && b && c", 1, False),
    ("a && b && c", 2, False),
    ("pytest &", 0, False),
]


@pytest.mark.parametrize("cmd,index,expected", MASKING_CASES)
def test_masking(cmd: str, index: int, expected: bool) -> None:
    assert segs(cmd)[index].masked is expected


def test_set_pipefail_unmasks_piped_segment() -> None:
    s = segs("set -o pipefail; pytest | tail")
    assert s[1].program == "pytest"
    assert s[1].masked is False
    assert s[2].program == "tail"


def test_set_euo_pipefail_combined_flags() -> None:
    s = segs("set -euo pipefail; pytest | tail; echo done")
    # pipefail is on (pytest not pipe-masked) and errexit is on (pytest not ;-masked either).
    assert s[1].program == "pytest"
    assert s[1].masked is False


def test_errexit_unmasks_semicolon_segment() -> None:
    s = segs("set -e; pytest; echo done")
    assert s[1].program == "pytest"
    assert s[1].masked is False


def test_explicit_set_plus_e_keeps_semicolon_masking() -> None:
    s = segs("set -e; set +e; pytest; echo done")
    assert s[2].program == "pytest"
    assert s[2].masked is True


def test_last_segment_of_pipeline_masking_depends_on_own_context() -> None:
    s = segs("pytest | tail; echo done")
    assert s[0].masked is True  # piped
    assert s[1].masked is True  # tail, followed by ';' + more
    assert s[2].masked is False  # echo, last


def test_background_segment_never_masked() -> None:
    s = segs("pytest &")
    assert s[0].background is True
    assert s[0].masked is False


def test_group_inherits_outer_pipe_masking() -> None:
    s = segs("( pytest ) | tail")
    assert [seg.program for seg in s] == ["pytest", "tail"]
    assert s[0].masked is True
    assert s[1].masked is False


def test_group_internal_masking_independent_of_outer() -> None:
    s = segs("( pytest || true ) && echo done")
    assert s[0].program == "pytest"
    assert s[0].masked is True  # masked by its own internal `||`
    assert s[1].program == "true"
    assert s[1].masked is False  # last inside the group; group itself is not masked (&&)
    assert s[2].program == "echo"
    assert s[2].masked is False


def test_group_masking_propagates_along_trailing_and_chain() -> None:
    # The group's own masking (it is followed by `|| true`) decides the group's exit
    # status, which is whichever of pytest/echo actually runs last: if pytest fails, the
    # `&&` short-circuits and the group's exit status IS pytest's, so pytest must be masked
    # too, not just the literal last segment.
    s = segs("(pytest && echo ok) || true")
    assert [seg.program for seg in s] == ["pytest", "echo", "true"]
    assert s[0].masked is True  # pytest: part of the pure && chain ending the group
    assert s[1].masked is True  # echo ok: the last segment of the group
    assert s[2].masked is False  # true: decides the top-level status itself, nothing after it


def test_group_masking_propagates_along_longer_and_chain() -> None:
    s = segs("(a && b && c) || true")
    assert [seg.program for seg in s] == ["a", "b", "c", "true"]
    assert [seg.masked for seg in s] == [True, True, True, False]


def test_and_chain_indices_top_level_chain() -> None:
    # `evidence.py`'s `&&`-chain ambiguity fix: a plain top-level `&&` chain --
    # every segment is "in the running" for being the one a failed call's status belongs to.
    from proof_of_done.shell import and_chain_indices

    s = segs("ruff check . && pytest -q")
    assert and_chain_indices(s) == [True, True]


def test_and_chain_indices_stops_at_a_semicolon() -> None:
    from proof_of_done.shell import and_chain_indices

    s = segs("a ; b && c")
    assert [seg.program for seg in s] == ["a", "b", "c"]
    assert and_chain_indices(s) == [False, True, True]


def test_group_masking_does_not_propagate_past_a_broken_and_chain() -> None:
    # `a`'s own separator is `&&`, but the chain to the group's last segment is broken by
    # the `;` after `b`: `b;c`'s exit status is always `c`'s, regardless of `b` (and hence
    # regardless of `a`), so the group's own masking (it is followed by `|| true`) must not
    # reach back past that break to mask `a` too.
    s = segs("(a && b; c) || true")
    assert [seg.program for seg in s] == ["a", "b", "c", "true"]
    assert s[0].masked is False  # a: not on the chain that decides the group's exit status
    assert s[1].masked is True  # b: masked for its own reason (followed by `;` and more)
    assert s[2].masked is True  # c: last segment of the group, inherits the group's masking
    assert s[3].masked is False


def test_brace_group_masking_propagates_along_and_chain() -> None:
    s = segs("{ pytest && echo ok; } || true")
    assert [seg.program for seg in s] == ["pytest", "echo", "true"]
    assert s[0].masked is True
    assert s[1].masked is True
    assert s[2].masked is False


def test_bash_c_masking_propagates_along_and_chain() -> None:
    s = segs('bash -c "pytest && echo ok" || true')
    assert [seg.program for seg in s] == ["pytest", "echo", "true"]
    assert s[0].masked is True
    assert s[1].masked is True
    assert s[2].masked is False


def test_bash_c_string_masks_last_command_when_piped() -> None:
    s = segs('bash -c "pytest" | tail')
    assert [seg.program for seg in s] == ["pytest", "tail"]
    assert s[0].masked is True
    assert s[1].masked is False


def test_bash_c_internal_or_true_masks_regardless_of_outer() -> None:
    s = segs('bash -c "pytest || true"')
    assert [seg.program for seg in s] == ["pytest", "true"]
    assert s[0].masked is True
    assert s[1].masked is False


def test_sh_c_unwraps() -> None:
    s = segs('sh -c "pytest -q"')
    assert [seg.program for seg in s] == ["pytest"]


def test_bash_lc_unwraps() -> None:
    s = segs('bash -lc "pytest -q"')
    assert [seg.program for seg in s] == ["pytest"]


def test_zsh_c_unwraps() -> None:
    s = segs('zsh -c "pytest -q"')
    assert [seg.program for seg in s] == ["pytest"]


def test_bash_c_nesting_up_to_depth_three() -> None:
    import shlex as _shlex

    inner = "pytest"
    for _ in range(3):
        inner = "bash -c " + _shlex.quote(inner)
    s = segs(inner)
    assert [seg.program for seg in s] == ["pytest"]


def test_bash_c_nesting_stops_at_depth_three() -> None:
    import shlex as _shlex

    inner = "pytest"
    for _ in range(4):
        inner = "bash -c " + _shlex.quote(inner)
    s = segs(inner)
    # The 4th (outermost-after-3-unwraps) level is left un-expanded, not crashed on or dropped.
    assert s[-1].program == "bash"
    assert "-c" in s[-1].argv


# ------------------------------------------------------------------------------------------
# Wrappers
# ------------------------------------------------------------------------------------------

WRAPPER_CASES = [
    ("cd x && uv run --with rich pytest -q", ["cd", "pytest"], "uv run --with rich pytest -q"),
    ("timeout -s KILL 60 go test ./...", ["go"], "timeout -s KILL 60 go test ./..."),
    ("env CI=1 npm test", ["npm"], "env CI=1 npm test"),
    ("npx --yes vitest run", ["vitest"], "npx --yes vitest run"),
    ("poetry run pytest", ["pytest"], "poetry run pytest"),
    ("pdm run pytest -q", ["pytest"], "pdm run pytest -q"),
    ("hatch run pytest", ["pytest"], "hatch run pytest"),
    ("pnpm exec vitest run", ["vitest"], "pnpm exec vitest run"),
    ("pnpm dlx vitest run", ["vitest"], "pnpm dlx vitest run"),
    ("bunx vitest run", ["vitest"], "bunx vitest run"),
    ("time pytest -q", ["pytest"], "time pytest -q"),
    ("nice -n 10 pytest -q", ["pytest"], "nice -n 10 pytest -q"),
    ("command pytest -q", ["pytest"], "command pytest -q"),
    ("exec pytest -q", ["pytest"], "exec pytest -q"),
    ("uv run pytest -q", ["pytest"], "uv run pytest -q"),
    (
        "uv run --python 3.11 --project . pytest -q",
        ["pytest"],
        "uv run --python 3.11 --project . pytest -q",
    ),
    ("env -i FOO=1 pytest", ["pytest"], "env -i FOO=1 pytest"),
]


@pytest.mark.parametrize("cmd,expected_programs,expected_display", WRAPPER_CASES)
def test_wrapper_stripping(cmd: str, expected_programs: list[str], expected_display: str) -> None:
    s = segs(cmd)
    assert [seg.program for seg in s] == expected_programs
    real = next(seg for seg in s if seg.program == expected_programs[-1])
    assert real.display == expected_display


def test_uv_run_captures_value_options_as_wrappers_not_argv() -> None:
    s = segs("uv run --with rich pytest -q")[0]
    assert s.argv == ["pytest", "-q"]
    assert "--with" in s.wrappers and "rich" in s.wrappers


def test_timeout_duration_not_left_in_argv() -> None:
    s = segs("timeout -s KILL 60 go test ./...")[0]
    assert s.argv[0] == "go"
    assert "60" not in s.argv


def test_env_assignment_prefix_without_env_program() -> None:
    s = segs("CI=1 FOO=bar pytest -q")[0]
    assert s.program == "pytest"
    assert s.env == {"CI": "1", "FOO": "bar"}


# ------------------------------------------------------------------------------------------
# Program normalisation
# ------------------------------------------------------------------------------------------

NORMALIZE_CASES = [
    ("python3 -m pytest", "python"),
    ("python3.12 -m pytest", "python"),
    ("py -m pytest", "python"),
    ("py.test -q", "pytest"),
    ("python -m pytest", "python"),
    ("./run_tests.sh", "./run_tests.sh"),
    ("../run_tests.sh", "../run_tests.sh"),
    (".venv/bin/pytest -q", "pytest"),
    ("/usr/local/bin/pytest -q", "pytest"),
    ("node_modules/.bin/jest", "jest"),
]


@pytest.mark.parametrize("cmd,expected_program", NORMALIZE_CASES)
def test_program_normalization(cmd: str, expected_program: str) -> None:
    assert segs(cmd)[0].program == expected_program


# ------------------------------------------------------------------------------------------
# Prefix fnmatch matching
# ------------------------------------------------------------------------------------------

PREFIX_CASES = [
    (["npm", "run", "test:unit"], ["npm", "run", "test:*"], True),
    (["npm", "run", "build"], ["npm", "run", "test:*"], False),
    (["make", "test"], ["make", "*"], True),
    (["make"], ["make", "*"], False),
    (["python", "foo.py"], ["python", "*.py"], True),
    (["python", "foo.txt"], ["python", "*.py"], False),
    (["pytest", "-q"], ["pytest"], True),
    (["pytest"], ["pytest", "-q"], False),
    (["vitest", "run"], ["vitest", "run"], True),
    (["vitest"], ["vitest", "run"], False),
]


@pytest.mark.parametrize("argv,prefix,expected", PREFIX_CASES)
def test_match_prefix(argv: list[str], prefix: list[str], expected: bool) -> None:
    assert match_prefix(argv, prefix) is expected


def test_matches_any_uses_command_regex_too() -> None:
    import re

    seg = segs("go test -run TestFoo ./...")[0]
    assert matches_any(seg, [["go", "test"]], None) is True
    assert matches_any(seg, [["cargo", "test"]], re.compile(r"-run \w+")) is True
    assert matches_any(seg, [["cargo", "test"]], re.compile(r"nomatch")) is False


# ------------------------------------------------------------------------------------------
# Read-only commands
# ------------------------------------------------------------------------------------------

READ_ONLY_PREFIXES = [
    ["cat"],
    ["ls"],
    ["grep"],
    ["head"],
    ["tail"],
    ["find"],
    ["echo"],
    ["printf"],
    ["wc"],
    ["sed", "-n"],
    ["pwd"],
    ["which"],
    ["git", "status"],
    ["git", "diff"],
    ["git", "log"],
    ["git", "show"],
]

READ_ONLY_CASES = [
    ("cat file.txt", True),
    ("sed -n '1,5p' file.txt", True),
    ("sed -i 's/a/b/' file.txt", False),
    ("git status", True),
    ("git diff HEAD~1", True),
    ("git commit -m x", False),
    ("pytest -q", False),
    ("awk '{print}' file.txt", False),
]


@pytest.mark.parametrize("cmd,expected", READ_ONLY_CASES)
def test_is_read_only(cmd: str, expected: bool) -> None:
    assert is_read_only(segs(cmd)[0], READ_ONLY_PREFIXES) is expected


# ------------------------------------------------------------------------------------------
# Exclude args
# ------------------------------------------------------------------------------------------

EXCLUDE_ARGS = ["--collect-only", "--co", "--help", "-h", "--version", "--watch", "--dry-run"]

EXCLUDE_CASES = [
    ("pytest --collect-only", True),
    ("pytest --co -q", True),
    ("pytest -h", True),
    ("pytest --help", True),
    ("pytest --watch", True),
    ("pytest -q", False),
    ("pytest --collect-only=foo", True),  # --opt=value form still disqualifies
    ("build --dry-run", True),
    ("build --target=--dry-run", False),  # value text, not the flag itself
]


@pytest.mark.parametrize("cmd,expected", EXCLUDE_CASES)
def test_is_excluded(cmd: str, expected: bool) -> None:
    assert is_excluded(segs(cmd)[0], EXCLUDE_ARGS) is expected


# ------------------------------------------------------------------------------------------
# Partial args
# ------------------------------------------------------------------------------------------

PARTIAL_ARGS = [
    "-k",
    "--lf",
    "--last-failed",
    "--sw",
    "-run",
    "-t",
    "--testNamePattern",
    "--test-name-pattern",
    "--deselect",
]

PARTIAL_CASES = [
    ("pytest -k foo", True),
    ("pytest --lf", True),
    ("go test -run TestFoo", True),
    ("pytest -q", False),
    ("pytest tests/test_x.py", True),
    ("pytest tests/test_x.py::test_y", True),
    ("go test ./...", False),
    ("go test .", False),
    ("go test ./", False),
    ("pytest .", False),
    ("npm test src/app.test.js", True),
    ("pytest -q --tb=short", False),
    ("pytest --testNamePattern=foo", True),
]


@pytest.mark.parametrize("cmd,expected", PARTIAL_CASES)
def test_is_partial(cmd: str, expected: bool) -> None:
    assert is_partial(segs(cmd)[0], PARTIAL_ARGS) is expected


# ------------------------------------------------------------------------------------------
# Disqualifiers
# ------------------------------------------------------------------------------------------

DISQUALIFY_CASES = [
    "pytest(){ echo 5 passed; }; pytest",
    "function pytest { :; }; pytest",
    "alias pytest=true; pytest",
    "PATH=./fake:$PATH pytest",
    "export PATH=./fake:$PATH; pytest",
]


@pytest.mark.parametrize("cmd", DISQUALIFY_CASES)
def test_disqualified_true_for_redefined_or_path_tampered(cmd: str) -> None:
    cmd_parsed = parse_command(cmd)
    pytest_segs = [s for s in cmd_parsed.segments if s.program == "pytest"]
    assert pytest_segs, f"no pytest segment produced for {cmd!r}"
    assert disqualified(cmd_parsed, pytest_segs[-1]) is True


def test_not_disqualified_when_clean() -> None:
    cmd_parsed = parse_command("pytest -q")
    assert disqualified(cmd_parsed, cmd_parsed.segments[0]) is False


def test_disqualifies_every_candidate_in_the_call() -> None:
    # PATH= anywhere in the call disqualifies every segment, not just the one it prefixes.
    cmd_parsed = parse_command("PATH=./fake:$PATH true; pytest -q")
    pytest_seg = cmd_parsed.segments[-1]
    assert pytest_seg.program == "pytest"
    assert disqualified(cmd_parsed, pytest_seg) is True


# ------------------------------------------------------------------------------------------
# Bash edit extraction: redirects
# ------------------------------------------------------------------------------------------


def _edit_paths(
    cmd: str, cwd: str | None = None, **kw: object
) -> list[tuple[str | None, bool, bool]]:
    p = parse_command(cmd, cwd)
    edits = bash_edits(p, cwd, **kw)  # type: ignore[arg-type]
    return [(e.path, e.is_dir, e.whole_tree) for e in edits]


REDIRECT_CASES = [
    ("echo hi > out.txt", [("out.txt", False, False)]),
    ("echo hi >> out.txt", [("out.txt", False, False)]),
    ("echo hi &> out.txt", [("out.txt", False, False)]),
    ("echo hi >| out.txt", [("out.txt", False, False)]),
    ("echo hi 2> err.txt", [("err.txt", False, False)]),
    ("echo hi > /dev/null", []),
    ("echo hi 2>&1", []),
    ("echo hi >&2", []),
    ("cmd < in.txt", []),
    ("cmd <<EOF\nbody\nEOF\n", []),
]


@pytest.mark.parametrize("cmd,expected", REDIRECT_CASES)
def test_redirect_edit_targets(cmd: str, expected: list[tuple[str, bool, bool]]) -> None:
    assert _edit_paths(cmd) == expected


def test_redirect_edit_is_phase_zero() -> None:
    p = parse_command("pytest -q > out.txt")
    et = bash_edits(p, None)
    assert et[0].phase == 0
    assert et[0].seg_index == 0


def test_tee_targets_are_phase_zero() -> None:
    p = parse_command("pytest -q | tee out.log")
    et = bash_edits(p, None)
    assert any(e.path == "out.log" and e.phase == 0 for e in et)


def test_tee_append_flag_still_finds_target() -> None:
    assert _edit_paths("pytest | tee -a out.log") == [("out.log", False, False)]


# ------------------------------------------------------------------------------------------
# Bash edit extraction: sed / perl in-place
# ------------------------------------------------------------------------------------------

SED_PERL_CASES = [
    ("sed -i 's/a/b/' file.py", [("file.py", False, False)]),
    ("sed -i.bak 's/a/b/' file.py", [("file.py", False, False)]),
    ("sed -i '' 's/a/b/' file.py", [("file.py", False, False)]),  # BSD form
    ("sed --in-place 's/a/b/' file.py", [("file.py", False, False)]),
    (
        "sed -i -e 's/a/b/' file.py file2.py",
        [("file.py", False, False), ("file2.py", False, False)],
    ),
    ("sed -n '1,5p' file.py", []),  # read-only, not an edit
    ("perl -pi -e 's/a/b/' file.py", [("file.py", False, False)]),
    ("perl -i.bak -pe 's/a/b/' file.py", [("file.py", False, False)]),
]


@pytest.mark.parametrize("cmd,expected", SED_PERL_CASES)
def test_sed_perl_edit_targets(cmd: str, expected: list[tuple[str, bool, bool]]) -> None:
    assert _edit_paths(cmd) == expected


# ------------------------------------------------------------------------------------------
# Bash edit extraction: file commands
# ------------------------------------------------------------------------------------------

FILE_CMD_CASES = [
    ("mv a.txt b.txt", [("a.txt", False, False), ("b.txt", False, False)]),
    ("cp a.txt b.txt", [("b.txt", False, False)]),
    ("cp -r src dest", [("dest", False, False)]),
    ("rm a.txt b.txt", [("a.txt", False, False), ("b.txt", False, False)]),
    ("rm -rf build", [("build", False, False)]),
    ("touch a.txt", [("a.txt", False, False)]),
    ("touch a.txt b.txt", [("a.txt", False, False), ("b.txt", False, False)]),
    ("ln -s target.txt link.txt", [("link.txt", False, False)]),
    ("ln target.txt link.txt", [("link.txt", False, False)]),
    ("truncate -s 0 file.txt", [("file.txt", False, False)]),
    ("truncate --size 0 file.txt", [("file.txt", False, False)]),
    ("dd if=/dev/zero of=out.img bs=1M count=1", [("out.img", False, False)]),
    ("dd if=in.img of=out.img", [("out.img", False, False)]),
    ("rsync -av src/ dest/", [("dest", True, False)]),
    ("install -m 0644 a.txt /usr/local/bin/a.txt", [("/usr/local/bin/a.txt", False, False)]),
    (
        "install a.txt b.txt /usr/local/bin/",
        [("/usr/local/bin", True, False)],
    ),
    ("patch app.py < fix.diff", [("app.py", False, False)]),
    ("patch < fix.diff", [(None, True, True)]),
    ("patch -p1 app.py < fix.diff", [("app.py", False, False)]),
]


@pytest.mark.parametrize("cmd,expected", FILE_CMD_CASES)
def test_file_command_edit_targets(cmd: str, expected: list[tuple[str, bool, bool]]) -> None:
    assert _edit_paths(cmd) == expected


def test_bash_edits_are_phase_one() -> None:
    p = parse_command("rm a.txt")
    et = bash_edits(p, None)
    assert et[0].phase == 1
    assert et[0].source == "bash"


# ------------------------------------------------------------------------------------------
# Bash edit extraction: git
# ------------------------------------------------------------------------------------------

GIT_CASES = [
    ("git apply fix.patch", [(None, True, True)]),
    ("git mv a.txt b.txt", [("a.txt", False, False), ("b.txt", False, False)]),
    ("git rm a.txt", [("a.txt", False, False)]),
    ("git checkout -- file.py", [("file.py", False, False)]),
    ("git checkout -- a.py b.py", [("a.py", False, False), ("b.py", False, False)]),
    ("git checkout main -- file.py", [("file.py", False, False)]),
    ("git checkout main", [(None, True, True)]),
    ("git checkout .", [(None, True, True)]),
    ("git switch main", [(None, True, True)]),
    ("git restore .", [(None, True, True)]),
    ("git pull", [(None, True, True)]),
    ("git merge main", [(None, True, True)]),
    ("git rebase main", [(None, True, True)]),
    ("git reset --hard HEAD~1", [(None, True, True)]),
    ("git reset HEAD~1", []),
    ("git reset", []),
    ("git stash", [(None, True, True)]),
    ("git stash push", [(None, True, True)]),
    ("git stash pop", [(None, True, True)]),
    ("git stash apply", [(None, True, True)]),
    ("git stash list", []),
    ("git stash show", []),
    ("git cherry-pick abc123", [(None, True, True)]),
    ("git revert abc123", [(None, True, True)]),
    ("git am patch.mbox", [(None, True, True)]),
    ("git clean -fd", [(None, True, True)]),
    ("git status", []),
    ("git diff", []),
    ("git log", []),
    ("git show HEAD", []),
]


@pytest.mark.parametrize("cmd,expected", GIT_CASES)
def test_git_edit_targets(cmd: str, expected: list[tuple[str, bool, bool]]) -> None:
    assert _edit_paths(cmd) == expected


# ------------------------------------------------------------------------------------------
# Bash edit extraction: whole-tree archive commands
# ------------------------------------------------------------------------------------------

ARCHIVE_CASES = [
    ("tar xvf archive.tar", [(None, True, True)]),
    ("tar -xvf archive.tar", [(None, True, True)]),
    ("tar --extract -f archive.tar", [(None, True, True)]),
    ("tar cvf archive.tar dir/", []),
    ("tar -tvf archive.tar", []),
    ("unzip archive.zip", [(None, True, True)]),
    ("unzip -o archive.zip -d out", [(None, True, True)]),
]


@pytest.mark.parametrize("cmd,expected", ARCHIVE_CASES)
def test_archive_edit_targets(cmd: str, expected: list[tuple[str, bool, bool]]) -> None:
    assert _edit_paths(cmd) == expected


# ------------------------------------------------------------------------------------------
# Bash edit extraction: formatters
# ------------------------------------------------------------------------------------------

FORMATTER_CASES = [
    ("ruff format .", [("", True, False)]),
    ("ruff format --check .", []),
    ("ruff format", [(None, True, True)]),
    ("ruff check --fix .", [("", True, False)]),
    ("ruff check .", []),
    ("black app.py", [("app.py", False, False)]),
    ("black --check app.py", []),
    ("black --diff app.py", []),
    ("isort app.py", [("app.py", False, False)]),
    ("isort --check app.py", []),
    ("autopep8 -i app.py", [("app.py", False, False)]),
    ("autopep8 app.py", []),
    ("prettier --write src/", [("src", True, False)]),
    ("prettier -w app.ts", [("app.ts", False, False)]),
    ("prettier --check src/", []),
    ("prettier src/", []),
    ("eslint --fix src/", [("src", True, False)]),
    ("eslint src/", []),
    ("cargo fmt", [(None, True, True)]),
    ("cargo fmt --check", []),
    ("rustfmt src/main.rs", [("src/main.rs", False, False)]),
    ("rustfmt --check src/main.rs", []),
    ("gofmt -w main.go", [("main.go", False, False)]),
    ("gofmt main.go", []),
    ("go fmt ./...", [("...", False, False)]),
    ("goimports -w main.go", [("main.go", False, False)]),
    ("goimports main.go", []),
]


@pytest.mark.parametrize("cmd,expected", FORMATTER_CASES)
def test_formatter_edit_targets(cmd: str, expected: list[tuple[str, bool, bool]]) -> None:
    assert _edit_paths(cmd) == expected


def test_formatter_edit_source_label() -> None:
    p = parse_command("black app.py")
    et = bash_edits(p, None)
    assert et[0].source == "formatter"


# ------------------------------------------------------------------------------------------
# Bash edit extraction: caller-supplied extension lists
# ------------------------------------------------------------------------------------------


def test_custom_bash_writes_extension() -> None:
    p = parse_command("mytool --write out.txt extra.txt")
    et = bash_edits(p, None, bash_writes=[("mytool",)])
    paths = {(e.path, e.is_dir) for e in et}
    assert ("out.txt", False) in paths
    assert ("extra.txt", False) in paths


def test_custom_formatter_extension() -> None:
    p = parse_command("mytool-fmt src/")
    et = bash_edits(p, None, formatters=[("mytool-fmt",)])
    assert et and et[0].path == "src" and et[0].is_dir is True


def test_custom_tree_command_extension() -> None:
    p = parse_command("myvcs sync")
    et = bash_edits(p, None, tree_commands=[("myvcs", "sync")])
    assert et and et[0].whole_tree is True


# ------------------------------------------------------------------------------------------
# cwd resolution
# ------------------------------------------------------------------------------------------


def test_relative_edit_target_resolved_against_call_cwd() -> None:
    p = parse_command("touch a.txt", "/work/demo-app")
    et = bash_edits(p, "/work/demo-app")
    assert et[0].path == "/work/demo-app/a.txt"


def test_relative_edit_target_resolved_against_in_command_cd() -> None:
    p = parse_command("cd sub && touch a.txt", "/work/demo-app")
    et = bash_edits(p, "/work/demo-app")
    assert et[0].path == "/work/demo-app/sub/a.txt"


def test_absolute_edit_target_ignores_cwd() -> None:
    p = parse_command("touch /tmp/a.txt", "/work/demo-app")
    et = bash_edits(p, "/work/demo-app")
    assert et[0].path == "/tmp/a.txt"


def test_cd_chain_accumulates() -> None:
    p = parse_command("cd a && cd b && touch c.txt", "/work/demo-app")
    et = bash_edits(p, "/work/demo-app")
    assert et[0].path == "/work/demo-app/a/b/c.txt"


# ------------------------------------------------------------------------------------------
# ~/$HOME target expansion (tamper-evasion fix)
# ------------------------------------------------------------------------------------------

_FAKE_HOME = "/work/home"


@pytest.mark.parametrize(
    "cmd",
    [
        "echo '...PROOF_OF_DONE...' >> ~/.claude/settings.json",
        "echo '...PROOF_OF_DONE...' >> $HOME/.claude/settings.json",
        "echo '...PROOF_OF_DONE...' >> ${HOME}/.claude/settings.json",
    ],
)
def test_redirection_target_under_tilde_or_home_resolves_to_real_home(cmd: str) -> None:
    assert _edit_paths(cmd, "/work/demo-app", home=_FAKE_HOME) == [
        (f"{_FAKE_HOME}/.claude/settings.json", False, False)
    ]


def test_tee_target_under_tilde_resolves_to_real_home() -> None:
    p = parse_command("echo x | tee ~/.claude/settings.json", "/work/demo-app")
    et = bash_edits(p, "/work/demo-app", home=_FAKE_HOME)
    assert any(e.path == f"{_FAKE_HOME}/.claude/settings.json" for e in et)


def test_sed_i_target_under_tilde_resolves_to_real_home() -> None:
    assert _edit_paths(
        "sed -i '' 's/a/b/' ~/.claude/settings.json", "/work/demo-app", home=_FAKE_HOME
    ) == [(f"{_FAKE_HOME}/.claude/settings.json", False, False)]


def test_mv_and_cp_targets_under_tilde_resolve_to_real_home() -> None:
    assert _edit_paths("mv a.txt ~/.claude/settings.json", "/work/demo-app", home=_FAKE_HOME) == [
        ("/work/demo-app/a.txt", False, False),
        (f"{_FAKE_HOME}/.claude/settings.json", False, False),
    ]
    assert _edit_paths("cp a.txt ~/.claude/settings.json", "/work/demo-app", home=_FAKE_HOME) == [
        (f"{_FAKE_HOME}/.claude/settings.json", False, False)
    ]


def test_tilde_target_without_a_home_stays_unresolvable() -> None:
    # No `home` passed: `~/...` must not be silently (and wrongly) resolved as relative to
    # the call's cwd -- it stays unresolvable, same as any other unexpanded `$VAR`.
    assert _edit_paths("touch ~/.claude/settings.json", "/work/demo-app") == []


def test_tilde_other_user_is_never_expanded() -> None:
    # `~otheruser/...` is a different user's home, never this one's -- must not be rewritten.
    assert _edit_paths("touch ~otheruser/file.txt", "/work/demo-app", home=_FAKE_HOME) == [
        ("/work/demo-app/~otheruser/file.txt", False, False)
    ]


def test_other_dollar_var_target_stays_unresolvable_even_with_home() -> None:
    assert _edit_paths("touch $OTHER/file.txt", "/work/demo-app", home=_FAKE_HOME) == []


def test_cd_inside_subshell_does_not_leak_out() -> None:
    p = parse_command("( cd sub && touch a.txt ); touch b.txt", "/work/demo-app")
    et = bash_edits(p, "/work/demo-app")
    paths = [e.path for e in et]
    assert "/work/demo-app/sub/a.txt" in paths
    assert "/work/demo-app/b.txt" in paths  # not /work/demo-app/sub/b.txt


def test_cd_inside_brace_group_leaks_out() -> None:
    p = parse_command("{ cd sub && touch a.txt; }; touch b.txt", "/work/demo-app")
    et = bash_edits(p, "/work/demo-app")
    paths = [e.path for e in et]
    assert "/work/demo-app/sub/b.txt" in paths


# ------------------------------------------------------------------------------------------
# Unresolvable targets: $VAR, $(...), backticks, globs
# ------------------------------------------------------------------------------------------


def test_dollar_var_target_is_skipped() -> None:
    assert _edit_paths("sed -i 's/a/b/' $FILE") == []


def test_dollar_paren_command_substitution_target_is_skipped() -> None:
    assert _edit_paths("touch $(mktemp)") == []


def test_backtick_command_substitution_target_is_skipped() -> None:
    assert _edit_paths("touch `mktemp`") == []


def test_dollar_paren_does_not_confuse_group_depth_tracking() -> None:
    # A real regression: `(` inside `$(...)` must not be read as a subshell-group opener.
    p = parse_command("touch $(mktemp) && pytest -q")
    assert [s.program for s in p.segments] == ["touch", "pytest"]


def test_glob_chars_replaced_with_literal_x() -> None:
    assert _edit_paths("rm src/*.py") == [("src/x.py", False, False)]


def test_glob_question_mark_and_bracket_replaced() -> None:
    assert _edit_paths("rm file?.py") == [("filex.py", False, False)]
    assert _edit_paths("rm file[0-9].py") == [("filex.py", False, False)]


# ------------------------------------------------------------------------------------------
# `display` text
# ------------------------------------------------------------------------------------------

DISPLAY_CASES = [
    ("pytest -q 2>&1 | tail -5", 0, "pytest -q"),
    ("cd app && uv run pytest -q 2>&1 | tail -5", 1, "uv run pytest -q"),
    ("pytest -q > out.txt", 0, "pytest -q"),
    ("npm test || true", 0, "npm test"),
    ("env CI=1 npm test", 0, "env CI=1 npm test"),
]


@pytest.mark.parametrize("cmd,index,expected", DISPLAY_CASES)
def test_display_text(cmd: str, index: int, expected: str) -> None:
    assert segs(cmd)[index].display == expected


# ------------------------------------------------------------------------------------------
# Dataclass wiring sanity (fields exist with the documented types/shape)
# ------------------------------------------------------------------------------------------


def test_redirect_dataclass_shape() -> None:
    r = Redirect(op=">", target="out.txt")
    assert r.op == ">"
    assert r.target == "out.txt"


def test_edit_target_dataclass_shape() -> None:
    et = EditTarget(
        path="a.txt", is_dir=False, whole_tree=False, seg_index=0, phase=1, source="bash"
    )
    assert et.path == "a.txt"
    assert et.source == "bash"


def test_segment_index_is_contiguous_and_position_ordered() -> None:
    s = segs("cd a && ( touch x; touch y ) && pytest")
    assert [seg.index for seg in s] == list(range(len(s)))


def test_parsed_command_defines_and_sets_path_shared_across_segments() -> None:
    p = parse_command("alias pytest=true; PATH=./fake:$PATH pytest")
    assert p.defines == {"pytest"}
    assert p.sets_path is True
    for seg in p.segments:
        assert seg.defines == {"pytest"}
        assert seg.sets_path is True
