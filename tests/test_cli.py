import inspect
import re
import sys

import pytest

from crossweave import cli, search, sessions
from crossweave.cli import UsageError, build_command


def test_claude_all_flags():
    cmd = build_command("claude", ["-p", "-c", "-m", "sonnet", "--add-dir", "/tmp", "-y", "hello world"])
    assert cmd == [
        "claude", "-p", "--continue", "--model", "sonnet",
        "--add-dir", "/tmp", "--dangerously-skip-permissions", "hello world",
    ]


def test_codex_print_and_continue_uses_exec_resume_last():
    cmd = build_command("codex", ["-p", "-c"])
    assert cmd == ["codex", "exec", "resume", "--last"]


def test_codex_interactive_ignores_continue():
    # codex has no non-interactive "continue"; -c without -p is a no-op,
    # matching what the underlying `codex` CLI supports.
    cmd = build_command("codex", ["-c"])
    assert cmd == ["codex"]


def test_codex_yolo_maps_to_approve_for_me():
    cmd = build_command("codex", ["-y"])
    assert cmd == ["codex", "--approve-for-me"]


def test_kimi_all_flags():
    cmd = build_command("kimi", ["-p", "-c", "-m", "kimi-for-coding", "-y", "fix the flaky test"])
    assert cmd == ["kimi", "-p", "-c", "-m", "kimi-for-coding", "-y", "fix the flaky test"]


def test_kimi_print_without_a_prompt_is_a_usage_error():
    """kimi's -p does not read stdin (unlike claude's and codex's), so a bare
    `-p` can only be a mistake."""
    with pytest.raises(UsageError):
        build_command("kimi", ["-p", "-c"])


def test_claude_print_without_a_prompt_is_allowed():
    """claude's -p reads the prompt from stdin, so a bare `-p` is a real
    invocation there and must keep working."""
    assert build_command("claude", ["-p"]) == ["claude", "-p"]


def test_step_all_flags():
    cmd = build_command("step", ["-p", "-c", "-m", "step-5-preview", "-y", "fix it"])
    assert cmd == ["step", "-p", "-c", "--model", "step-5-preview",
                   "--approval-mode", "auto", "--non-interactive-approval", "allow", "fix it"]


def test_step_yolo_also_covers_the_no_ui_fallback():
    """step decides tool approval with --approval-mode when there is a UI and
    falls back to --non-interactive-approval when there is not, which denies
    by default. -y has to set both or `ai step -p -y` still gets stopped."""
    cmd = build_command("step", ["-y", "go"])
    assert cmd == ["step", "--approval-mode", "auto",
                   "--non-interactive-approval", "allow", "go"]


def test_step_rejects_add_dir():
    """step has no per-workspace directory flag; the nearest thing it has,
    --session-dir, is where sessions live, not what to work on."""
    with pytest.raises(UsageError):
        build_command("step", ["--add-dir", "/tmp", "go"])


def test_step_accepts_a_plain_prompt():
    """Unlike kimi, step takes a bare positional prompt, which is what makes
    `ai 3 step` a simple handoff rather than a seeded one-shot."""
    assert build_command("step", ["hello"]) == ["step", "hello"]


def test_multiple_add_dirs_repeat_flag():
    cmd = build_command("claude", ["--add-dir", "/a", "--add-dir", "/b"])
    assert cmd == ["claude", "--add-dir", "/a", "--add-dir", "/b"]


def test_the_unknown_tool_error_names_every_command_it_dispatches():
    """`ai all` reported "expected ... sessions/full/search/resume/update" --
    five subcommands, while main() dispatches six, and neither `stats` nor
    `step` was in the list. The message is generated from the same constant
    the tests check main() against, so it cannot drift again silently."""
    dispatched = set()
    source = inspect.getsource(cli.main)
    for name in re.findall(r'tool == "(\w+)"', source):
        dispatched.add(name)

    with pytest.raises(UsageError) as exc_info:
        build_command("all", [])

    message = str(exc_info.value)
    for name in sorted(dispatched | set(cli.TOOLS)):
        assert name in message, name


def test_help_lists_every_tool_and_every_judge():
    """`ai --help` printed [--judge claude|codex|kimi] for the two commits
    after step became a judge, and "asks claude" after claude stopped being
    the default -- a literal string, describing lists that live in TOOLS and
    JUDGE_CMD. USAGE is built from both now; this holds the printed line to
    them the way the test above holds the unknown-tool message to main()."""
    search_line = next(line for line in cli.USAGE.splitlines() if "cw search" in line)
    tools_shown = re.search(r"\[--tool ([^\]]+)\]", search_line)
    judges_shown = re.search(r"\[--judge ([^\]]+)\]", search_line)
    assert tools_shown and judges_shown, search_line
    assert tools_shown.group(1).split("|") == list(cli.TOOLS)
    assert judges_shown.group(1).split("|") == list(search.JUDGE_CMD)
    # the example names the judge the command will actually ask
    assert f"asks {search.DEFAULT_JUDGE} (the default judge)" in cli.USAGE


def test_double_dash_passes_rest_through_untouched():
    cmd = build_command("claude", ["--", "--agent", "reviewer", "-p"])
    assert cmd == ["claude", "--agent", "reviewer", "-p"]


def test_unrecognized_flag_passes_through():
    cmd = build_command("kimi", ["--plan", "do the thing"])
    assert cmd == ["kimi", "--plan", "do the thing"]


