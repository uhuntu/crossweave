"""Tests for read-only extra homes (e.g. a mounted Windows partition)."""

import json
import os
import sqlite3

import pytest

from crossweave import (
    agy,
    claude,
    codex,
    common,
    delete,
    kimi,
    note,
    opencode,
    pi,
    rename,
    sessions,
    step,
    zcode,
)


def _write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------- discovery & config ----------

def test_extra_homes_from_env_and_windows_root_expansion(monkeypatch, tmp_path):
    win = tmp_path / "mnt_win"
    (win / "Users" / "Administrator" / ".codex").mkdir(parents=True)
    (win / "Users" / "Alice").mkdir(parents=True)
    for skip in ("Public", "Default", "Default User", "All Users", "CodexSandboxOffline"):
        (win / "Users" / skip).mkdir(parents=True)
    # A symlink under Users should be skipped (e.g. All Users -> C:/ProgramData)
    os.symlink(str(win / "Users" / "Alice"), str(win / "Users" / "LinkUser"))

    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", f"{win}{os.pathsep}{tmp_path / 'unmounted'}")
    homes = common.extra_homes()
    assert homes == [
        str(win / "Users" / "Administrator"),
        str(win / "Users" / "Alice"),
    ]


def test_extra_homes_from_config_file_and_empty_env_override(monkeypatch, tmp_path):
    direct = tmp_path / "other_home"
    (direct / ".claude").mkdir(parents=True)
    cfg = tmp_path / "homes"
    cfg.write_text(
        f"# mounted Windows user\n\n{direct}\n{tmp_path / 'missing'}\n{direct}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "EXTRA_HOMES_FILE", str(cfg))
    monkeypatch.delenv("CROSSWEAVE_EXTRA_HOMES", raising=False)
    assert common.extra_homes() == [str(direct)]

    # Explicit empty env var disables config file
    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", "")
    assert common.extra_homes() == []


def test_extra_homes_never_duplicates_primary_home(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "HOME", str(tmp_path))
    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(tmp_path))
    assert common.extra_homes() == []


# ---------- per-tool readers ----------

def test_claude_extra_home_merge_and_primary_precedence(monkeypatch, tmp_path):
    win_home = tmp_path / "win_user"
    win_file = win_home / ".claude" / "projects" / "C--Users-Admin" / "win-only.jsonl"
    _write_jsonl(str(win_file), [
        {"type": "user", "cwd": r"C:\Users\Admin\proj", "message": {"content": "Fix Windows build"}},
        {"type": "assistant", "message": {"content": "Done."}},
    ])
    dup_win = win_home / ".claude" / "projects" / "C--Users-Admin" / "shared-id.jsonl"
    _write_jsonl(str(dup_win), [{"type": "user", "message": {"content": "from win"}}])
    dup_primary = tmp_path / "no-stores" / "claude" / "home-proj" / "shared-id.jsonl"
    _write_jsonl(str(dup_primary), [{"type": "user", "message": {"content": "from linux"}}])

    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))
    recs = {r["id"]: r for r in claude.claude_light_records()}
    assert set(recs) == {"win-only", "shared-id"}
    assert recs["win-only"]["extra_home"] == str(win_home)
    assert "extra_home" not in recs["shared-id"]
    assert recs["shared-id"]["path"] == str(dup_primary)
    assert claude.claude_resolve("win-") == ["win-only"]
    details = sessions.session_handoff_details("claude", "win-only")
    assert details is not None and "Fix Windows build" in details[1]


def test_codex_extra_home_merge_and_find_record(monkeypatch, tmp_path):
    win_home = tmp_path / "win_user"
    sid = "11111111-2222-3333-4444-555555555555"
    rollout = win_home / ".codex" / "sessions" / "2026" / "11" / "18" / f"rollout-2026-11-18T01-00-00-{sid}.jsonl"
    _write_jsonl(str(rollout), [
        {"type": "session_meta", "payload": {"id": sid, "cwd": r"C:\Users\Admin\work"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Analyze drivers"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checked."}]}},
    ])
    _write_jsonl(str(win_home / ".codex" / "session_index.jsonl"), [
        {"id": sid, "thread_name": "Windows driver analysis", "updated_at": "2026-11-18T01:05:00Z"},
    ])

    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))
    recs = codex.codex_light_records()
    assert len(recs) == 1
    assert recs[0]["id"] == sid
    assert recs[0]["title"] == "Windows driver analysis"
    assert recs[0]["cwd"] == r"C:\Users\Admin\work"
    assert recs[0]["extra_home"] == str(win_home)
    assert codex.codex_resolve("11111111") == [sid]
    found = sessions._find_record("codex", sid)
    assert found is not None and found["extra_home"] == str(win_home)
    details = sessions.session_handoff_details("codex", sid)
    assert details is not None and "Analyze drivers" in details[1]


