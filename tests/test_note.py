"""cw note: a session summarized by the judge into an Obsidian vault.

The judge and the digest are stubbed -- what is pinned is everything around
them: where the note lands, what its frontmatter says, that noting a
session twice rewrites one note instead of adding a second, and that a
missing vault or an unusable judge writes nothing.
"""

import os
import re
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from crossweave import cli, note, rename, search, sessions

NOTE = "## 做了什么\n- 修好了 USB 枚举\n\n## 为什么\n- 握手超时\n\n## 还剩什么\n- 回归测试"


def _record(sid="sess_abc", title="USB 枚举握手排查", cwd="/w/board"):
    return {"tool": "zcode", "id": sid, "title": title, "cwd": cwd,
            "ts": time.time() - 3600, "path": None}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """One zcode session, row 1 of the last listing, a vault, a judge."""
    records = [_record()]

    def gather(show_all=False):
        return [dict(r) for r in records]

    monkeypatch.setattr(sessions, "zcode_light_records", gather)
    monkeypatch.setattr(sessions, "zcode_resolve",
                        lambda p: [r["id"] for r in records if r["id"].startswith(p)])
    monkeypatch.setitem(sessions.ID_RESOLVERS, "zcode", sessions.zcode_resolve)
    sessions.write_list_cache([{"tool": "zcode", "id": "sess_abc"}])
    monkeypatch.setattr(rename, "_digest_for", lambda r: "User asked: fix usb")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("CW_VAULT", str(vault))
    w = SimpleNamespace(reply=NOTE, vault=vault, records=records, calls=[])

    def judge(name, prompt):
        w.calls.append(prompt)
        return subprocess.CompletedProcess([name], 0, stdout=w.reply, stderr="")

    monkeypatch.setattr(search, "_call_judge", judge)
    return w


def _notes(vault):
    # as_posix: the assertions spell folders with "/", Windows paths with "\\"
    return sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*.md")
                  if p.name != note.INDEX_NAME)


def test_a_note_lands_in_the_project_folder_with_frontmatter(world, capsys):
    assert note.cmd_note(["1"]) == 0

    files = _notes(world.vault)
    assert len(files) == 1
    assert files[0].startswith("board/") and files[0].endswith(" USB 枚举握手排查.md")
    text = (world.vault / files[0]).read_text(encoding="utf-8")
    assert text.startswith("---\ntitle: \"USB 枚举握手排查\"\ntool: \"zcode\"\n")
    assert 'session: "sess_abc"' in text
    assert 'resume: "cw resume zcode sess_abc"' in text
    assert "tags: [ai-session, zcode]" in text
    assert text.rstrip().endswith("- 回归测试")
    assert "# USB 枚举握手排查\n\n## 做了什么" in text
    assert f"noted: board{os.sep}" in capsys.readouterr().out  # a native path


def test_noting_again_rewrites_the_same_note(world):
    assert note.cmd_note(["1"]) == 0
    world.records[0]["title"] = "a new title"
    world.reply = "## Done\n- something else"
    assert note.cmd_note(["1"]) == 0

    files = _notes(world.vault)
    assert len(files) == 1, files  # found by its session line, not its name
    text = (world.vault / files[0]).read_text(encoding="utf-8")
    assert "something else" in text and "a new title" in text


def test_an_id_prefix_works_where_a_row_does(world):
    assert note.cmd_note(["sess_a"]) == 0
    assert len(_notes(world.vault)) == 1


