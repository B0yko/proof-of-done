"""Tests for `proof_of_done.shell`: segmentation, wrapper stripping, masking, matching and
bash edit extraction. Table-driven throughout; see PLAN §5 for the behaviour these encode."""

from __future__ import annotations

import pytest

from proof_of_done.shell import (
    ParseError,
    Segment,
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
