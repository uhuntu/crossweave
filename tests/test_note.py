"""cw note: a session summarized by the judge into an Obsidian vault.

The judge and the digest are stubbed -- what is pinned is everything around
them: where the note lands, what its frontmatter says, that noting a
session twice rewrites one note instead of adding a second, and that a
missing vault or an unusable judge writes nothing.
"""

import os
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
    return sorted(str(p.relative_to(vault)) for p in vault.rglob("*.md"))


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
    assert "noted: board/" in capsys.readouterr().out


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