def test_the_row_may_lead_the_verb(world, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cw", "1", "note"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert len(_notes(world.vault)) == 1


def test_no_vault_writes_nothing(world, monkeypatch, capsys):
    monkeypatch.delenv("CW_VAULT")
    assert note.cmd_note(["1"]) == 1
    assert "CW_VAULT" in capsys.readouterr().err
    assert world.calls == []  # not even the judge is asked


def test_a_missing_vault_is_not_created(world, tmp_path, capsys):
    assert note.cmd_note(["1", "--vault", str(tmp_path / "typo")]) == 1
    assert not (tmp_path / "typo").exists()
    assert "does not exist" in capsys.readouterr().err


def test_a_prose_reply_is_asked_again_then_refused(world, capsys):
    world.reply = "Sure! Here is a summary of the session."
    assert note.cmd_note(["1"]) == 1
    assert len(world.calls) == 2
    assert _notes(world.vault) == []
    assert "no note" in capsys.readouterr().err


def test_a_fenced_reply_keeps_only_the_note(world):
    world.reply = "Here you go:\n```markdown\n## Done\n- x\n```\n"
    assert note.cmd_note(["1"]) == 0
    text = (world.vault / _notes(world.vault)[0]).read_text(encoding="utf-8")
    assert text.endswith("## Done\n- x\n")


def test_a_dead_judge_writes_nothing(world, monkeypatch, capsys):
    def dies(name, prompt):
        raise search.JudgeError(124)

    monkeypatch.setattr(search, "_call_judge", dies)
    assert note.cmd_note(["1"]) == 1
    assert _notes(world.vault) == []
    assert "judge" in capsys.readouterr().err


def test_a_session_with_no_cwd_goes_to_unsorted(world):
    world.records[0]["cwd"] = None
    assert note.cmd_note(["1"]) == 0
    assert _notes(world.vault)[0].startswith(note.NO_PROJECT + "/")


def test_unsafe_title_characters_are_dropped_from_the_file_name(world):
    world.records[0]["title"] = "fix [[a/b]] #tag: why?"
    assert note.cmd_note(["1"]) == 0
    name = os.path.basename(_notes(world.vault)[0])
    assert name.endswith(" fix a b tag why.md"), name


# ---------- cw context ----------

def test_context_prints_the_projects_notes_newest_first(world, monkeypatch, capsys):
    assert note.cmd_note(["1"]) == 0
    world.records.append(_record("sess_new", "later work"))
    world.records[-1]["ts"] = time.time()
    world.reply = "## Done\n- the newer thing"
    assert note.cmd_note(["sess_new"]) == 0
    capsys.readouterr()

    assert note.cmd_context(["/w/board"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Notes from earlier sessions in board\n")
    assert out.index("## later work (zcode,") < out.index("## USB 枚举握手排查 (zcode,")
    assert "### Done\n- the newer thing" in out  # sections sit one level down
    assert "Resume: `cw resume zcode sess_new`" in out
    assert "title:" not in out  # frontmatter is not repeated

    assert note.cmd_context(["/w/board", "--limit", "1"]) == 0
    assert "USB" not in capsys.readouterr().out


def test_context_defaults_to_the_current_directory(world, monkeypatch, tmp_path, capsys):
    assert note.cmd_note(["1"]) == 0
    here = tmp_path / "board"
    here.mkdir()
    monkeypatch.chdir(here)
    capsys.readouterr()
    assert note.cmd_context([]) == 0
    assert "USB 枚举握手排查" in capsys.readouterr().out


def test_context_with_no_notes_says_how_to_make_one(world, capsys):
    assert note.cmd_context(["/w/elsewhere"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing to splice into a prompt
    assert "cw note" in captured.err


def test_context_ignores_markdown_cw_did_not_write(world, capsys):
    (world.vault / "board").mkdir()
    (world.vault / "board" / "mine.md").write_text("# my own thoughts\n", encoding="utf-8")
    assert note.cmd_context(["/w/board"]) == 1


def test_a_session_in_the_home_directory_goes_to_home(world, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "someone"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "someone"))  # what ~ is on Windows
    world.records[0]["cwd"] = str(tmp_path / "someone") + "/"
    assert note.cmd_note(["1"]) == 0
    files = _notes(world.vault)
    assert files[0].startswith(note.HOME_PROJECT + "/"), files
    assert 'project: "_home"' in (world.vault / files[0]).read_text(encoding="utf-8")


# ---------- the listing's ✎ ----------

def _listing(monkeypatch, records, capsys):
    rows = [("zcode", r["id"], "1h ago", r["id"][:8], r.get("cwd") or "?", r["title"])
            for r in records]
    monkeypatch.setattr(sessions, "session_turns", lambda r: 3)
    monkeypatch.setattr(sessions, "session_last_message", lambda r: None)
    capsys.readouterr()
    sessions.render_rows(rows, write_cache=False, sources={r["id"]: r for r in records})
    return capsys.readouterr().out


def test_the_listing_marks_noted_sessions(world, monkeypatch, capsys):
    world.records.append(_record("sess_other", "never noted"))
    assert note.cmd_note(["1"]) == 0

    out = _listing(monkeypatch, world.records, capsys)
    assert "USB 枚举握手排查 ✎" in out
    assert "never noted ✎" not in out and "never noted" in out
    # the mark trails the title, so every title still starts in one column
    lines = [l for l in out.splitlines() if "USB" in l or "never noted" in l]
    header = next(l for l in out.splitlines() if "TITLE" in l)
    starts = {sessions._display_width(l[:l.index("USB" if "USB" in l else "never")])
              for l in lines}
    assert starts == {sessions._display_width(header[:header.index("TITLE")])}, out


def test_a_session_that_moved_on_since_its_note_is_marked_stale(world, monkeypatch, capsys):
    assert note.cmd_note(["1"]) == 0
    world.records[0]["ts"] = time.time() + 3600  # new turns after the note

    out = _listing(monkeypatch, world.records, capsys)
    assert "USB 枚举握手排查 ✎+" in out


def test_no_vault_no_marks(world, monkeypatch, capsys):
    assert note.cmd_note(["1"]) == 0
    monkeypatch.delenv("CW_VAULT")
    assert "✎" not in _listing(monkeypatch, world.records, capsys)


# ---------- cw note --all ----------

@pytest.fixture
def many(world, monkeypatch):
    """Four sessions: one long, one short, one live, one old."""
    now = time.time()
    world.records[:] = [
        dict(_record("sess_long", "long work"), ts=now - 3600),
        dict(_record("sess_short", "hi"), ts=now - 3600),
        dict(_record("sess_live", "typing now"), ts=now - 30),
        dict(_record("sess_old", "last month"), ts=now - 30 * 86400),
    ]
    turns = {"sess_long": 20, "sess_short": 1, "sess_live": 9, "sess_old": 12}
    monkeypatch.setattr(sessions, "session_turns", lambda r: turns[r["id"]])
    for name in ("claude", "codex", "kimi", "step", "codebuddy", "workbuddy"):
        monkeypatch.setattr(sessions, f"{name}_light_records",
                            lambda show_all=False: [])
    return world


def _noted_ids(vault):
    return set(note.noted_index())


def test_all_notes_long_settled_sessions_only(many, capsys):
    assert note.cmd_note(["--all", "-y"]) == 0
    assert _noted_ids(many.vault) == {"sess_long", "sess_old"}
    err = capsys.readouterr().err
    assert "2 sessions to note" in err
    assert "1 live" in err and "1 under 3 turns" in err


def test_all_checks_the_vault_it_writes_to(many, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert note.cmd_note(["--all", "-y"]) == 0
    assert note.cmd_note(["--all", "-y", "--vault", str(other)]) == 0
    assert set(note.noted_index(str(other))) == {"sess_long", "sess_old"}


def test_all_skips_what_is_already_noted_until_it_moves_on(many, capsys):
    assert note.cmd_note(["--all", "-y"]) == 0
    assert _noted_ids(many.vault) == {"sess_long", "sess_old"}
    calls = len(many.calls)

    assert note.cmd_note(["--all", "-y"]) == 0
    assert len(many.calls) == calls  # nothing new to say
    assert "0 sessions to note" in capsys.readouterr().err

    # the long session's note was written two hours ago; it has moved on since
    path = next(p for p in many.vault.rglob("*.md")
                if p.name != note.INDEX_NAME and "sess_long" in p.read_text(encoding="utf-8"))
    two_hours_ago = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() - 7200))
    path.write_text(re.sub(r"(?m)^noted: .*$", f"noted: {two_hours_ago}",
                           path.read_text(encoding="utf-8")), encoding="utf-8")
    assert note.cmd_note(["--all", "-y"]) == 0
    assert len(many.calls) == calls + 1  # only the one that moved on


def test_since_narrows_the_window(many):
    assert note.cmd_note(["--since", "7d", "-y"]) == 0  # --all implied
    assert _noted_ids(many.vault) == {"sess_long"}


def test_all_without_a_yes_asks_and_a_pipe_means_no(many, capsys):
    assert note.cmd_note(["--all"]) == 1  # stdin is not a terminal under pytest
    assert many.calls == []
    assert "-y" in capsys.readouterr().err


def test_all_and_rows_do_not_mix(many):
    assert note.cmd_note(["--all", "1"]) == 2
    assert note.cmd_note(["--since", "soon"]) == 2


# ---------- the project index ----------

def _index(vault, project="board"):
    return (vault / project / note.INDEX_NAME).read_text(encoding="utf-8")


def test_noting_a_session_writes_its_projects_index(world):
    assert note.cmd_note(["1"]) == 0
    text = _index(world.vault)
    stem = os.path.splitext(os.path.basename(_notes(world.vault)[0]))[0]
    assert text.startswith('---\nproject: "board"\n')
    assert "## Open\n\nFrom [[" + stem + "]]:\n- 回归测试" in text
    assert f"| zcode | [[{stem}]] | `cw resume zcode sess_abc` |" in text


def test_the_index_is_not_a_note(world, capsys):
    assert note.cmd_note(["1"]) == 0
    assert set(note.noted_index()) == {"sess_abc"}
    capsys.readouterr()
    assert note.cmd_context(["/w/board"]) == 0
    assert capsys.readouterr().out.count("## ") == 1 + 3  # one note, its 3 sections


def test_open_items_come_from_the_newest_notes_only(world):
    now = time.time()
    world.records[:] = [dict(_record(f"s{i}", f"task {i}"), ts=now - 86400 * (i + 1))
                        for i in range(note.INDEX_OPEN_NOTES + 2)]
    sessions.write_list_cache([{"tool": "zcode", "id": r["id"]} for r in world.records])
    for i in range(len(world.records)):
        world.reply = f"## Done\n- did {i}\n\n## Open\n- left {i}"
        assert note.cmd_note([str(i + 1)]) == 0
    text = _index(world.vault)
    assert text.index("left 0") < text.index("left 1")  # newest first
    assert f"left {note.INDEX_OPEN_NOTES - 1}" in text
    assert f"left {note.INDEX_OPEN_NOTES}" not in text
    assert text.count("| zcode |") == len(world.records)  # the table has them all


def test_index_rebuilds_every_project_without_the_judge(world, capsys):
    assert note.cmd_note(["1"]) == 0
    (world.vault / "board" / note.INDEX_NAME).unlink()
    (world.vault / "mine").mkdir()  # the user's own folder, no cw notes
    calls = len(world.calls)
    assert note.cmd_note(["--index"]) == 0
    assert len(world.calls) == calls
    assert (world.vault / "board" / note.INDEX_NAME).exists()
    assert not (world.vault / "mine" / note.INDEX_NAME).exists()
    assert "1 project index rebuilt" in capsys.readouterr().err


def test_index_and_rows_do_not_mix(world):
    assert note.cmd_note(["--index", "1"]) == 2


def test_a_pipe_in_a_title_stays_inside_its_cell():
    assert note._cell("a | b\nc") == "a \\| b c"


def test_open_items_read_either_language():
    assert note.open_items("## 还剩什么\n- 甲\n* 乙\n## 为什么\n- 丙") == ["甲", "乙"]
    assert note.open_items("## Done\n- x\n## Open\n- y") == ["y"]
