"""`cw <tool>` starts a fresh session with the project's vault notes as
background: appended to the system prompt, never sent as a message."""
import json
import sys

import pytest

from crossweave import cli, note

NOTE = """---
title: "USB 枚举"
tool: "zcode"
session: "sess_abc"
updated: 2026-10-27T10:00
resume: "cw resume zcode sess_abc"
---
# USB 枚举

## 还剩什么
- 回归测试
"""


@pytest.fixture
def project(monkeypatch, tmp_path):
    """A vault with one note for the project `board`, and cwd inside it."""
    vault = tmp_path / "vault"
    (vault / "board").mkdir(parents=True)
    (vault / "board" / "2026-10-27 USB 枚举.md").write_text(NOTE, encoding="utf-8")
    work = tmp_path / "board"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("CW_VAULT", str(vault))
    monkeypatch.delenv("CW_CONTEXT", raising=False)
    ran = []
    monkeypatch.setattr(cli.sessions, "exec_or_die", ran.append)
    return ran


def launch(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["cw", *argv])
    cli.main()


def test_claude_gets_the_notes_appended_to_its_system_prompt(project, monkeypatch, capsys):
    launch(monkeypatch, "claude")
    cmd = project[0]
    assert cmd[:2] == ["claude", "--append-system-prompt"]
    assert "# Notes from earlier sessions in board" in cmd[2]
    assert "回归测试" in cmd[2]
    assert "1 note from board attached" in capsys.readouterr().err


def test_a_prompt_still_goes_last(project, monkeypatch):
    launch(monkeypatch, "claude", "-m", "opus", "fix the usb test")
    cmd = project[0]
    assert cmd[1] == "--append-system-prompt"
    assert cmd[-3:] == ["--model", "opus", "fix the usb test"]


def test_codex_gets_them_as_developer_instructions(project, monkeypatch):
    launch(monkeypatch, "codex")
    cmd = project[0]
    assert cmd[:2] == ["codex", "-c"]
    key, _, value = cmd[2].partition("=")
    assert key == "developer_instructions"
    assert "回归测试" in json.loads(value)


@pytest.mark.parametrize("tool", ["step", "codebuddy"])
def test_step_and_codebuddy_append_like_claude(project, monkeypatch, tool):
    launch(monkeypatch, tool)
    assert project[0][1] == "--append-system-prompt"


@pytest.mark.parametrize("argv", [
    ["claude", "-p", "summarize"],      # a script's one-shot run
    ["claude", "-c"],                   # a continued session knows already
    ["claude", "--no-context"],         # asked not to
    ["claude", "doctor"],               # a subcommand, not a session
    ["claude", "--append-system-prompt", "mine"],  # the user's own wins
    ["kimi"],                           # no way to take it
])
def test_no_notes_where_they_do_not_belong(project, monkeypatch, argv):
    launch(monkeypatch, *argv)
    cmd = project[0]
    assert not any("Notes from earlier sessions" in a for a in cmd)
    assert "--no-context" not in cmd


def test_cw_context_off_turns_it_off(project, monkeypatch):
    monkeypatch.setenv("CW_CONTEXT", "off")
    launch(monkeypatch, "claude")
    assert project[0] == ["claude"]


def test_no_vault_or_no_notes_changes_nothing(project, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CW_VAULT")
    launch(monkeypatch, "claude")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("CW_VAULT", str(tmp_path / "vault"))
    monkeypatch.chdir(other)
    launch(monkeypatch, "claude")
    assert project == [["claude"], ["claude"]]
    assert capsys.readouterr().err == ""


def test_no_context_after_a_double_dash_belongs_to_the_tool(project, monkeypatch):
    launch(monkeypatch, "claude", "--", "--no-context")
    cmd = project[0]
    assert cmd[1] == "--append-system-prompt"
    assert cmd[-1] == "--no-context"


def test_the_project_index_is_not_attached(project, monkeypatch, tmp_path):
    note.rebuild_indexes(str(tmp_path / "vault"))
    launch(monkeypatch, "claude")
    assert project[0][2].count("## ") == 2  # the one note and its section
