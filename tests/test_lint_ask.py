"""`cw wiki --lint` checks the vault and writes _lint.md; `cw ask` answers
from the vault and can file the answer back."""
import os

import pytest

from crossweave import ask, lint, note, search, wiki


class _Proc:
    def __init__(self, stdout):
        self.stdout, self.returncode, self.stderr = stdout, 0, ""


def session_note(folder, name, sid, body, updated="2026-11-01T10:00", noted="2026-11-01T11:00"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(f"""---
title: "{name}"
tool: "claude"
session: "{sid}"
cwd: "/work/board"
project: "board"
updated: {updated}
noted: {noted}
resume: "cw resume claude {sid}"
tags: [ai-session, claude]
---
# {name}

{body}
""", encoding="utf-8")


@pytest.fixture
def vault(monkeypatch, tmp_path):
    v = tmp_path / "vault"
    session_note(v / "board", "2026-11-01 USB 枚举", "s1",
                 "## 做了什么\n- 修好枚举，命令 `fastboot flash boot boot.img`\n## 还剩什么\n- 跑回归")
    session_note(v / "board", "2026-11-05 回归", "s2", "## 做了什么\n- 回归通过\n## 还剩什么\n- 发布",
                 updated="2026-11-05T10:00", noted="2026-11-05T11:00")
    monkeypatch.setenv("CW_VAULT", str(v))
    monkeypatch.setattr(search, "gather_candidates", lambda tool, show_all=False: [])
    replies, prompts = [], []

    def judge(prompt, j, e, label):
        prompts.append(prompt)
        return _Proc(replies.pop(0) if replies else ""), j
    monkeypatch.setattr(search, "call_judge_with_fallback", judge)
    return v, replies, prompts


# ---------- lint ----------

def test_lint_names_what_is_behind_with_the_command_that_fixes_it(vault):
    v, replies, _ = vault
    assert lint.run(str(v), "step", review=False) == 0
    text = (v / lint.LINT_NAME).read_text(encoding="utf-8")
    assert "kind: cw-lint" in text
    assert "## 项目概览需要更新 / Project pages behind" in text and "[[board/_project|board]]" in text
    assert "Fix: `cw wiki --projects`" in text


def test_lint_finds_links_to_nothing_in_cws_pages_but_not_inside_code(vault):
    v, _, _ = vault
    (v / "board" / "_project.md").write_text(
        "---\nkind: cw-project\nproject: \"board\"\nbuilt: 2099-01-01T00:00\n---\n"
        "- [[2026-11-05 回归|N1]] [[gone note|N2]] `[[not a link]]`\n", encoding="utf-8")
    assert lint.broken_links(str(v)) == ["board/_project.md -> [[gone note]]"]


def test_lint_review_asks_the_judge_per_project_and_reports_findings(vault, capsys):
    v, replies, prompts = vault
    replies.append("## 矛盾\n- 无\n\n## 可能已过时\n- “跑回归”已在较新的笔记中完成。[N1]\n\n## 缺口\n- 无\n")
    assert lint.run(str(v), "step", review=True, yes=True) == 0
    assert "跑回归" in prompts[-1] and "回归通过" in prompts[-1]
    text = (v / lint.LINT_NAME).read_text(encoding="utf-8")
    assert "## board" in text and "### 可能已过时" in text
    assert "[[2026-11-05 回归|N1]]" in text
    assert "Fix: `cw wiki --project board --full`" in text
    assert "review findings in board" in capsys.readouterr().out
    assert "] lint | _vault |" in (v / note.LOG_NAME).read_text(encoding="utf-8")


def test_a_review_with_nothing_to_say_adds_no_section(vault):
    v, replies, _ = vault
    replies.append("## 矛盾\n- 无\n\n## 可能已过时\n- 无\n\n## 缺口\n- 无\n")
    lint.run(str(v), "step", review=True, yes=True)
    assert "## board" not in (v / lint.LINT_NAME).read_text(encoding="utf-8")


def test_the_review_waits_for_a_yes(vault, capsys):
    v, replies, prompts = vault
    lint.run(str(v), "step", review=True, yes=False)  # no terminal: no yes
    assert prompts == []
    assert "judge review was skipped" in capsys.readouterr().err


def test_cli_lint(vault, capsys):
    v, _, _ = vault
    assert wiki.cmd_wiki(["--lint", "--no-review"]) == 0
    assert "report: _lint.md" in capsys.readouterr().out


# ---------- ask ----------

def test_ask_reads_the_catalog_then_answers_from_the_pages_it_picked(vault, capsys):
    v, replies, prompts = vault
    replies += ["E2\n", "用 `fastboot flash boot boot.img` 刷写。[E2]"]
    assert ask.cmd_ask(["怎么刷", "boot？", "--no-save"]) == 0
    pick, answer_prompt = prompts
    assert "[E1]" in pick and "[E2]" in pick and "Question: 怎么刷 boot？" in pick
    assert "fastboot flash boot boot.img" in answer_prompt   # E2's full text
    assert "回归通过" not in answer_prompt                    # E1 was not picked
    out = capsys.readouterr().out
    assert "用 `fastboot flash boot boot.img` 刷写。[E2]" in out
    assert "[E2] note · 2026-11-01 · 2026-11-01 USB 枚举 (board) — board/2026-11-01 USB 枚举.md" in out
    assert not (v / "_wiki").exists()


def test_a_saved_answer_links_its_sources_and_is_found_next_time(vault, capsys):
    v, replies, prompts = vault
    replies += ["E2", "用 fastboot 刷写。[E2]"]
    assert ask.cmd_ask(["怎么刷 boot", "--save"]) == 0
    saved = list((v / "_wiki" / "answers").glob("*.md"))
    assert len(saved) == 1
    text = saved[0].read_text(encoding="utf-8")
    assert "kind: cw-answer" in text and "[[2026-11-01 USB 枚举|E2]]" in text
    assert "] ask | _wiki | 怎么刷 boot" in (v / note.LOG_NAME).read_text(encoding="utf-8")
    kinds = [e[0] for e in ask.catalog(str(v))]
    assert "answer" in kinds
    # an answer is not a topic page: the topic machinery never sees it
    assert wiki.all_pages(str(v)) == []


def test_nothing_to_go_on_says_so(vault, capsys):
    v, replies, _ = vault
    replies.append("NONE")
    assert ask.cmd_ask(["天气如何"]) == 1
    assert "nothing in the vault" in capsys.readouterr().err


def test_an_empty_vault_says_so(monkeypatch, tmp_path, capsys):
    v = tmp_path / "empty"
    v.mkdir()
    monkeypatch.setenv("CW_VAULT", str(v))
    assert ask.cmd_ask(["x"]) == 1
    assert "no notes or pages yet" in capsys.readouterr().err


# ---------- the LLM Wiki side ----------

@pytest.fixture
def llm_wiki(vault, monkeypatch, tmp_path):
    import json
    root = tmp_path / "work-wiki"
    for rel in ("entities/devgetter", "entities/dev-getter", "entities/codex", "entities/codex-cli",
                "entities/rock-5b", "concepts/DevGetter", "concepts/usb-enumeration"):
        path = root / "wiki" / f"{rel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntype: x\n---\n# x\n", encoding="utf-8")
    (root / ".llm-wiki").mkdir()
    (root / ".llm-wiki" / "review.json").write_text(json.dumps([
        {"type": "suggestion", "title": "Write a guide", "resolved": False},
        {"type": "contradiction", "title": "LPO register", "resolved": False,
         "affectedPages": ["wiki/concepts/lpo.md"]},
        {"type": "missing-page", "title": "done already", "resolved": True},
    ]), encoding="utf-8")
    monkeypatch.setenv("CW_LLM_WIKI", str(root))
    monkeypatch.setenv("CW_LLM_WIKI_API", "http://127.0.0.1:9")  # nothing listens: the file is read
    return root


