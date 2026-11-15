"""Google Antigravity (agy) integration tests."""

import json
import os
import re
import sqlite3

import pytest

from crossweave import agy, cli, doctor, search, sessions, update

SID = "37197b54-255d-4390-a0c1-7d015ba5a452"
SUB_SID = "aaaaaaaa-1111-2222-3333-444444444444"

SCHEMA = """
CREATE TABLE conversation_summaries (
    conversation_id text PRIMARY KEY,
    title text NOT NULL DEFAULT "",
    preview text NOT NULL DEFAULT "",
    step_count integer NOT NULL DEFAULT 0,
    last_modified_time datetime NOT NULL,
    workspace_uris text NOT NULL,
    status text NOT NULL DEFAULT "",
    source text NOT NULL DEFAULT "",
    project_id text NOT NULL DEFAULT "",
    agent_name text NOT NULL DEFAULT "",
    parent_conversation_id text NOT NULL DEFAULT "",
    nesting_depth integer NOT NULL DEFAULT 0
);
"""


@pytest.fixture(autouse=True)
def _fresh_connection():
    agy._drop_connection()
    yield
    agy._drop_connection()


class Store:
    def __init__(self, root):
        self.root = root
        self.db_path = root / "conversation_summaries.db"
        self.brain = root / "brain"
        self.con = sqlite3.connect(str(self.db_path))
        self.con.executescript(SCHEMA)

    def session(self, sid, title="", preview="", step_count=2,
                modified="2026-11-15T07:53:42Z",
                workspace_uris='["file:///c%3A/Users/huntl/work/crossweave"]',
                parent="", depth=0):
        self.con.execute(
            "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, title, preview, step_count, modified, workspace_uris,
             "DONE", "CLI", "", "", parent, depth),
        )
        self.con.commit()
        return self

    def transcript(self, sid, steps):
        logs = self.brain / sid / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "transcript.jsonl"
        path.write_text(
            "\n".join(json.dumps(s) for s in steps) + "\n",
            encoding="utf-8",
        )
        return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store(tmp_path)
    monkeypatch.setattr(agy, "AGY_DIR", str(tmp_path))
    monkeypatch.setattr(agy, "AGY_DB", str(s.db_path))
    monkeypatch.setattr(agy, "AGY_BRAIN", str(s.brain))
    yield s
    s.con.close()


def test_light_records_and_subagent_filtering(store):
    store.session(SID, title="Integrate agy into crossweave")
    store.session(SUB_SID, title="Subagent helper", parent=SID, depth=1)
    store.transcript(SID, [
        {"step_index": 0, "type": "USER_INPUT",
         "content": "<USER_REQUEST>\nAdd agy support\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime\n</ADDITIONAL_METADATA>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE",
         "content": "Done adding agy.", "thinking": "Checking cli.py",
         "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "pytest -q"}}]},
    ])

    recs = agy.agy_light_records(show_all=False)
    assert [r["id"] for r in recs] == [SID]
    r = recs[0]
    assert r["tool"] == "agy"
    assert r["title"] == "Integrate agy into crossweave"
    assert r["ts"] > 0
    expected_cwd = os.path.normpath("c:/Users/huntl/work/crossweave") if os.name == "nt" else "/c:/Users/huntl/work/crossweave"
    assert r["cwd"] == expected_cwd
    assert r["path"] is not None

    all_recs = agy.agy_light_records(show_all=True)
    assert {r["id"] for r in all_recs} == {SID, SUB_SID}


def test_title_fallback_and_transcript_readers(store):
    store.session(SID, title="", preview="fallback preview")
    store.transcript(SID, [
        {"step_index": 0, "type": "USER_INPUT",
         "content": "<USER_REQUEST>\nRefactor session scanner\n</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE",
         "content": "Refactored it.",
         "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "pytest tests/test_agy.py"}}]},
    ])

    recs = agy.agy_light_records()
    assert recs[0]["title"] == "Refactor session scanner"
    assert agy.agy_resolve("3719") == [SID]
    assert agy.agy_session_turns(SID) == 1
    assert agy.agy_handoff_messages(SID) == [
        ("user", "Refactor session scanner"),
        ("assistant", "Refactored it."),
    ]
    assert agy.agy_last_message(SID) == "Refactored it."
    assert agy.agy_first_user_text(SID) == "Refactor session scanner"
    assert "Refactored it." in agy.agy_snippet(SID)
    assert agy.agy_contains(SID, re.compile(r"test_agy\.py"))


def test_cli_flags_and_resume_and_update(store, monkeypatch, capsys):
    launched = []
    monkeypatch.setattr(cli.sessions, "exec_or_die", lambda argv: launched.append(argv))
    monkeypatch.setattr("sys.argv", ["cw", "agy", "-p", "-c", "-m", "Gemini 3.1 Pro (High)", "--add-dir", "/tmp/x", "-y", "hello"])

    cli.main()
    assert launched[-1] == [
        "agy", "-p", "--continue", "--model", "Gemini 3.1 Pro (High)",
        "--add-dir", "/tmp/x", "--dangerously-skip-permissions", "hello",
    ]

    assert update.TOOL_UPDATE_CMD["agy"] == ["agy", "update"]

    store.session(SID, title="Resume test", workspace_uris="[]")
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: launched.append(argv))
    sessions.cmd_resume(["agy", "3719"])
    assert launched[-1] == ["agy", "--conversation", SID]

    sessions.cmd_resume(["agy"])
    assert launched[-1] == ["agy"]


def test_doctor_and_search_include_agy(store):
    store.session(SID, title="Doctor check")
    assert ("agy", doctor._check_agy) in doctor.CHECKS
    state, count, _ = doctor._check_agy()
    assert state == doctor.OK
    assert count == 1

    recs = search.gather_candidates(tool_filter="agy", show_all=False)
    assert [r["id"] for r in recs] == [SID]
