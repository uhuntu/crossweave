"""Google Antigravity (agy) integration tests."""

import json
import os
import re
import sqlite3

import pytest

from crossweave import agy, cli, delete, doctor, rename, search, sessions, update

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
    assert r["cwd"] == r"c:\Users\huntl\work\crossweave"
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


def test_a_handoff_into_agy_goes_through_prompt_interactive(monkeypatch):
    monkeypatch.setattr(sessions, "session_handoff_details", lambda t, s: (None, "# x"))
    monkeypatch.setattr(sessions, "write_handoff_export", lambda *a: "/tmp/x.md")
    ran = []
    monkeypatch.setattr(sessions, "exec_or_die", ran.append)
    sessions.perform_handoff("claude", "abc", "agy", [])
    assert ran[0][0] == "agy" and ran[0][-2] == "-i"
    assert ran[0][-1].startswith("Continue the work from this claude session (abc)")


def test_doctor_and_search_include_agy(store):
    store.session(SID, title="Doctor check")
    assert ("agy", doctor._check_agy) in doctor.CHECKS
    state, count, _ = doctor._check_agy()
    assert state == doctor.OK
    assert count == 1

    recs = search.gather_candidates(tool_filter="agy", show_all=False)
    assert [r["id"] for r in recs] == [SID]


def test_rename_agy(store, tmp_path):
    store.con.execute("ALTER TABLE conversation_summaries ADD COLUMN raw_summary blob")
    old_raw = b"\x0a\x09Old title\x12\x04keep\x18\x2a"
    store.con.execute(
        "INSERT INTO conversation_summaries "
        "(conversation_id, title, preview, step_count, last_modified_time, workspace_uris, raw_summary) "
        "VALUES (?,?,?,?,?,?,?)",
        (SID, "Old title", "", 1, "2026-11-15T07:53:42Z", "[]", old_raw),
    )
    store.con.commit()
    ann_dir = tmp_path / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    pbtxt = ann_dir / f"{SID}.pbtxt"
    pbtxt.write_text('title:"Old title" last_user_view_time:{seconds:123}\n', encoding="utf-8")

    assert rename._apply({"tool": "agy", "id": SID}, "新标题 agy") is True
    assert agy.agy_light_records()[0]["title"] == "新标题 agy"
    assert pbtxt.read_text(encoding="utf-8") == 'title:"新标题 agy" last_user_view_time:{seconds:123}\n'
    updated_raw = store.con.execute(
        "SELECT raw_summary FROM conversation_summaries WHERE conversation_id=?", (SID,)
    ).fetchone()[0]
    encoded = "新标题 agy".encode("utf-8")
    assert updated_raw == b"\x0a" + bytes([len(encoded)]) + encoded + b"\x12\x04keep\x18\x2a"

    # Even if agy runtime overwrites conversation_summaries.title later, cw keeps the renamed title
    store.con.execute("UPDATE conversation_summaries SET title=? WHERE conversation_id=?", ("Overwritten", SID))
    store.con.commit()
    assert agy.agy_light_records()[0]["title"] == "新标题 agy"


def test_renamed_agy_handoff_keeps_custom_title(store):
    src_sid = "bbbbbbbb-1111-2222-3333-444444444444"
    store.session(src_sid, title="Original source topic")
    store.session(SID, title="Continue the work from this agy session")
    store.con.execute("ALTER TABLE conversation_summaries ADD COLUMN raw_summary blob")
    store.con.commit()
    store.transcript(SID, [
        {"step_index": 0, "type": "USER_INPUT",
         "content": f"<USER_REQUEST>\nContinue the work from this agy session ({src_sid}).\n</USER_REQUEST>"},
    ])

    rec = next(r for r in agy.agy_light_records() if r["id"] == SID)
    assert sessions.resolve_row(rec)[5] == "(handoff) Original source topic"

    assert rename._apply({"tool": "agy", "id": SID}, "Custom renamed title") is True
    rec = next(r for r in agy.agy_light_records() if r["id"] == SID)
    assert sessions.resolve_row(rec)[5] == "Custom renamed title"


def test_delete_agy(store, tmp_path):
    store.session(SID, title="Delete me")
    tpath = store.transcript(SID, [{"step_index": 0, "type": "USER_INPUT", "content": "hi"}])
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    conv_db = conv_dir / f"{SID}.db"
    conv_db.write_bytes(b"sqlite-data")
    ann_dir = tmp_path / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    ann_file = ann_dir / f"{SID}.pbtxt"
    ann_file.write_text('title:"Delete me"\n', encoding="utf-8")

    sessions.write_list_cache([{"tool": "agy", "id": SID}])
    assert delete.cmd_delete(["1", "-y"]) == 0
    assert agy.agy_light_records() == []
    assert not tpath.exists()
    assert not conv_db.exists()
    assert not ann_file.exists()

    manifest_path = next((tmp_path / "no-stores" / "deleted").glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tool"] == "agy"
    assert manifest["id"] == SID
    assert manifest["db_row"]["title"] == "Delete me"
    assert len(manifest["files"]) == 3


def test_agy_cli_brain_transcript_fallback(store, monkeypatch, tmp_path):
    store.session(SID, title="CLI session", step_count=4)
    cli_brain = tmp_path / "antigravity-cli" / "brain"
    monkeypatch.setattr(agy, "AGY_CLI_BRAIN", str(cli_brain))
    logs = cli_brain / SID / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    tpath = logs / "transcript.jsonl"
    tpath.write_text(
        "\n".join([
            json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nFix agy rename\n</USER_REQUEST>"}),
            json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Checking agy.py."}),
        ]) + "\n",
        encoding="utf-8",
    )

    assert agy.agy_transcript_path(SID) == str(tpath)
    assert agy.agy_handoff_messages(SID) == [("user", "Fix agy rename"), ("assistant", "Checking agy.py.")]


def test_agy_judge_cwd_uses_judge_session_title(store):
    store.session(
        SID,
        title="Each line below describes a past AI coding-assistant session",
        workspace_uris=json.dumps(["file:///tmp/cw-judge-j2u85r13"]),
    )

    records = agy.agy_light_records()
    assert len(records) == 1
    assert records[0]["title"] == common.JUDGE_SESSION_TITLE