def test_step_and_pi_extra_home(monkeypatch, tmp_path):
    win_home = tmp_path / "win_user"
    step_file = win_home / ".stepcode" / "agent" / "sessions" / "--C--Users-Admin--" / "2026-11-17T03-06-52-285Z_step-win-1.jsonl"
    _write_jsonl(str(step_file), [
        {"type": "session", "id": "step-win-1", "cwd": r"C:\Users\Admin", "timestamp": "2026-11-17T03:06:52.285Z"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Check plugins"}]}},
    ])
    pi_file = win_home / ".pi" / "agent" / "sessions" / "--C--Users-Admin--" / "2026-11-17T03-06-52-285Z_pi-win-1.jsonl"
    _write_jsonl(str(pi_file), [
        {"type": "session", "id": "pi-win-1", "cwd": r"C:\Users\Admin", "timestamp": "2026-11-17T03:06:52.285Z"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Pi task"}]}},
    ])

    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))
    srecs = step.step_light_records()
    assert len(srecs) == 1 and srecs[0]["id"] == "step-win-1" and srecs[0]["extra_home"] == str(win_home)
    precs = pi.pi_light_records()
    assert len(precs) == 1 and precs[0]["id"] == "pi-win-1" and precs[0]["extra_home"] == str(win_home)


def test_kimi_extra_home_translates_windows_session_dir(monkeypatch, tmp_path):
    win_home = tmp_path / "win_user"
    sdir = win_home / ".kimi-code" / "sessions" / "sess_win_1"
    os.makedirs(sdir / "agents" / "main", exist_ok=True)
    (sdir / "state.json").write_text(json.dumps({
        "updatedAt": 1700000000000,
        "createdAt": 1700000000000,
        "cwd": r"C:\Users\Admin\repo",
    }), encoding="utf-8")
    _write_jsonl(str(sdir / "agents" / "main" / "wire.jsonl"), [
        {"type": "turn.prompt", "input": [{"type": "text", "text": "Hello from Windows Kimi"}]},
    ])
    _write_jsonl(str(win_home / ".kimi-code" / "session_index.jsonl"), [
        {
            "sessionId": "sess_win_1",
            "sessionDir": r"C:\Users\Administrator\.kimi-code\sessions\sess_win_1",
            "workDir": r"C:\Users\Admin\repo",
        }
    ])

    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))
    recs = kimi.kimi_light_records(False)
    assert len(recs) == 1
    assert recs[0]["dir"] == str(sdir)
    assert recs[0]["extra_home"] == str(win_home)
    assert kimi.kimi_resolve("sess_win") == ["sess_win_1"]
    msgs = kimi.kimi_handoff_messages(recs[0]["dir"])
    assert msgs == [("user", "Hello from Windows Kimi")]


def test_agy_extra_home_cli_db_and_transcript(monkeypatch, tmp_path):
    win_home = tmp_path / "win_user"
    cli_dir = win_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    db_path = cli_dir / "conversation_summaries.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, last_modified_time TEXT, "
        "workspace_uris TEXT, parent_conversation_id TEXT, nesting_depth INTEGER, step_count INTEGER)"
    )
    sid = "aaaa1111-2222-3333-4444-555566667777"
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, "Win AGY CLI", "hi", "2026-11-17T10:00:00Z",
         json.dumps(["file:///C:/Users/Administrator"]), None, 0, 2),
    )
    con.commit()
    con.close()
    tpath = cli_dir / "brain" / sid / ".system_generated" / "logs" / "transcript.jsonl"
    _write_jsonl(str(tpath), [
        {"type": "USER_INPUT", "content": "<USER_REQUEST>Check Windows setup</USER_REQUEST>"},
        {"type": "PLANNER_RESPONSE", "content": "Looks good."},
    ])

    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))
    recs = agy.agy_light_records()
    assert len(recs) == 1
    assert recs[0]["id"] == sid
    assert recs[0]["cwd"] == r"C:\Users\Administrator"
    assert recs[0]["extra_home"] == str(win_home)
    assert recs[0]["path"] == str(tpath)
    assert agy.agy_resolve("aaaa1111") == [sid]
    assert agy.agy_handoff_messages(sid) == [("user", "Check Windows setup"), ("assistant", "Looks good.")]


