"""cw rename: titles written through to the stores themselves.

Each tool's rename surface is the record its own UI writes -- zcode's
title_source='custom', codex's newest-wins index line, claude's
last-wins custom-title record -- so the tests pin those contracts
against the readers that have to see them, plus the row-number and
--auto flows: remote rows refuse, live sessions skip, the judge's
partial answers keep their titles, and the confirm layer never applies
anything without a yes or -y.
"""

import json
import os
import sqlite3
import sys
import time

import pytest

from crossweave import codex, common, rename, search, sessions, zcode


@pytest.fixture(autouse=True)
def _store_env(monkeypatch, tmp_path):
    # the readers resolve these through the common module attribute, the
    # rename writers through the same one -- one patch point for both
    monkeypatch.setattr(common, "CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(common, "CLAUDE_PROJECTS", str(tmp_path / "claude"))
    monkeypatch.setattr(sessions, "CODEX_HOME", common.CODEX_HOME)
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", common.CLAUDE_PROJECTS)
    monkeypatch.setattr(zcode, "ZCODE_DB", str(tmp_path / "zcode" / "db.sqlite"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    monkeypatch.setattr(codex, "_codex_path_index", None)


def _make_zcode_db(tmp_path):
    path = zcode.ZCODE_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    # the real table's CHECK, so 'custom' is the only manual spelling here too
    con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, "
                "time_created INTEGER, time_updated INTEGER, parent_id TEXT, "
                "time_title_updated INTEGER, "
                "title_source TEXT CHECK(title_source in "
                "('default', 'first_input', 'generated', 'custom')))")
    # the snippet reader walks these; empty but present, like the real store
    con.execute("CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, sequence INTEGER, data TEXT)")
    con.commit()
    con.close()
    return path


def _zcode_row(sid, title, age_seconds=None):
    con = sqlite3.connect(zcode.ZCODE_DB)
    updated = int((time.time() - (age_seconds or 0)) * 1000)
    with con:
        con.execute("INSERT INTO session (id, directory, title, time_created, "
                    "time_updated, parent_id, time_title_updated, title_source) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (sid, "/w", title, updated, updated, None, updated, "first_input"))
    con.close()


def _zcode_record(sid, title, age_seconds=None):
    """A fake zcode light record: ts is the store's time_updated in seconds."""
    return {"tool": "zcode", "id": sid, "title": title, "directory": "/w",
            "ts": time.time() - (age_seconds if age_seconds is not None else 0),
            "started": 0, "path": None, "cwd": "/w"}


def _fake_scanners(monkeypatch, records):
    """Each tool's light-record reader returns the records naming it."""
    def make(tool):
        def gather(show_all=False):
            return [dict(r) for r in records if r.get("tool") == tool]
        return gather
    monkeypatch.setattr(sessions, "claude_light_records", make("claude"))
    monkeypatch.setattr(sessions, "codex_light_records", make("codex"))
    monkeypatch.setattr(sessions, "kimi_light_records", make("kimi"))
    monkeypatch.setattr(sessions, "step_light_records", make("step"))
    monkeypatch.setattr(sessions, "zcode_light_records", make("zcode"))
    monkeypatch.setattr(sessions, "codebuddy_light_records", make("codebuddy"))


def _cache(monkeypatch, entries):
    sessions.write_list_cache(entries)


def _zcode_title(sid):
    con = sqlite3.connect(zcode.ZCODE_DB)
    row = con.execute("SELECT title, title_source FROM session WHERE id=?",
                      (sid,)).fetchone()
    con.close()
    return row


OLD = time.time() - 999_000  # well clear of the liveness window


# ---------- manual rename ----------

def test_manual_rename_writes_zcode_with_the_custom_source(monkeypatch, tmp_path):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])

    assert rename.cmd_rename(["1", "跨机索引设计讨论"]) == 0

    assert _zcode_title("sess_abc") == ("跨机索引设计讨论", "custom")


def test_manual_rename_appends_a_codex_index_line(monkeypatch, tmp_path):
    sid = "019c2209-74fe-72c2-86f9-373d2311604a"  # _find_record wants a rollout
    os.makedirs(os.path.join(common.CODEX_HOME, "sessions", "2026", "10", "13"))
    rollout = os.path.join(common.CODEX_HOME, "sessions", "2026", "10", "13",
                           f"rollout-2026-10-13T00-00-00-{sid}.jsonl")
    with open(rollout, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "id": sid}) + "\n")
    index = os.path.join(common.CODEX_HOME, "session_index.jsonl")
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": sid, "thread_name": "old name",
                             "updated_at": "2026-03-09T01:35:07Z"}) + "\n")
    # _find_record("codex", sid) needs the rollout file (above) and the
    # cache row from the listing
    _cache(monkeypatch, [{"tool": "codex", "id": sid}])

    assert rename.cmd_rename(["1", "kernel lockup triage"]) == 0

    lines = [json.loads(l) for l in open(index, encoding="utf-8")]
    assert lines[-1]["thread_name"] == "kernel lockup triage"
    assert lines[-1]["id"] == sid
    # the reader keeps the newest line per id -- the rename is live
    assert codex.codex_thread_names().get(sid) == "kernel lockup triage"