def test_duplicates_are_one_name_spelled_differently_or_one_word_longer(llm_wiki):
    same, near = lint.llm_wiki_duplicates(str(llm_wiki))
    assert same == [["concepts/DevGetter", "entities/dev-getter", "entities/devgetter"]]
    assert near == [("entities/codex", "entities/codex-cli")]


def test_open_reviews_come_from_the_project_file_when_the_api_is_away(llm_wiki):
    titles = [i["title"] for i in lint.llm_wiki_reviews(str(llm_wiki))]
    assert titles == ["Write a guide", "LPO register"]


def test_the_report_lists_reviews_contradictions_first_and_the_duplicates(vault, llm_wiki, capsys):
    v, _, _ = vault
    lint.run(str(v), "step", review=False)
    text = (v / lint.LINT_NAME).read_text(encoding="utf-8")
    section = text[text.index("## LLM Wiki"):]
    assert section.index("**contradiction**") < section.index("**suggestion**")
    assert "- LPO register — `wiki/concepts/lpo.md`" in section
    assert "done already" not in section
    assert "- 重复 / same name: `concepts/DevGetter`, `entities/dev-getter`, `entities/devgetter`" in section
    assert "- 可能 / maybe: `entities/codex` · `entities/codex-cli`" in section
    out = capsys.readouterr().out
    assert "2 LLM Wiki reviews open" in out and "2 possible duplicate LLM Wiki pages" in out


def test_no_llm_wiki_section_without_one(vault):
    v, _, _ = vault
    lint.run(str(v), "step", review=False)
    assert "## LLM Wiki" not in (v / lint.LINT_NAME).read_text(encoding="utf-8")