def test_zcode_and_opencode_extra_home_sqlite(monkeypatch, tmp_path):
    win_home = tmp_path / "win_user"

    # zcode
    zdb = win_home / ".zcode" / "cli" / "db" / "db.sqlite"
    zdb.parent.mkdir(parents=True)
    con = sqlite3.connect(str(zdb))
    con.execute("CREATE TABLE session (id TEXT, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT)")
    con.execute("CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT)")
    con.execute("CREATE TABLE part (id TEXT, message_id TEXT, sequence INTEGER, data TEXT)")
    con.execute("INSERT INTO session VALUES ('zc-win-1', 'C:\\Users\\Admin', 'Win ZCode', 1700000000, 1700000000, NULL)")
    con.execute("INSERT INTO message VALUES ('m1', 'zc-win-1', 1, ?)", (json.dumps({"role": "user"}),))
    con.execute("INSERT INTO part VALUES ('p1', 'm1', 1, ?)", (json.dumps({"type": "text", "text": "ZCode prompt"}),))
    con.commit()
    con.close()

    # opencode
    odb = win_home / ".local" / "share" / "opencode" / "opencode.db"
    odb.parent.mkdir(parents=True)
    con = sqlite3.connect(str(odb))
    con.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT, time_archived INTEGER)")
    con.execute("CREATE TABLE session_message (session_id TEXT, seq INTEGER, type TEXT, data TEXT)")
    con.execute("INSERT INTO session_v2 VALUES ('ses_win1', 'C:\\Users\\Admin', 'Win OpenCode', 1700000000, 1700000000, NULL, NULL)")
    con.execute("INSERT INTO session_message VALUES ('ses_win1', 1, 'user', ?)", (json.dumps({"text": "OpenCode prompt"}),))
    con.commit()
    con.close()

    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))
    zrecs = zcode.zcode_light_records()
    assert len(zrecs) == 1 and zrecs[0]["id"] == "zc-win-1" and zrecs[0]["extra_home"] == str(win_home)
    assert zcode.zcode_session_turns("zc-win-1") == 1
    assert zcode.zcode_handoff_messages("zc-win-1") == [("user", "ZCode prompt")]

    orecs = opencode.opencode_light_records()
    assert len(orecs) == 1 and orecs[0]["id"] == "ses_win1" and orecs[0]["extra_home"] == str(win_home)
    assert opencode.opencode_session_turns("ses_win1") == 1
    assert opencode.opencode_handoff_messages("ses_win1") == [("user", "OpenCode prompt")]


# ---------- resume handoff & read-only protection ----------

def test_resume_extra_home_session_uses_same_tool_handoff(monkeypatch, tmp_path, capsys):
    win_home = tmp_path / "win_user"
    win_file = win_home / ".claude" / "projects" / "C--Users-Admin" / "win-resume-1.jsonl"
    _write_jsonl(str(win_file), [
        {"type": "user", "cwd": r"C:\Users\Admin", "message": {"content": "Continue this on Linux"}},
    ])
    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))

    handoffs = []
    monkeypatch.setattr(
        sessions,
        "perform_handoff",
        lambda src_tool, src_id, dst_tool, extra, forced_cwd=None, label=None: handoffs.append(
            (src_tool, src_id, dst_tool, extra, forced_cwd)
        ),
    )
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: pytest.fail(f"should not exec natively: {argv}"))

    sessions.cmd_resume(["claude", "win-resume-1"])
    assert handoffs == [("claude", "win-resume-1", "claude", [], None)]
    assert "read-only extra home" in capsys.readouterr().err


def test_rename_refuses_and_delete_supports_extra_home_sessions(monkeypatch, tmp_path, capsys):
    win_home = tmp_path / "win_user"
    win_file = win_home / ".claude" / "projects" / "C--Users-Admin" / "win-ro-1.jsonl"
    _write_jsonl(str(win_file), [
        {"type": "user", "cwd": r"C:\Users\Admin", "message": {"content": "hi"}},
    ])
    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win_home))

    sessions.write_list_cache([{"tool": "claude", "id": "win-ro-1", "extra_home": str(win_home)}])

    # Manual rename still refused
    assert rename.cmd_rename(["1", "New Title"]) == 1
    assert "read-only extra home" in capsys.readouterr().err

    # Direct _apply still refused
    rec = sessions._find_record("claude", "win-ro-1")
    assert rename._apply(rec, "New Title") is False

    # Delete succeeds with backup
    assert delete.cmd_delete(["1", "-y"]) == 0
    out = capsys.readouterr().out
    assert str(win_home) in out
    assert "Backup:" in out
    assert not win_file.exists()