def test_manual_rename_appends_a_claude_custom_title(monkeypatch, tmp_path):
    proj = os.path.join(common.CLAUDE_PROJECTS, "-w")
    os.makedirs(proj)
    path = os.path.join(proj, "claude-1.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": "hi"}) + "\n")
    _fake_scanners(monkeypatch, [{"tool": "claude", "id": "claude-1",
                                  "path": path, "ts": OLD}])
    _cache(monkeypatch, [{"tool": "claude", "id": "claude-1"}])

    assert rename.cmd_rename(["1", "katago tuning"]) == 0

    last = json.loads(open(path, encoding="utf-8").readlines()[-1])
    assert last["type"] == "custom-title" and last["customTitle"] == "katago tuning"


def test_a_tool_without_a_rename_surface_is_refused(monkeypatch, tmp_path, capsys):
    _fake_scanners(monkeypatch, [{"tool": "step", "id": "01a12", "ts": OLD,
                                  "path": "/nowhere.jsonl"}])
    _cache(monkeypatch, [{"tool": "step", "id": "01a12"}])

    assert rename.cmd_rename(["1", "whatever"]) == 1
    assert "no rename surface" in capsys.readouterr().err
    # nothing was written anywhere -- the refusal is the whole outcome


def test_a_remote_row_is_refused(monkeypatch, tmp_path, capsys):
    _make_zcode_db(tmp_path)
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_x", "machine": "kestrel"}])

    assert rename.cmd_rename(["1", "whatever"]) == 1
    assert "lives on kestrel" in capsys.readouterr().err


def test_an_out_of_range_row_is_refused(monkeypatch, tmp_path, capsys):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])

    assert rename.cmd_rename(["4", "whatever"]) == 1
    assert "out of range" in capsys.readouterr().err


# ---------- auto ----------

class _Tty:
    def isatty(self):
        return True


def _confirm(monkeypatch, answer="y"):
    """A terminal that says yes: pytest's stdin is never a tty, and the
    confirm layer refuses off-terminal batches without -y."""
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda _: answer)


def _judge_says(monkeypatch, text, calls=None):
    def fake_call(judge, prompt):
        if calls is not None:
            calls.append((judge, prompt))
        class R:
            returncode = 0
            stdout = text
            stderr = ""
        return R()
    monkeypatch.setattr(search, "_call_judge", fake_call)


def test_auto_renames_via_the_judge_after_a_yes(monkeypatch, tmp_path):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    calls = []
    _judge_says(monkeypatch, "1. 跨机索引设计\n", calls)
    _confirm(monkeypatch)

    assert rename.cmd_rename(["--auto", "1"]) == 0

    assert _zcode_title("sess_abc") == ("跨机索引设计", "custom")
    # one batched judge call, and the prompt asks for the session's own language
    assert len(calls) == 1
    assert "language" in calls[0][1]


def test_auto_without_a_yes_touches_nothing(monkeypatch, tmp_path):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. 跨机索引设计\n")
    _confirm(monkeypatch, answer="n")

    assert rename.cmd_rename(["--auto", "1"]) == 0

    assert _zcode_title("sess_abc")[0] == "hi"


def test_auto_off_a_terminal_needs_dash_y(monkeypatch, tmp_path, capsys):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. 跨机索引设计\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert rename.cmd_rename(["--auto", "1"]) == 1
    assert "pass -y" in capsys.readouterr().err


def test_a_live_session_is_skipped_by_auto(monkeypatch, tmp_path, capsys):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi")  # updated now: a live conversation
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi")])  # ts = now
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    calls = []
    _judge_says(monkeypatch, "", calls)

    assert rename.cmd_rename(["--auto", "1"]) == 0
    assert calls == []  # the judge was never asked
    assert "still live" in capsys.readouterr().err


def test_the_trivial_sweep_picks_only_titles_that_say_nothing(monkeypatch, tmp_path):
    _make_zcode_db(tmp_path)
    for sid, title in (("sess_hi", "hi"), ("sess_cn", "中文"),
                       ("sess_real", "Git rebase 问题")):
        _zcode_row(sid, title, age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_hi", "hi", age_seconds=999_000),
        _zcode_record("sess_cn", "中文", age_seconds=999_000),
        _zcode_record("sess_real", "Git rebase 问题", age_seconds=999_000),
    ])
    calls = []
    _judge_says(monkeypatch, "1. 跨机索引设计\n2. 中文环境配置\n", calls)
    _confirm(monkeypatch, answer="n")  # look, don't touch

    assert rename.cmd_rename(["--auto", "--trivial"]) == 0

    prompt = calls[0][1]
    assert "hi" in prompt and "中文" in prompt
    # the real title is not among the judge's candidates
    assert "Git rebase 问题" not in prompt


def test_a_partial_judge_answer_renames_only_what_it_answered(monkeypatch, tmp_path, capsys):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_a", "hi", age_seconds=999_000)
    _zcode_row("sess_b", "中文", age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_a", "hi", age_seconds=999_000),
        _zcode_record("sess_b", "中文", age_seconds=999_000),
    ])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_a"},
                         {"tool": "zcode", "id": "sess_b"}])
    _judge_says(monkeypatch, "1. 跨机索引设计\n")  # session 2 left unanswered
    _confirm(monkeypatch)

    assert rename.cmd_rename(["--auto", "1", "2"]) == 0
    assert "1 of 2 renamed" in capsys.readouterr().out
    assert _zcode_title("sess_b")[0] == "中文"


def test_a_judge_failure_renames_nothing(monkeypatch, tmp_path, capsys):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])

    def judge_dies(judge, prompt):
        raise search.JudgeError(124)

    monkeypatch.setattr(search, "_call_judge", judge_dies)

    assert rename.cmd_rename(["--auto", "1", "-y"]) == 1
    assert "judge" in capsys.readouterr().err
    assert _zcode_title("sess_abc")[0] == "hi"
