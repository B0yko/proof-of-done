from __future__ import annotations

from proof_of_done.paths import could_match_under, find_project_root, glob_match, to_rel

# --------------------------------------------------------------------------------------
# glob_match
# --------------------------------------------------------------------------------------


def test_literal_match() -> None:
    assert glob_match("pyproject.toml", "pyproject.toml")
    assert not glob_match("pyproject.toml", "pyproject.toml.bak")
    assert not glob_match("pyproject.toml", "sub/pyproject.toml")


def test_star_does_not_cross_slash() -> None:
    # A pattern with no "/" is a single segment: it only ever matches a top-level file.
    assert glob_match("*.md", "readme.md")
    assert not glob_match("*.md", "docs/readme.md")
    assert not glob_match("*.md", "docs/sub/readme.md")


def test_star_within_one_segment() -> None:
    assert glob_match("src/*.py", "src/app.py")
    assert not glob_match("src/*.py", "src/pkg/app.py")
    assert not glob_match("src/*.py", "app.py")


def test_question_mark_single_char_no_slash_cross() -> None:
    assert glob_match("a?c", "abc")
    assert not glob_match("a?c", "ac")
    assert not glob_match("a?c", "abbc")
    assert not glob_match("a?c", "a/c")


def test_double_star_matches_any_depth_from_start() -> None:
    assert glob_match("**/*.md", "readme.md")
    assert glob_match("**/*.md", "docs/readme.md")
    assert glob_match("**/*.md", "docs/sub/readme.md")
    assert not glob_match("**/*.md", "docs/readme.txt")


def test_double_star_in_middle() -> None:
    assert glob_match("src/**/test_*.py", "src/test_app.py")
    assert glob_match("src/**/test_*.py", "src/pkg/test_app.py")
    assert glob_match("src/**/test_*.py", "src/pkg/sub/test_app.py")
    assert not glob_match("src/**/test_*.py", "lib/pkg/test_app.py")
    assert not glob_match("src/**/test_*.py", "src/pkg/app_test.py")


def test_double_star_trailing_matches_everything_beneath() -> None:
    assert glob_match("dist/**", "dist/a.txt")
    assert glob_match("dist/**", "dist/sub/b.txt")
    assert glob_match("dist/**", "dist/sub/deep/c.txt")
    assert not glob_match("dist/**", "distant/a.txt")
    assert not glob_match("dist/**", "other/dist/a.txt")


def test_bracket_class() -> None:
    assert glob_match("file[12].txt", "file1.txt")
    assert glob_match("file[12].txt", "file2.txt")
    assert not glob_match("file[12].txt", "file3.txt")


def test_bracket_negated_class() -> None:
    assert glob_match("file[!12].txt", "file3.txt")
    assert not glob_match("file[!12].txt", "file1.txt")
    assert not glob_match("file[!12].txt", "file2.txt")


def test_bracket_range() -> None:
    assert glob_match("file[0-9].txt", "file5.txt")
    assert not glob_match("file[0-9].txt", "filea.txt")


def test_unterminated_bracket_is_literal() -> None:
    assert glob_match("weird[thing", "weird[thing")
    assert not glob_match("weird[thing", "weirdthing")


def test_top_level_vs_any_depth_docs_example() -> None:
    # Explicitly the two contrasting cases called out in the spec.
    assert glob_match("*.md", "readme.md")
    assert not glob_match("*.md", "docs/readme.md")
    assert glob_match("**/*.md", "docs/readme.md")


def test_empty_pattern_and_path() -> None:
    assert glob_match("", "")
    assert not glob_match("", "a")


def test_multiple_double_star_segments() -> None:
    assert glob_match("**/**/x.py", "x.py")
    assert glob_match("**/**/x.py", "a/x.py")
    assert glob_match("**/**/x.py", "a/b/c/x.py")


# --------------------------------------------------------------------------------------
# could_match_under
# --------------------------------------------------------------------------------------


def test_could_match_under_trailing_double_star() -> None:
    assert could_match_under("dist/**", "dist")
    assert could_match_under("dist/**", "dist/sub")
    assert not could_match_under("dist/**", "other")


def test_could_match_under_leading_double_star_matches_any_prefix() -> None:
    assert could_match_under("**/*.py", "anything")
    assert could_match_under("**/*.py", "anything/deeper")


def test_could_match_under_top_level_only_pattern_excludes_subdirs() -> None:
    assert not could_match_under("*.py", "src")
    assert not could_match_under("*.py", "src/sub")