def test_unknown_tool_raises_usage_error():
    with pytest.raises(UsageError, match="unknown tool"):
        build_command("bogus", [])


@pytest.mark.parametrize("flag", ["-m", "--model", "--add-dir"])
def test_flag_missing_value_raises_usage_error(flag):
    with pytest.raises(UsageError, match="requires a value"):
        build_command("claude", [flag])


# ---------- a session id where a tool name goes ----------

def _run_main(monkeypatch, argv):
    """`cw <argv>` all the way through main(), returning the exit code it
    raised (None for the paths that just return). The id form exists only in
    main()'s dispatch -- build_command knows nothing about it -- so it has to
    be exercised from here."""
    monkeypatch.setattr(sys, "argv", ["cw", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return None


def test_an_id_prefix_resumes_the_session_it_names(monkeypatch):
    """`cw search` prints ids, and the command after finding a session is
    pointing cw straight at it. `cw 327e2ad9-210f-48cd-b` used to answer
    "unknown tool" -- a list of tools for a thing that is not a tool, and no
    mention that ids go through resume."""
    calls = []
    monkeypatch.setattr(sessions, "resolve_any_id",
                        lambda prefix: [("claude", "327e2ad9-210f-48cd-b1f8-65a0cdabd7d8")]
                        if prefix == "327e2ad9-210f-48cd-b" else [])
    monkeypatch.setattr(sessions, "cmd_resume", lambda args: calls.append(args))

    assert _run_main(monkeypatch, ["327e2ad9-210f-48cd-b"]) is None

    assert calls == [["claude", "327e2ad9-210f-48cd-b1f8-65a0cdabd7d8"]]


def test_an_id_prefix_carries_what_follows_it(monkeypatch):
    """The same words after a row number already work (`cw 3 codex` hands
    off, `cw 3 "..."` continues the session); an id gets them for free
    because it routes through the same cmd_resume."""
    calls = []
    monkeypatch.setattr(sessions, "resolve_any_id", lambda prefix: [("claude", "abc123")])
    monkeypatch.setattr(sessions, "cmd_resume", lambda args: calls.append(args))

    _run_main(monkeypatch, ["abc123", "codex"])
    _run_main(monkeypatch, ["abc123", "keep going"])

    assert calls == [["claude", "abc123", "codex"], ["claude", "abc123", "keep going"]]


def test_an_ambiguous_id_prefix_lists_the_matches(monkeypatch, capsys):
    """A bare prefix has no tool to narrow it, so several hits are listed
    rather than guessed at -- the answer `cw resume <tool> <id>` already
    gives, capped, because here the list can be the whole history."""
    hits = [("claude", f"1a{i:036d}") for i in range(12)]
    monkeypatch.setattr(sessions, "resolve_any_id", lambda prefix: hits)
    monkeypatch.setattr(sessions, "cmd_resume",
                        lambda args: pytest.fail("resumed anyway"))

    assert _run_main(monkeypatch, ["1a"]) == 1

    err = capsys.readouterr().err
    assert "ambiguous id '1a' -- 12 sessions start with it:" in err
    assert len(re.findall(r"^  claude 1a", err, re.M)) == 10  # capped
    assert "... and 2 more; narrow it with `cw resume <tool> 1a`" in err


def test_a_word_is_not_an_id(monkeypatch, capsys):
    """Every id in every store is hex or carries a sess_/session_ prefix, so
    an argument with no digit, dash or underscore in it is a word -- and
    `cw session` is a typo of `cw sessions`, which deserves that message
    rather than a list of every kimi session on the box."""
    monkeypatch.setattr(sessions, "resolve_any_id",
                        lambda prefix: pytest.fail("a word was offered to the stores"))

    assert _run_main(monkeypatch, ["session"]) == 1

    assert "unknown tool 'session'" in capsys.readouterr().err


def test_a_tool_name_is_never_read_as_an_id(monkeypatch):
    """Asking six stores whether any id starts with "claude" would tax every
    `cw claude ...` for an answer that is always no."""
    monkeypatch.setattr(sessions, "resolve_any_id",
                        lambda prefix: pytest.fail("a tool name was offered to the stores"))
    ran = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: ran.append(argv))

    _run_main(monkeypatch, ["claude", "-p", "hi"])

    assert ran == [["claude", "-p", "hi"]]


def test_an_id_that_matches_nothing_is_still_an_unknown_command(monkeypatch, capsys):
    """No hit falls through to the tool error, so a mistyped command keeps
    the message that names the real ones."""
    monkeypatch.setattr(sessions, "resolve_any_id", lambda prefix: [])

    assert _run_main(monkeypatch, ["deadbeef99"]) == 1

    assert "unknown tool 'deadbeef99'" in capsys.readouterr().err


@pytest.mark.parametrize("env, expected", [
    (None, "15"), ("", "15"), ("30", "30"), (" 40 ", "40"), ("ALL", "all"),
    ("0", "15"), ("-5", "15"), ("lots", "15"),
])
def test_bare_cw_row_count_follows_cw_limit(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("CW_LIMIT", raising=False)
    else:
        monkeypatch.setenv("CW_LIMIT", env)
    seen = []
    monkeypatch.setattr(cli.sessions, "cmd_list", lambda args: seen.append(args))
    monkeypatch.setattr(sys, "argv", ["cw"])
    cli.main()
    assert seen == [["--limit", expected]]
