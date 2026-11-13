"""`cw note --sort-home`: notes of sessions started in ~ are moved into the
projects they are about, and stay there when they are rewritten."""
import json

import pytest

from crossweave import note, search


class _Proc:
    def __init__(self, stdout):
        self.stdout, self.returncode, self.stderr = stdout, 0, ""


def write_note(folder, name, sid, title, body="## 做了什么\n- x\n"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(f"""---
title: {json.dumps(title, ensure_ascii=False)}
tool: "claude"
session: "{sid}"
cwd: "/home/u"
project: "_home"
updated: 2026-11-01T10:00
noted: 2026-11-01T11:00
resume: "cw resume claude {sid}"
tags: [ai-session, claude]
---
# {title}

{body}""", encoding="utf-8")


@pytest.fixture
def vault(monkeypatch, tmp_path):
    v = tmp_path / "vault"
    home = v / note.HOME_PROJECT
    write_note(home, "2026-11-01 蓝牙流控", "s1", "Rock 5B 蓝牙流控")
    write_note(home, "2026-11-02 LPO", "s2", "Rock 5B LPO 时钟")
    write_note(home, "2026-11-03 签名", "s3", "DevGetter 签名")
    write_note(home, "2026-11-04 apt", "s4", "apt 锁")
    write_note(v / "ApiService", "2026-10-01 上架", "s9", "DevGetter 上架")
    monkeypatch.setenv("CW_VAULT", str(v))
    replies, prompts = [], []
    monkeypatch.setattr(search, "call_judge_with_fallback",
                        lambda prompt, j, e, label: prompts.append(prompt) or (_Proc(replies.pop(0)), j))
    return v, replies, prompts


REPLY = "H1 -> new: Rock 5B\nH2 -> new: Rock 5B\nH3 -> apiservice\nH4 -> keep\n"


def test_notes_move_to_their_projects_and_the_rest_is_marked(vault, capsys):
    v, replies, prompts = vault
    replies.append(REPLY)
    moved = note.sort_home(str(v), "step", yes=True)
    assert len(moved) == 3
    assert sorted(p.name for p in (v / "Rock 5B").glob("*.md") if p.name != note.INDEX_NAME) == [
        "2026-11-01 蓝牙流控.md", "2026-11-02 LPO.md"]
    # an existing project is matched whatever the case the judge wrote it in
    assert (v / "ApiService" / "2026-11-03 签名.md").exists()
    text = (v / "Rock 5B" / "2026-11-01 蓝牙流控.md").read_text(encoding="utf-8")
    assert 'project: "Rock 5B"' in text and 'project: "_home"' not in text
    kept = (v / note.HOME_PROJECT / "2026-11-04 apt.md").read_text(encoding="utf-8")
    assert "home_sorted: true" in kept
    assert (v / "Rock 5B" / note.INDEX_NAME).exists()
    assert "] sort | _home | 3 moved, 1 kept -> ApiService, Rock 5B" in (v / note.LOG_NAME).read_text(encoding="utf-8")
    # the judge saw the existing project and the notes
    assert "- ApiService: DevGetter 上架" in prompts[0] and "[H3] DevGetter 签名" in prompts[0]
    # nothing left to ask about
    assert note.unsorted_home_notes(str(v)) == []


def test_dry_run_shows_the_plan_and_moves_nothing(vault, capsys):
    v, replies, _ = vault
    replies.append(REPLY)
    assert note.sort_home(str(v), "step", dry_run=True) == []
    out = capsys.readouterr().out
    assert "Rock 5B (new project):" in out and "ApiService:" in out and "stays in _home:" in out
    assert len(list((v / note.HOME_PROJECT).glob("*.md"))) == 4


def test_moving_a_note_preserves_all_existing_names(vault):
    v, replies, _ = vault
    folder = v / "Rock 5B"
    folder.mkdir()
    names = ["2026-11-01 蓝牙流控.md", "2026-11-01 蓝牙流控 (from _home).md",
             "2026-11-01 蓝牙流控 (from _home 2).md"]
    for name in names:
        (folder / name).write_text("Existing handwritten content", encoding="utf-8")
    replies.append(REPLY)

    moved = note.sort_home(str(v), "step", yes=True)

    for name in names:
        assert (folder / name).read_text(encoding="utf-8") == "Existing handwritten content"
    destination = folder / "2026-11-01 蓝牙流控 (from _home 3).md"
    assert (str(v / note.HOME_PROJECT / names[0]), str(destination)) in moved
    assert 'session: "s1"' in destination.read_text(encoding="utf-8")


def test_without_a_yes_nothing_moves(vault, capsys):
    v, replies, _ = vault
    replies.append(REPLY)
    assert note.sort_home(str(v), "step") == []  # no terminal to say yes
    assert "nothing moved" in capsys.readouterr().err
    assert not (v / "Rock 5B").exists()


def test_a_moved_note_is_rewritten_where_it_now_lives(vault):
    v, replies, _ = vault
    replies.append(REPLY)
    note.sort_home(str(v), "step", yes=True)
    r = {"tool": "claude", "id": "s1", "ts": 0}
    path = note._write_note(r, str(v), "Rock 5B 蓝牙流控", "/home/u", "## 做了什么\n- 新进展\n")
    assert path == str(v / "Rock 5B" / "2026-11-01 蓝牙流控.md")
    text = open(path, encoding="utf-8").read()
    assert "新进展" in text and 'project: "Rock 5B"' in text
    assert not (v / note.HOME_PROJECT / "2026-11-01 蓝牙流控.md").exists()


def test_a_name_the_judge_makes_up_is_made_safe_and_underscores_are_refused():
    got = note._parse_sort("H1 -> new: Rock/5B\nH2 -> _wiki\nH3 → keep\n- H4: CRA\nnoise", 4, {"CRA": []})
    assert got == {1: "Rock 5B", 2: None, 3: None, 4: "CRA"}


def test_cli(vault, capsys):
    v, replies, _ = vault
    replies.append(REPLY)
    assert note.cmd_note(["--sort-home", "-y"]) == 0
    assert (v / "Rock 5B").is_dir()
    assert note.cmd_note(["--dry-run"]) == 2