def test_could_match_under_requires_matching_prefix_literal() -> None:
    assert could_match_under("src/**/test_*.py", "src")
    assert could_match_under("src/**/test_*.py", "src/pkg")
    assert not could_match_under("src/**/test_*.py", "lib")


def test_could_match_under_empty_dir_prefix_is_root() -> None:
    # An empty dir prefix (the project root itself) can always host a match.
    assert could_match_under("*.py", "")
    assert could_match_under("src/**", "")


# --------------------------------------------------------------------------------------
# to_rel
# --------------------------------------------------------------------------------------


def test_to_rel_basic() -> None:
    assert to_rel("/work/demo-app/src/app.py", "/work/demo-app") == "src/app.py"


def test_to_rel_root_itself() -> None:
    assert to_rel("/work/demo-app", "/work/demo-app") == ""


def test_to_rel_outside_root() -> None:
    assert to_rel("/work/other-app/src/app.py", "/work/demo-app") is None
    assert to_rel("/work/demo-app-2/src/app.py", "/work/demo-app") is None


def test_to_rel_trailing_slashes() -> None:
    assert to_rel("/work/demo-app/src/app.py/", "/work/demo-app/") == "src/app.py"
    assert to_rel("/work/demo-app//src//app.py", "/work/demo-app") == "src/app.py"


def test_to_rel_dot_dot_lexical() -> None:
    assert to_rel("/work/demo-app/src/../src/app.py", "/work/demo-app") == "src/app.py"
    assert to_rel("/work/demo-app/../demo-app/src/app.py", "/work/demo-app") == "src/app.py"


def test_to_rel_dot_dot_escapes_root() -> None:
    assert to_rel("/work/demo-app/../other/app.py", "/work/demo-app") is None


def test_to_rel_dot_dot_above_absolute_root_is_noop() -> None:
    # ".." above "/" collapses to "/", it never becomes negative/invalid.
    assert to_rel("/../work/demo-app/app.py", "/work/demo-app") == "app.py"


def test_to_rel_current_dir_components() -> None:
    assert to_rel("/work/demo-app/./src/./app.py", "/work/demo-app") == "src/app.py"


# --------------------------------------------------------------------------------------
# find_project_root
# --------------------------------------------------------------------------------------


def test_find_project_root_marker_at_cwd() -> None:
    markers = {"/work/demo-app/.git"}
    assert (
        find_project_root("/work/demo-app", "/home/user", lambda p: p in markers)
        == "/work/demo-app"
    )


def test_find_project_root_marker_in_ancestor() -> None:
    markers = {"/home/user/project/.proof-of-done.yaml"}
    assert (
        find_project_root("/home/user/project/src/pkg", "/home/user", lambda p: p in markers)
        == "/home/user/project"
    )


def test_find_project_root_stops_at_home_inclusive() -> None:
    markers = {"/home/user/.git"}
    assert (
        find_project_root("/home/user/work/deep", "/home/user", lambda p: p in markers)
        == "/home/user"
    )


def test_find_project_root_never_goes_above_home() -> None:
    # A marker exists above home, but must never be found because the walk stops at home.
    markers = {"/home/.git"}
    assert (
        find_project_root("/home/user/work/deep", "/home/user", lambda p: p in markers)
        == "/home/user/work/deep"
    )


def test_find_project_root_no_marker_returns_cwd() -> None:
    assert find_project_root("/home/user/work/deep", "/home/user", lambda p: False) == (
        "/home/user/work/deep"
    )


def test_find_project_root_prefers_nearest_ancestor() -> None:
    markers = {
        "/home/user/project/.git",
        "/home/user/project/nested/.git",
    }
    assert (
        find_project_root("/home/user/project/nested/sub", "/home/user", lambda p: p in markers)
        == "/home/user/project/nested"
    )


def test_find_project_root_cwd_outside_home_walks_to_filesystem_root() -> None:
    markers = {"/var/tmp/proj/.git"}
    assert (
        find_project_root("/var/tmp/proj/sub", "/home/user", lambda p: p in markers)
        == "/var/tmp/proj"
    )


def test_find_project_root_cwd_outside_home_no_marker_returns_cwd() -> None:
    assert find_project_root("/var/tmp/proj/sub", "/home/user", lambda p: False) == (
        "/var/tmp/proj/sub"
    )