def test_delete_extra_home_agy_cli_db_and_brain(monkeypatch, tmp_path, capsys):
    win_home = tmp_path / "win_user"
    cli_dir = win_home / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    db_path = cli_dir / "conversation_summaries.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, last_modified_time TEXT, "
        "workspace_uris TEXT, parent_conversation_id TEXT, nesting_depth INTEGER, step_count INTEGER)"
    )
    sid = "bbbb1111-2222-3333-4444-555566667777"
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, "Win AGY Del", "hi", "2026-11-17T10:00:00Z",
         json.dumps(["file:///C:/Users/Administrator"]), None, 0, 1),
    )
    con.commit()
    con.close()
    brain_dir = cli_dir / "brain" / sid
    _write_jsonl(str(brain_dir / ".system_generated" / "logs" / "transcript.jsonl"), [
        {"type": "USER_INPUT", "content": "<USER_REQUEST>Hello</USER_REQUEST>"},
    ])

    # Zero-config listing via --home, then delete by row number
    sessions.cmd_list(["--home", str(win_home)])
    capsys.readouterr()
    assert delete.cmd_delete(["1", "-y"]) == 0
    assert not brain_dir.exists()
    con = sqlite3.connect(str(db_path))
    assert con.execute("SELECT COUNT(*) FROM conversation_summaries WHERE conversation_id = ?", (sid,)).fetchone()[0] == 0
    con.close()


def test_note_project_handles_windows_paths():
    assert note._project(r"C:\Users\Administrator\Desktop\Qwen3.0_VLM") == "Qwen3.0_VLM"
    assert note._project(r"C:\Users\Administrator\\") == "Administrator"


def test_cmd_list_home_filter(monkeypatch, tmp_path, capsys):
    win = tmp_path / "mnt_win"
    win_file = win / "Users" / "Admin" / ".claude" / "projects" / "C--Users-Admin" / "win-only.jsonl"
    _write_jsonl(str(win_file), [
        {"type": "user", "cwd": r"C:\Users\Admin", "message": {"content": "Windows task"}},
    ])
    linux_file = tmp_path / "no-stores" / "claude" / "home-proj" / "linux-only.jsonl"
    _write_jsonl(str(linux_file), [
        {"type": "user", "cwd": "/home/hunt", "message": {"content": "Linux task"}},
    ])

    # Zero-config direct path works even without CROSSWEAVE_EXTRA_HOMES
    sessions.cmd_list(["--home", str(win)])
    out = capsys.readouterr().out
    assert "Windows task" in out
    assert "Linux task" not in out

    # Substring match when configured
    monkeypatch.setenv("CROSSWEAVE_EXTRA_HOMES", str(win))
    sessions.cmd_list(["--home", "win"])
    out = capsys.readouterr().out
    assert "Windows task" in out
    assert "Linux task" not in out

    # --home local shows only primary home
    sessions.cmd_list(["--home", "local"])
    out = capsys.readouterr().out
    assert "Linux task" in out
    assert "Windows task" not in out

    # Top-level shorthand `cw --home win`
    from crossweave import cli
    monkeypatch.setattr("sys.argv", ["cw", "--home", "win"])
    cli.main()
    out = capsys.readouterr().out
    assert "Windows task" in out
    assert "Linux task" not in out

    # Zero-config `cw --home /mnt/win` records extra_home in list cache so
    # subsequent `cw 1 rename` reports read-only instead of "no longer exists",
    # and `cw 1 delete` finds and removes the session cleanly.
    monkeypatch.delenv("CROSSWEAVE_EXTRA_HOMES", raising=False)
    sessions.cmd_list(["--home", str(win)])
    capsys.readouterr()
    assert rename.cmd_rename(["1", "New Title"]) == 1
    assert "read-only extra home" in capsys.readouterr().err
    assert delete.cmd_delete(["1", "-y"]) == 0
    assert not win_file.exists()
