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


# ---------- the vault's conventions (_schema.md) and log (_log.md) ----------

def test_the_conventions_go_into_every_prompt_just_before_the_reply_line(vault):
    v, prompts = vault
    (v / note.SCHEMA_NAME).write_text("---\nkind: cw-schema\n---\n# x\n- 用 DevGetter 这个写法\n",
                                      encoding="utf-8")
    wiki.build_project(str(v), "board", "step")
    prompt = prompts[-1]
    assert "- 用 DevGetter 这个写法" in prompt
    assert prompt.index("用 DevGetter") < prompt.index("Reply with the page only")
    assert "kind: cw-schema" not in prompt


def test_the_default_conventions_apply_and_land_as_a_file_on_first_success(vault):
    v, prompts = vault
    assert not (v / note.SCHEMA_NAME).exists()
    wiki.build_project(str(v), "board", "step")
    assert "同一个东西只用一种写法" in prompts[-1]
    assert (v / note.SCHEMA_NAME).read_text(encoding="utf-8") == note.DEFAULT_SCHEMA


def test_nothing_is_written_when_the_judge_fails(vault, monkeypatch):
    v, _ = vault
    def dead(*a):
        raise search.JudgeError(1)
    monkeypatch.setattr(search, "call_judge_with_fallback", dead)
    assert wiki.build_project(str(v), "board", "step") is None
    assert not (v / note.SCHEMA_NAME).exists() and not (v / note.LOG_NAME).exists()


def test_every_build_is_logged_in_one_greppable_shape(vault):
    v, _ = vault
    wiki.build_project(str(v), "board", "step")
    lines = [line for line in (v / note.LOG_NAME).read_text(encoding="utf-8").splitlines()
             if line.startswith("## [")]
    assert len(lines) == 1
    assert lines[0].endswith("] project | board | built from 2 notes")


# ---------- the page is edited as notes arrive, not re-derived ----------

UPDATE_REPLY = """## 现状
- 枚举已修好，回归已通过。[N1][N3]

## 还没解决
- 发布。[N1]
"""


def test_a_new_note_updates_the_page_rather_than_rebuilding_it(vault, monkeypatch):
    v, prompts = vault
    wiki.build_project(str(v), "board", "step")
    session_note(v / "board", "2099-01-01 回归通过", "s9", "2099-01-01T10:00",
                 "2099-01-01T11:00", "## 做了什么\n- 回归通过\n## 还剩什么\n- 发布")
    monkeypatch.setattr(search, "call_judge_with_fallback",
                        lambda prompt, j, e, label: prompts.append(prompt) or (_Proc(UPDATE_REPLY), j))
    path = wiki.build_project(str(v), "board", "step")
    prompt = prompts[-1]
    assert "Update the page" in prompt
    # the current page goes in with its citations renumbered: the new note
    # is N1 now, so the old N1/N2 are N2/N3
    assert "- 枚举已修好，回归测试还没跑。[N2][N3]" in prompt
    assert "回归通过" in prompt          # the new note, in full
    assert "修好枚举" not in prompt      # the old ones only by title
    text = open(path, encoding="utf-8").read()
    assert "[[2099-01-01 回归通过|N1]]" in text and "[[2026-11-01 USB 枚举|N3]]" in text
    assert 'read: ["2099-01-01 回归通过", "2026-11-05 回归", "2026-11-01 USB 枚举"]' in text
    log = (v / note.LOG_NAME).read_text(encoding="utf-8")
    assert "] project | board | updated from 2099-01-01 回归通过" in log


def test_an_up_to_date_page_costs_no_judge_call(vault):
    v, prompts = vault
    wiki.build_project(str(v), "board", "step")
    n = len(prompts)
    assert wiki.build_project(str(v), "board", "step") == str(v / "board" / "_project.md")
    assert len(prompts) == n


def test_a_note_gone_from_the_folder_or_full_means_a_rebuild(vault):
    v, prompts = vault
    wiki.build_project(str(v), "board", "step")
    wiki.build_project(str(v), "board", "step", full=True)
    assert "Write the project's overview page" in prompts[-1]
    (v / "board" / "2026-11-01 USB 枚举.md").unlink()
    session_note(v / "board", "2099-01-01 later", "s9", "2099-01-01T10:00", "2099-01-01T11:00", "- x")
    wiki.build_project(str(v), "board", "step")
    assert "Write the project's overview page" in prompts[-1]


def test_a_note_the_page_found_nothing_in_does_not_keep_it_stale(vault, monkeypatch):
    v, prompts = vault
    only_n1 = "## 现状\n- 只引用了一条。[N1]\n"
    monkeypatch.setattr(search, "call_judge_with_fallback",
                        lambda prompt, j, e, label: prompts.append(prompt) or (_Proc(only_n1), j))
    wiki.build_project(str(v), "board", "step")
    assert wiki.stale_projects(str(v)) == []


def test_projects_full_rebuilds_every_page_even_when_current(vault, capsys):
    v, prompts = vault
    assert wiki.cmd_wiki(["--projects", "-y"]) == 0
    assert wiki.cmd_wiki(["--projects", "-y"]) == 0
    assert len(prompts) == 1
    assert wiki.cmd_wiki(["--projects", "--full", "-y"]) == 0
    assert len(prompts) == 2
    assert "Write the project's overview page" in prompts[-1]
