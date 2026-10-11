"""cw doctor: per-tool store health.

The point it exists for is the difference the readers swallow -- a store
that is missing, a store that is there but yields nothing, and a store
that is there but cannot be read are three different situations that
`cw sessions` renders identically as zero rows. These tests build each
situation for real and hold the report to it.
"""

import json
import os
import sqlite3

import pytest

from crossweave import common, doctor, zcode


def test_an_empty_world_reports_every_store_missing(capsys):
    """The conftest isolation points every root at nothing: that is the
    ordinary nobody-uses-this-tool case, not a failure."""
    assert doctor.cmd_doctor([]) == 0
    out = capsys.readouterr().out
    assert out.count("missing") == 6
    assert "0 sessions" in out


def test_a_readable_store_reports_ok_with_its_count(tmp_path, monkeypatch, capsys):
    d = tmp_path / "claude" / "proj"
    d.mkdir(parents=True)
    (d / "abc123.jsonl").write_text(
        json.dumps({"type": "user",
                    "message": {"content": [{"type": "text", "text": "fix it"}]}}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(common, "CLAUDE_PROJECTS", str(tmp_path / "claude"))

    assert doctor.cmd_doctor(["--tool", "claude"]) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "ok" in out and "1 session" in out


def test_a_db_with_the_wrong_schema_is_broken_not_empty(tmp_path, monkeypatch, capsys):
    """The case doctor exists for: the reader's _query turns a schema
    mismatch into [], indistinguishable from no sessions. The report has
    to say what happened, and the exit code has to notice."""
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE something_else (id TEXT)")  # not the store's schema
    con.commit()
    con.close()
    monkeypatch.setattr(zcode, "ZCODE_DB", str(db))

    assert doctor.cmd_doctor(["--tool", "zcode"]) == 1
    out = capsys.readouterr().out
    assert "broken" in out and "schema" in out


def test_a_zcode_store_missing_one_table_is_broken_too(tmp_path, monkeypatch, capsys):
    """`session` alone is not enough: without `message`/`part` the rows
    list but their turns and last lines silently come back empty."""
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, "
                "time_created INTEGER, time_updated INTEGER, parent_id TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(zcode, "ZCODE_DB", str(db))
    monkeypatch.setattr(zcode, "zcode_cli_on_path", lambda: False)

    assert doctor.cmd_doctor(["--tool", "zcode"]) == 1
    assert "broken" in capsys.readouterr().out


def test_a_healthy_zcode_store_says_how_zcode_is_reached(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE session (id TEXT, directory TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER, parent_id TEXT);"
        "CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT);"
        "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, sequence INTEGER, data TEXT);")
    con.execute("INSERT INTO session VALUES ('sess_1', '/w', 't', 1, 2, NULL)")
    con.commit()
    con.close()
    monkeypatch.setattr(zcode, "ZCODE_DB", str(db))
    monkeypatch.setattr(zcode, "zcode_cli_on_path", lambda: False)

    assert doctor.cmd_doctor(["--tool", "zcode"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "1 session" in out and "desktop app only" in out


def test_transcript_files_that_resolve_to_no_sessions_are_broken(tmp_path, monkeypatch, capsys):
    """Codex names its rollouts after the session id; files whose names
    carry no id resolve to nothing. That is not an empty store -- it is a
    store the reader cannot see into, and the report says which."""
    sessions_dir = tmp_path / "codex" / "sessions" / "2026" / "10"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-not-a-uuid.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(common, "CODEX_HOME", str(tmp_path / "codex"))

    assert doctor.cmd_doctor(["--tool", "codex"]) == 1
    out = capsys.readouterr().out
    assert "broken" in out and "1 transcript file(s)" in out


def test_unknown_option_and_bad_tool_are_refused(capsys):
    with pytest.raises(SystemExit):
        doctor.cmd_doctor(["--wat"])
    assert "unknown option '--wat'" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        doctor.cmd_doctor(["--tool", "not-a-tool"])
    assert "--tool must be one of" in capsys.readouterr().err
