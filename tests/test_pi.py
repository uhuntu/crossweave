"""pi: the store step's format came from.

step is a pi derivative and the two write the same session files, so pi's
reader is step's pointed at ~/.pi/agent/sessions. These tests check the
records come out labelled pi, and that every per-tool path (titles, turns,
search, handoff, rename, resume, delete) treats pi like step.
"""

import json
import os

import pytest

from crossweave import cli, delete, pi, rename, search, sessions, step

SID = "01a0dcd6-7f24-75a5-89b6-982cb9455002"
CWD = "/home/hunt/work/proj"


def write_session(root, sid=SID, cwd=CWD, entries=()):
    folder = os.path.join(str(root), "--home-hunt-work-proj--")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "2026-09-26T08-30-39-909Z_%s.jsonl" % sid)
    lines = [{"type": "session", "version": 3, "id": sid,
              "timestamp": "2026-09-26T08:30:39.909Z", "cwd": cwd}]
    lines += list(entries)
    with open(path, "w", encoding="utf-8") as fh:
        for d in lines:
            fh.write(json.dumps(d, separators=(",", ":")) + "\n")
    return path


def msg(role, *blocks):
    return {"type": "message", "id": os.urandom(4).hex(), "parentId": None,
            "timestamp": "2026-09-26T08:32:17.276Z",
            "message": {"role": role, "content": list(blocks)}}


def text(t):
    return {"type": "text", "text": t}


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "pi-sessions"
    monkeypatch.setattr(pi, "PI_SESSIONS", str(root))
    path = write_session(root, entries=[
        msg("system"),
        msg("user", text("set up the stepfun provider")),
        msg("assistant", {"type": "thinking", "thinking": "check models.json"},
            {"type": "toolCall", "name": "bash", "arguments": {"command": "cat models.json"}}),
        msg("toolResult", text("STEP_API_KEY unset")),
        msg("assistant", text("Provider configured.")),
        msg("user", text("thanks")),
    ])
    return path


def test_records_are_labelled_pi(store):
    [r] = pi.pi_light_records()
    assert r["tool"] == "pi" and r["id"] == SID and r["cwd"] == CWD and r["path"] == store


def test_step_and_pi_stores_stay_apart(store, tmp_path, monkeypatch):
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "step-sessions"))
    write_session(tmp_path / "step-sessions", sid="step-one")
    assert [r["tool"] for r in step.step_light_records()] == ["step"]
    assert [r["tool"] for r in pi.pi_light_records()] == ["pi"]


def test_the_session_dir_env_moves_the_store(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "x"))
    assert pi._default_sessions_dir() == str(tmp_path / "x")
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    assert pi._default_sessions_dir() == os.path.join(str(tmp_path / "agent"), "sessions")


def test_row_title_turns_and_last_message(store):
    [r] = pi.pi_light_records()
    assert sessions.resolve_row(r)[5] == "set up the stepfun provider"
    assert sessions.session_turns(r) == 2
    assert sessions.session_last_message(r) == "thanks"


def test_search_reaches_calls_and_results(store):
    recs = search.gather_candidates("pi")
    assert sessions.literal_matches(recs, "STEP_API_KEY") == recs
    assert "cat models.json" in search.snippet_for(recs[0])


def test_handoff_exports_what_was_said(store):
    cwd, md = sessions.session_handoff_details("pi", SID)
    assert cwd == CWD
    assert "Provider configured." in md and "STEP_API_KEY" not in md


def test_rename_appends_a_session_info_record_pi_reads(store):
    [r] = pi.pi_light_records()
    assert rename._apply(r, "StepFun provider setup")
    with open(store, encoding="utf-8") as fh:
        last = json.loads(fh.readlines()[-1])
    assert last["type"] == "session_info" and last["name"] == "StepFun provider setup"
    assert sessions.resolve_row(r)[5] == "StepFun provider setup"


def test_resume_opens_the_session_by_id(store, monkeypatch):
    ran = []
    monkeypatch.setattr(sessions, "exec_or_die", ran.append)
    monkeypatch.setattr(sessions.os, "chdir", lambda d: None)
    sessions.cmd_resume(["pi", SID[:8]])
    assert ran == [["pi", "--session", SID]]


def test_delete_takes_the_session_file(store):
    [r] = pi.pi_light_records()
    assert delete._paths(r) == [store]


def test_build_command_maps_the_common_flags():
    assert cli.build_command("pi", ["-p", "-m", "step-5", "hi"]) == \
        ["pi", "-p", "--model", "step-5", "hi"]
    assert cli.build_command("pi", ["-c", "-y"]) == ["pi", "-c"]
    with pytest.raises(cli.UsageError):
        cli.build_command("pi", ["--add-dir", "x"])
    assert cli.wants_context("pi", [])
