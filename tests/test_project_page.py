"""A project's overview page: `cw wiki --project` reads every note in the
project's vault folder and writes `_project.md` beside `_index.md`."""
import os
import sys

import pytest

from crossweave import autoexport, cli, note, search, wiki


class _Proc:
    def __init__(self, stdout):
        self.stdout, self.returncode, self.stderr = stdout, 0, ""


def session_note(folder, name, sid, updated, noted, body, title=None):
    (folder / f"{name}.md").write_text(f"""---
title: "{title or name}"
tool: "claude"
session: "{sid}"
cwd: "/work/board"
project: "board"
updated: {updated}
noted: {noted}
resume: "cw resume claude {sid}"
show: "cw show claude {sid}"
tags: [ai-session, claude]
---
# {title or name}

原文 / Transcript: `cw show claude {sid}`

{body}
""", encoding="utf-8")


REPLY = """## 概要
- board 是 USB 灯条固件。[N1]

## 现状
- 枚举已修好，回归测试还没跑。[N1][N2]

## 还没解决
- 回归测试。[N2]
"""


@pytest.fixture
def vault(monkeypatch, tmp_path):
    v = tmp_path / "vault"
    board = v / "board"
    board.mkdir(parents=True)
    session_note(board, "2026-11-01 USB 枚举", "s1", "2026-11-01T10:00", "2026-11-01T11:00",
                 "## 做了什么\n- 修好枚举")
    session_note(board, "2026-11-05 回归", "s2", "2026-11-05T10:00", "2026-11-05T11:00",
                 "## 还剩什么\n- 跑回归")
    (v / "_home").mkdir()
    session_note(v / "_home", "2026-11-05 杂事", "s3", "2026-11-05T10:00", "2026-11-05T11:00", "- x")
    monkeypatch.setenv("CW_VAULT", str(v))
    prompts = []
    monkeypatch.setattr(search, "call_judge_with_fallback",
                        lambda prompt, j, e, label: prompts.append(prompt) or (_Proc(REPLY), j))
    return v, prompts


def test_the_page_is_built_from_every_note_and_links_its_sources(vault):
    v, prompts = vault
    path = wiki.build_project(str(v), "board", "step")
    assert path == str(v / "board" / "_project.md")
    text = open(path, encoding="utf-8").read()
    assert "kind: cw-project" in text
    # newest first: N1 is the 11-05 note
    assert "[[2026-11-05 回归|N1]]" in text and "[[2026-11-01 USB 枚举|N2]]" in text
    assert "## 来源 / Notes" in text and wiki.OWN_MARKER in text
    assert "原文 / Transcript" not in prompts[0]  # the pointer is not knowledge
    assert "跑回归" in prompts[0] and "修好枚举" in prompts[0]
    index = (v / "board" / note.INDEX_NAME).read_text(encoding="utf-8")
    assert "[[_project|项目概览 / Overview]]" in index


def test_the_page_is_not_taken_for_a_note(vault):
    v, _ = vault
    wiki.build_project(str(v), "board", "step")
    stems = [s for s, _m, _b in note._project_notes(str(v / "board"))]
    assert "_project" not in stems
    assert "_project" not in {os.path.splitext(os.path.basename(p))[0]
                              for _n, _b, p in note.session_notes(str(v)).values()}


def test_your_own_part_survives_a_rebuild(vault):
    v, _ = vault
    path = wiki.build_project(str(v), "board", "step")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("- 记得换天线\n")
    wiki.build_project(str(v), "board", "step")
    assert "- 记得换天线" in open(path, encoding="utf-8").read()


def test_a_page_goes_stale_when_a_note_is_written_or_added(vault):
    v, _ = vault
    assert wiki.stale_projects(str(v)) == ["board"]  # missing; _home never gets one
    wiki.build_project(str(v), "board", "step")
    assert wiki.stale_projects(str(v)) == []
    session_note(v / "board", "2099-01-01 later", "s9", "2099-01-01T10:00", "2099-01-01T11:00", "- new")
    assert wiki.stale_projects(str(v)) == ["board"]


def test_cw_context_starts_from_the_overview(vault, monkeypatch, capsys):
    v, _ = vault
    wiki.build_project(str(v), "board", "step")
    work = v.parent / "board"
    work.mkdir()
    monkeypatch.chdir(work)
    assert note.cmd_context([]) == 0
    out = capsys.readouterr().out
    assert "## Project overview (all its notes, built " in out
    assert "### 现状\n- 枚举已修好，回归测试还没跑。[N1][N2]" in out
    assert "Built by" not in out and "来源 / Notes" not in out
    assert out.index("Project overview") < out.index("回归")


def test_cli_builds_the_current_directorys_project(vault, monkeypatch, capsys):
    v, _ = vault
    work = v.parent / "board"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(sys, "argv", ["cw", "wiki", "--project"])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code in (0, None)
    assert (v / "board" / "_project.md").exists()


def test_cli_projects_rebuilds_only_what_is_behind(vault, capsys):
    v, prompts = vault
    assert wiki.cmd_wiki(["--projects", "-y"]) == 0
    assert len(prompts) == 1
    assert wiki.cmd_wiki(["--projects", "-y"]) == 0
    assert len(prompts) == 1
    assert "Every project page is current." in capsys.readouterr().out


def test_no_project_page_for_a_folder_that_is_not_one(vault, capsys):
    v, _ = vault
    assert wiki.cmd_wiki(["--project", "_home"]) == 1
    assert "no project folder" in capsys.readouterr().err


def test_the_daily_run_refreshes_stale_project_pages(vault, monkeypatch):
    v, prompts = vault
    autoexport._refresh_project_pages()
    assert (v / "board" / "_project.md").exists()
    assert not (v / "_home" / "_project.md").exists()
