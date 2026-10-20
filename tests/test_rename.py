"""cw rename: titles written through to the stores themselves.

Each tool's rename surface is the record its own UI writes -- zcode's
title_source='custom', codex's newest-wins index line, claude's
last-wins custom-title record, step's last-wins session_info record,
kimi's titleKind='custom' in state.json -- so the tests pin those
contracts against the readers that have to see them, plus the row-number
and --auto flows: remote rows refuse, live sessions skip, the judge's
partial answers keep their titles, and the confirm layer never applies
anything without a yes or -y.
"""

import json
import os
import re
import sqlite3
import sys
import time

import pytest

from crossweave import cli, codex, common, kimi, rename, search, sessions, step, zcode


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
    monkeypatch.setattr(sessions, "workbuddy_light_records", make("workbuddy"))


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


STEP_SID = "01a0e0e1-2965-79d4-aba9-3dd3bfc0f7cd"


def _step_file(tmp_path, monkeypatch, lines, sid=STEP_SID):
    """A real step session file in a store the step reader is pointed at,
    plus the light record for it. The rename writer appends to the path and
    the reader reads it back through step_title_with_name, so these tests
    exercise the write against the real reader rather than a fake."""
    d = tmp_path / "step" / "--home-hunt--"
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("f_%s.jsonl" % sid)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines),
                    encoding="utf-8")
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "step"))
    return path


def _step_record(path, sid=STEP_SID):
    return {"tool": "step", "id": sid, "ts": OLD, "path": str(path),
            "cwd": "/home/hunt", "started": 0, "name": None}


def test_manual_rename_appends_a_step_session_info(monkeypatch, tmp_path):
    path = _step_file(tmp_path, monkeypatch, [
        {"type": "session", "version": 3, "id": STEP_SID,
         "timestamp": "2026-10-12T02:47:14.509Z", "cwd": "/home/hunt"},
        {"type": "message", "id": "a8831c82", "parentId": None,
         "timestamp": "2026-10-12T02:47:19.139Z",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "中文"}]}},
        {"type": "message", "id": "81cc080a", "parentId": "a8831c82",
         "timestamp": "2026-10-12T02:47:23.601Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "好的，后续用中文回复。"}]}},
    ])
    _cache(monkeypatch, [{"tool": "step", "id": STEP_SID}])

    assert rename.cmd_rename(["1", "pi 升级到 1.1.0"]) == 0

    lines = [json.loads(l) for l in open(path, encoding="utf-8")]
    assert len(lines) == 4  # the three that were there, plus one
    record = lines[-1]
    assert record["type"] == "session_info"
    assert record["name"] == "pi 升级到 1.1.0"
    # ...and step's own readers see it: the name takes the first prompt's
    # place, which is what makes a session titled "中文" nameable at all
    assert step.step_title_with_name(str(path)) == "pi 升级到 1.1.0"


def test_the_step_record_is_shaped_the_way_step_writes_it(monkeypatch, tmp_path):
    """appendSessionInfo's own shape: an 8-hex id that collides with none of
    the file's, the last entry's id as parentId (the chain step's tree walks),
    a millisecond ISO timestamp, and the sanitized name."""
    path = _step_file(tmp_path, monkeypatch, [
        {"type": "session", "version": 3, "id": STEP_SID,
         "timestamp": "2026-10-12T02:47:14.509Z", "cwd": "/home/hunt"},
        {"type": "message", "id": "a8831c82", "parentId": None,
         "timestamp": "2026-10-12T02:47:19.139Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "中文"}]}},
    ])
    _cache(monkeypatch, [{"tool": "step", "id": STEP_SID}])

    assert rename.cmd_rename(["1", "ipv6 开关排查"]) == 0

    record = json.loads(open(path, encoding="utf-8").readlines()[-1])
    assert re.fullmatch(r"[0-9a-f]{8}", record["id"]), record["id"]
    assert record["id"] != STEP_SID  # the header's id is a uuid, not an entry id
    assert record["parentId"] == "a8831c82"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
                        record["timestamp"]), record["timestamp"]


def test_a_second_step_rename_replaces_the_first(monkeypatch, tmp_path):
    """step keeps the LAST session_info record, so renaming twice shows the
    second name -- the first is not an error, just superseded."""
    path = _step_file(tmp_path, monkeypatch, [
        {"type": "session", "version": 3, "id": STEP_SID,
         "timestamp": "2026-10-12T02:47:14.509Z", "cwd": "/home/hunt"},
        {"type": "message", "id": "a8831c82", "parentId": None,
         "timestamp": "2026-10-12T02:47:19.139Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "中文"}]}},
    ])
    _cache(monkeypatch, [{"tool": "step", "id": STEP_SID}])

    assert rename.cmd_rename(["1", "first attempt"]) == 0
    assert rename.cmd_rename(["1", "second attempt"]) == 0

    assert step.step_title_with_name(str(path)) == "second attempt"


def test_a_step_rename_flattens_newlines_the_way_step_does(monkeypatch, tmp_path):
    """appendSessionInfo replaces [\\r\\n]+ with a space before writing; a
    title arriving with a stray newline is stored the way step would have
    stored it, not as a record that breaks the file's one-record-per-line
    shape."""
    path = _step_file(tmp_path, monkeypatch, [
        {"type": "session", "version": 3, "id": STEP_SID,
         "timestamp": "2026-10-12T02:47:14.509Z", "cwd": "/home/hunt"},
    ])
    _cache(monkeypatch, [{"tool": "step", "id": STEP_SID}])

    assert rename.cmd_rename(["1", "ipv6\n开关\r\n排查"]) == 0

    assert step.step_title_with_name(str(path)) == "ipv6 开关 排查"


def test_a_step_rename_refuses_a_file_that_is_gone():
    """The writer's own guard: a record whose file is not there any more is
    refused rather than appended to a path that would be created fresh."""
    assert not rename._apply({"tool": "step", "id": STEP_SID,
                              "path": "/nowhere.jsonl"}, "whatever")


KIMI_SID = "session_ba9b3dc8-ba38-4e52-8222-382bbb5fba08"

# The live session this was built for: kimi's own title is the pasted shell
# transcript it opened with, still marked replaceable, while the wire log's
# first real prompt is "中文" -- which is what cw's row showed.
KIMI_PASTED_TITLE = ("(hunt@hunt-OptiPlex-7071)-[~] $ ai update all Updating "
                     "git install at /home/hunt/work/crossweave")


def _kimi_session(tmp_path, prompt="中文"):
    """A real-shaped kimi session directory: state.json beside
    agents/main/wire.jsonl, with the field set a current build writes."""
    sdir = tmp_path / "kimi" / "wd_crossweave" / KIMI_SID
    (sdir / "agents" / "main").mkdir(parents=True)
    (sdir / "agents" / "main" / "wire.jsonl").write_text(json.dumps({
        "type": "turn.prompt", "input": [{"type": "text", "text": prompt}],
    }) + "\n", encoding="utf-8")
    (sdir / "state.json").write_text(json.dumps({
        "id": KIMI_SID, "version": 2, "cwd": "/home/hunt/work/crossweave",
        "archived": False,
        "agents": {"main": {"homedir": str(sdir / "agents" / "main"),
                            "type": "main"}},
        "custom": {}, "lastTurnReason": "completed",
        "title": KIMI_PASTED_TITLE, "titleKind": "replaceable",
        "createdAt": 1791854095494, "updatedAt": 1791856031638,
        "lastPrompt": "好的", "isCustomTitle": False,
    }), encoding="utf-8")
    return sdir


def _kimi_record(sdir):
    """The light record kimi_light_records builds -- a kimi record carries
    the session *directory*, not a transcript path."""
    return {"tool": "kimi", "id": KIMI_SID, "ts": OLD, "started": OLD,
            "cwd": "/home/hunt/work/crossweave", "dir": str(sdir)}


def test_manual_rename_writes_kimi_state_with_the_custom_kind(monkeypatch, tmp_path):
    sdir = _kimi_session(tmp_path)
    _fake_scanners(monkeypatch, [_kimi_record(sdir)])
    _cache(monkeypatch, [{"tool": "kimi", "id": KIMI_SID}])
    # before: the row says nothing -- the wire's first prompt is "中文" and
    # kimi's own title is a paste it is still holding as replaceable
    assert sessions.kimi_title(str(sdir)) == "中文"

    assert rename.cmd_rename(["1", "cw rename 接入 kimi"]) == 0

    state = json.loads((sdir / "state.json").read_text(encoding="utf-8"))
    assert state["title"] == "cw rename 接入 kimi"
    assert state["titleKind"] == "custom"  # the kind every titler path bails on
    assert state["isCustomTitle"] is True  # the v1 spelling of the same fact
    # ...and kimi's own reader sees it: its picker card shows the stored
    # title as the header, so the row changes with the store
    assert sessions.kimi_title(str(sdir)) == "cw rename 接入 kimi"


def test_the_kimi_rename_leaves_the_rest_of_the_state_alone(monkeypatch, tmp_path):
    """setTitle passes touchUpdatedAt: false, and cw reads updatedAt for a
    session's recency -- bumping it would make a renamed old session look
    live and skip itself the next time. Everything else in the document is
    kimi's own state and goes back untouched."""
    sdir = _kimi_session(tmp_path)
    before = json.loads((sdir / "state.json").read_text(encoding="utf-8"))
    _fake_scanners(monkeypatch, [_kimi_record(sdir)])
    _cache(monkeypatch, [{"tool": "kimi", "id": KIMI_SID}])

    assert rename.cmd_rename(["1", "kimi state 正名"]) == 0

    after = json.loads((sdir / "state.json").read_text(encoding="utf-8"))
    assert after["updatedAt"] == before["updatedAt"]
    assert after["createdAt"] == before["createdAt"]
    for key in ("id", "version", "cwd", "archived", "agents", "custom",
                "lastTurnReason", "lastPrompt"):
        assert after[key] == before[key], key
    # written through a temp file that is gone again -- not left in the
    # session directory for kimi to trip over
    assert [p.name for p in sdir.iterdir() if p.name.endswith(".tmp")] == []


def test_the_kimi_rename_is_the_record_kimis_own_settitle_writes(monkeypatch, tmp_path):
    """{title, titleKind: "custom"} is exactly what setTitle writes and
    encodeSessionMeta mirrors isCustomTitle off -- so a state.json cw wrote
    is indistinguishable from one kimi renamed itself, including to a reader
    that only knows the v1 flag."""
    sdir = _kimi_session(tmp_path)
    _fake_scanners(monkeypatch, [_kimi_record(sdir)])
    _cache(monkeypatch, [{"tool": "kimi", "id": KIMI_SID}])

    assert rename.cmd_rename(["1", "v2 schema 迁移"]) == 0

    state = json.loads((sdir / "state.json").read_text(encoding="utf-8"))
    assert {k: state[k] for k in ("title", "titleKind")} == {
        "title": "v2 schema 迁移", "titleKind": "custom"}
    assert state["isCustomTitle"] == (state["titleKind"] == "custom")
    # a v1 state.json read on its own flag alone sees the same rename
    v1_only = {k: v for k, v in state.items() if k != "titleKind"}
    (sdir / "state.json").write_text(json.dumps(v1_only), encoding="utf-8")
    assert kimi.kimi_stored_title(str(sdir)) == "v2 schema 迁移"


def test_a_second_kimi_rename_replaces_the_first(monkeypatch, tmp_path):
    """The document is rewritten whole, so the second name is the one there
    is -- there is no record stack to keep the last of."""
    sdir = _kimi_session(tmp_path)
    _fake_scanners(monkeypatch, [_kimi_record(sdir)])
    _cache(monkeypatch, [{"tool": "kimi", "id": KIMI_SID}])

    assert rename.cmd_rename(["1", "first attempt"]) == 0
    assert rename.cmd_rename(["1", "second attempt"]) == 0

    assert sessions.kimi_title(str(sdir)) == "second attempt"


def test_a_kimi_rename_refuses_a_session_that_is_gone(tmp_path):
    """The writer's own guards: no directory, or a state.json that is not
    there (or is not a document) -- refused rather than written fresh."""
    gone = tmp_path / "gone" / KIMI_SID
    assert not rename._apply({"tool": "kimi", "id": KIMI_SID, "dir": str(gone)},
                             "whatever")
    broken = tmp_path / "broken" / KIMI_SID
    broken.mkdir(parents=True)
    (broken / "state.json").write_text("{not json", encoding="utf-8")
    assert not rename._apply({"tool": "kimi", "id": KIMI_SID,
                              "dir": str(broken)}, "whatever")
    # nothing was created in its place
    assert (broken / "state.json").read_text(encoding="utf-8") == "{not json"


def test_auto_renames_a_kimi_session(monkeypatch, tmp_path):
    """The live case: a kimi session that opened with "中文" gets a name from
    the judge, and it lands as the custom kind kimi's own picker card shows
    as its header."""
    sdir = _kimi_session(tmp_path)
    _fake_scanners(monkeypatch, [_kimi_record(sdir)])
    _cache(monkeypatch, [{"tool": "kimi", "id": KIMI_SID}])
    _judge_says(monkeypatch, "1. crossweave rename 接入 kimi\n")
    _confirm(monkeypatch)

    assert rename.cmd_rename(["--auto", "1"]) == 0

    assert sessions.kimi_title(str(sdir)) == "crossweave rename 接入 kimi"


@pytest.mark.parametrize("tool,row_id", [("codebuddy", "cb-1"), ("workbuddy", "wb-1")])
def test_a_tool_without_a_rename_surface_is_refused(monkeypatch, tmp_path, capsys, tool, row_id):
    """codebuddy and workbuddy are the two tools with no rename surface cw
    knows: their titler writes the title field of its own record, and there
    is no record cw can append that their reader would keep. workbuddy's
    store is the same engine's under another root, so it inherits the
    refusal rather than getting a guess of its own."""
    _fake_scanners(monkeypatch, [{"tool": tool, "id": row_id, "ts": OLD,
                                  "title": "hi", "cwd": "/w"}])
    _cache(monkeypatch, [{"tool": tool, "id": row_id}])

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


# ---------- the row-first spelling ----------

def _run_cli(monkeypatch, argv):
    """`cw <argv>` all the way through main(), returning the exit code it
    raised (0 for the paths that just return). main() is the only place the
    row-first spelling exists, so the dispatch has to be exercised from there
    and not from cmd_rename."""
    monkeypatch.setattr(sys, "argv", ["cw", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_the_row_may_lead_the_verb(monkeypatch, tmp_path):
    """`cw 12 rename "..."` is `cw rename 12 "..."` -- the same command with
    the row first, the shape `cw 12 codex` already reads in. The word has to
    be lifted out of argv before cmd_rename sees it: cmd_rename takes the row
    as its first positional, so leaving it in place would fold it into the
    title and rename the session to "rename USB 枚举握手排查"."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])

    assert _run_cli(monkeypatch, ["1", "rename", "USB 枚举握手排查"]) == 0

    assert _zcode_title("sess_abc") == ("USB 枚举握手排查", "custom")


def test_a_row_with_no_title_gets_a_judge_proposal(monkeypatch, tmp_path, capsys):
    """`cw 1 rename` with no title is a request for a suggestion, not a
    mistake. The row is the part that takes knowing the listing; the title is
    the part the judge is better at. So it goes and gets one and applies it,
    the way `cw 1 rename <title>` applies what it is handed."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. ZCode session resume flow")

    assert _run_cli(monkeypatch, ["1", "rename"]) == 0

    assert _zcode_title("sess_abc") == ("ZCode session resume flow", "custom")
    assert ("renamed: Hello -> ZCode session resume flow"
            in capsys.readouterr().out)


def test_the_verb_first_spelling_proposes_too(monkeypatch, tmp_path, capsys):
    """`cw rename 1` is the same request spelled the other way round."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. ZCode session resume flow")

    assert rename.cmd_rename(["1"]) == 0

    assert _zcode_title("sess_abc") == ("ZCode session resume flow", "custom")


def test_the_row_named_back_is_the_name_the_listing_shows(monkeypatch, tmp_path, capsys):
    """The title the proposal is measured against has to come from
    _resolve_title_and_cwd, not off the light record: several stores carry no
    title there and the listing resolves it separately, so reading the field
    directly printed "(no title)" for a row that plainly had one."""
    path = _step_file(tmp_path, monkeypatch, [
        {"type": "session", "version": 3, "id": STEP_SID,
         "timestamp": "2026-10-12T02:47:14.509Z", "cwd": "/home/hunt"},
        {"type": "message", "id": "a8831c82", "parentId": None,
         "timestamp": "2026-10-12T02:47:19.139Z",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "中文"}]}},
    ])
    _fake_scanners(monkeypatch, [_step_record(path)])
    _cache(monkeypatch, [{"tool": "step", "id": STEP_SID}])
    _judge_says(monkeypatch, "1. 中文问题排查")

    assert rename.cmd_rename(["1"]) == 0

    assert "renamed: 中文 -> 中文问题排查" in capsys.readouterr().out


def test_a_row_with_nothing_in_it_is_not_asked(monkeypatch, tmp_path, capsys):
    """Same rule as the sweep: a session that holds no words has no topic to
    name, and asking anyway buys an invented one. The judge is never called."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    calls = []
    _judge_says(monkeypatch, "1. invented", calls=calls, snippet="")

    assert rename.cmd_rename(["1"]) == 1

    assert "holds no content to name" in capsys.readouterr().err
    assert calls == []
    assert _zcode_title("sess_abc") == ("hi", "first_input")


def test_a_proposal_that_restates_the_title_is_not_written(monkeypatch, tmp_path, capsys):
    """A custom rename that changes nothing is worse than doing nothing: in
    every store a custom rename is what stops the tool's own titler from ever
    improving the name, so writing it freezes a bad title in place for good."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. hello")

    assert rename.cmd_rename(["1"]) == 1

    assert "already has" in capsys.readouterr().err
    assert _zcode_title("sess_abc") == ("Hello", "first_input")


def test_a_dead_judge_renames_the_row_to_nothing(monkeypatch, tmp_path, capsys):
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])

    def judge_dies(judge, prompt):
        raise search.JudgeError(124)

    monkeypatch.setattr(search, "_call_judge", judge_dies)
    _snippet_says(monkeypatch)

    assert rename.cmd_rename(["1"]) == 1

    assert "judge" in capsys.readouterr().err
    assert _zcode_title("sess_abc") == ("Hello", "first_input")


def test_a_mistyped_row_with_no_title_says_out_of_range(monkeypatch, tmp_path, capsys):
    """Counting the arguments first used to answer a bad row number with the
    usage wall, hiding the "out of range" message that has something to say
    about it. Resolving first lets both errors be their own."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello")
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello")])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])

    assert rename.cmd_rename(["4"]) == 1

    assert "4 is out of range (last listing had 1 rows)" in capsys.readouterr().err


def test_no_arguments_at_all_still_prints_the_full_usage(monkeypatch, tmp_path, capsys):
    """With no row named there is nothing to resolve and nothing to propose
    for, so the usage text is still the whole answer."""
    def judge_never(judge, prompt):
        raise AssertionError("no row was named, so nothing should be asked")

    monkeypatch.setattr(search, "_call_judge", judge_never)
    assert rename.cmd_rename([]) == 2
    assert rename.cmd_rename(["hello"]) == 2  # not a row number either

    err = capsys.readouterr().err
    assert "--auto" in err and "write through to the store" in err
    assert "renamed:" not in err


def test_a_truncated_title_cuts_at_a_separator_not_mid_word():
    """The message shows the row's title so the row can be recognized, which
    means a long one gets cut. Cutting at the character limit landed inside a
    word -- "…是 bug 不是日志噪…" reads like a typo rather than a title that
    goes on -- so the cut moves to the nearest separator instead."""
    long_mixed = "1. OpenClaw telegram 重复轮询（每 60s 报一次错，是 bug 不是日志噪音）： Telegram getUpdate"
    # The limit falls on 噪, three characters into the word 噪音; the cut moves
    # back to the space before 不是 instead.
    assert rename._short(long_mixed) == "1. OpenClaw telegram 重复轮询（每 60s 报一次错，是 bug…"

    # Only a separator in the last stretch counts: this one sits at the very
    # front, and reaching back to it would spend the whole limit buying one
    # clause. A hard cut keeps the rest.
    assert rename._short("a " + "x" * 60) == "a " + "x" * 46 + "…"

    # No separator anywhere in range -- nothing to move to.
    assert rename._short("x" * 60) == "x" * 48 + "…"

    # Short enough, or empty: untouched either way.
    assert rename._short("Hello") == "Hello"
    assert rename._short("") == "(no title)"


def test_any_other_word_after_a_row_still_reaches_the_tool(monkeypatch):
    """Only `rename` is lifted out of that slot. Every other word keeps the
    meaning it always had -- a continuation prompt handed to the tool, which
    is why the one word that changes had to be named explicitly."""
    seen = []
    monkeypatch.setattr(sessions, "cmd_resume", lambda args: seen.append(args))

    assert _run_cli(monkeypatch, ["1", "接着这个话题聊"]) == 0

    assert seen == [["1", "接着这个话题聊"]]


def test_the_explicit_spelling_still_passes_rename_as_a_prompt(monkeypatch):
    """`cw resume 1 rename` says what `cw 1 rename` used to mean -- continue
    the session with the word as its instruction. Giving up that reading is
    the price of the row-first one, and this is the way back to it."""
    seen = []
    monkeypatch.setattr(sessions, "cmd_resume", lambda args: seen.append(args))

    assert _run_cli(monkeypatch, ["resume", "1", "rename"]) == 0

    assert seen == [["1", "rename"]]


def test_the_row_first_spelling_takes_an_auto_selector(monkeypatch, tmp_path, capsys):
    """`cw 12 rename --auto` falls out of the routing for free: the row lands
    in cmd_rename's positionals and --auto keeps its flags, so it reads as
    `cw rename --auto 12` -- propose for that one row and nothing else."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])

    def judge_refuses(_judge, _prompt):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no judge"
        return R()
    monkeypatch.setattr(search, "_call_judge", judge_refuses)
    _snippet_says(monkeypatch)

    assert _run_cli(monkeypatch, ["1", "rename", "--auto"]) == 1

    # it got as far as asking the judge about row 1 -- not a usage error, and
    # not a sweep of every session
    assert "judge" in capsys.readouterr().err


# ---------- auto ----------

class _Tty:
    def isatty(self):
        return True


def _confirm(monkeypatch, answer="y"):
    """A terminal that says yes: pytest's stdin is never a tty, and the
    confirm layer refuses off-terminal batches without -y."""
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda _: answer)


def _snippet_says(monkeypatch, text="hello, can you help me look at something"):
    """Stand in for what the judge is shown -- see _judge_says."""
    monkeypatch.setattr(search, "snippet_for", lambda r: text)


def _judge_says(monkeypatch, text, calls=None,
                snippet="hello, can you help me look at something"):
    """Stub the judge's answer.

    `snippet` stands in for what the judge gets shown. The real readers
    find nothing in these fixtures, and a row with nothing to show is not
    offered to the judge at all (see _propose_titles), so a test that
    wants its row to reach the judge has to give it something to be
    shown. Pass snippet="" for the other case.
    """
    def fake_call(judge, prompt):
        if calls is not None:
            calls.append((judge, prompt))
        class R:
            returncode = 0
            stdout = text
            stderr = ""
        return R()
    monkeypatch.setattr(search, "_call_judge", fake_call)
    _snippet_says(monkeypatch, snippet)


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


# ---------- a rename is not the conversation happening again ----------

def test_renaming_a_claude_session_leaves_its_mtime_alone(monkeypatch, tmp_path):
    """claude takes the session's timestamp from the file's mtime, so a plain
    append reads a months-old conversation as active a minute ago -- it floats
    above genuinely recent work, and the sweep's own liveness guard skips it
    next run. The clock is restored, the record is not."""
    # a claude-shaped transcript, since that is the store under test
    cpath = tmp_path / "claude.jsonl"
    cpath.write_text(json.dumps({"type": "user", "sessionId": "cs-1",
                                 "message": {"role": "user", "content": "hi"}}) + "\n",
                     encoding="utf-8")
    cbefore = os.stat(cpath).st_mtime

    assert rename._rename_claude("cs-1", "A real name", str(cpath)) is True

    assert os.stat(cpath).st_mtime == cbefore
    lines = [json.loads(ln) for ln in
             cpath.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines[-1] == {"type": "custom-title", "customTitle": "A real name",
                         "sessionId": "cs-1"}
    # the mtime is untouched, but the record is really there
    assert len(lines) == 2


def test_renaming_a_step_session_leaves_its_mtime_alone(monkeypatch, tmp_path):
    """Same clock, same store problem: step also derives ts from mtime."""
    path = _step_file(tmp_path, monkeypatch, [
        {"type": "session", "version": 3, "id": STEP_SID,
         "timestamp": "2026-10-12T02:47:14.509Z", "cwd": "/home/hunt"},
        {"type": "message", "id": "a8831c82", "parentId": None,
         "timestamp": "2026-10-12T02:47:19.139Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}],
        sid=STEP_SID)
    before = os.stat(path).st_mtime

    assert rename._rename_step("A real name", str(path)) is True

    assert os.stat(path).st_mtime == before
    lines = [json.loads(ln) for ln in
             path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines[-1]["type"] == "session_info"
    assert lines[-1]["name"] == "A real name"


# ---------- the id column has to tell sessions apart ----------

def test_the_listing_shows_enough_id_to_tell_sessions_apart(monkeypatch):
    """Twelve characters was not enough: 43 of this machine's sessions share a
    12-character prefix with another, so a row could not be pointed at. The
    width is a named constant so a store that starts issuing denser ids has
    one place to change."""
    assert sessions.ID_SHOW >= 13
    # and it is actually used by the row the listing builds
    row = sessions.resolve_row({"tool": "codex", "id": "a" * 40, "ts": 0,
                                "cwd": "/w", "title": "t"})
    assert row[3] == "a" * sessions.ID_SHOW


def test_the_proposal_table_widens_the_id_and_lines_the_arrow_up(
        monkeypatch, tmp_path, capsys):
    """The continuation line used a hardcoded 16-space indent while the title
    started at column 25, so every arrow sat nine columns left of the thing it
    was explaining."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. a much better name\n")
    _confirm(monkeypatch, answer="n")  # look, don't touch

    assert rename.cmd_rename(["--auto", "1"]) == 0

    out = capsys.readouterr().out.splitlines()
    head = next(ln for ln in out if "sess_abc" in ln)
    arrow = next(ln for ln in out if "-> a much better name" in ln)
    # the arrow starts exactly where the old title does
    assert arrow.index("->") == head.index("hi")


def test_a_proposal_that_restates_the_title_is_not_applied(monkeypatch, tmp_path, capsys):
    """The judge answering "hi" for a session titled "hi" has said nothing
    new -- and a custom rename is, in every store, exactly what stops the
    tool's own titler from ever improving the name. Applying one would freeze
    the bad title in place for good, so it is dropped and shown as kept. A
    real sweep hit this for a fifth of its batch."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. hi\n")
    _confirm(monkeypatch, answer="y")  # would apply it if it were offered

    assert rename.cmd_rename(["--auto", "1"]) == 1

    assert "judge proposed the title it already has" in capsys.readouterr().out
    assert _zcode_title("sess_abc") == ("hi", "first_input")


def test_a_case_only_difference_counts_as_the_same_title(monkeypatch, tmp_path, capsys):
    """"hello" for a session titled "Hello" is the same nothing-new answer, and
    writing it would still freeze the title -- case is not an improvement
    worth a custom rename for."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "Hello", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "Hello", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    _judge_says(monkeypatch, "1. hello\n")
    _confirm(monkeypatch, answer="y")

    assert rename.cmd_rename(["--auto", "1"]) == 1

    assert "judge proposed the title it already has" in capsys.readouterr().out
    assert _zcode_title("sess_abc") == ("Hello", "first_input")


# ---------- a session with nothing in it is not offered to the judge ----------

def test_a_session_with_no_content_is_never_offered_to_the_judge(
        monkeypatch, tmp_path, capsys):
    """A session opened and abandoned -- a cancelled /resume, a codex launch
    closed before a word was typed -- has no topic to name. Sending it to the
    judge anyway buys an invented title, because the prompt says to propose
    one for every line and forbids the honest answer. It stays in the table
    marked as considered, so the sweep is not silently smaller than it looks.
    """
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _zcode_row("sess_xyz", "hello", age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_abc", "hi", age_seconds=999_000),
        _zcode_record("sess_xyz", "hello", age_seconds=999_000),
    ])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"},
                         {"tool": "zcode", "id": "sess_xyz"}])
    # sess_abc holds nothing; the judge is asked about sess_xyz alone
    # (after _judge_says, which installs its own snippet stand-in)
    _judge_says(monkeypatch, "1. a real answer\n")
    monkeypatch.setattr(search, "snippet_for",
                        lambda r: "" if r["id"] == "sess_abc" else "a real question")
    _confirm(monkeypatch, answer="n")  # look, don't touch

    assert rename.cmd_rename(["--auto", "1", "2"]) == 0

    out = capsys.readouterr().out
    assert "no content to name" in out
    assert "a real answer" in out
    assert _zcode_title("sess_abc") == ("hi", "first_input")


def test_a_batch_of_empty_sessions_never_calls_the_judge(monkeypatch, tmp_path,
                                                         capsys):
    """Every candidate empty is the degenerate sweep: there is nothing to ask
    about, so the judge is not called at all rather than called to invent
    sixty titles."""
    _make_zcode_db(tmp_path)
    for sid in ("sess_a", "sess_b"):
        _zcode_row(sid, "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_a", "hi", age_seconds=999_000),
        _zcode_record("sess_b", "hi", age_seconds=999_000),
    ])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_a"},
                         {"tool": "zcode", "id": "sess_b"}])
    calls = []
    _judge_says(monkeypatch, "1. invented\n2. invented\n", calls, snippet="")

    assert rename.cmd_rename(["--auto", "1", "2", "-y"]) == 1

    assert not calls
    assert "hold any content to name" in capsys.readouterr().err


def test_skipping_an_empty_row_keeps_the_judge_numbering_lined_up(
        monkeypatch, tmp_path, capsys):
    """The renumbering is the whole risk in not sending empty rows: the judge
    is handed lines 1..K, so its "2." has to land on the row that was third
    in the batch, not the second. Getting this wrong renames the wrong
    session."""
    _make_zcode_db(tmp_path)
    for sid in ("sess_one", "sess_two", "sess_three"):
        _zcode_row(sid, "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_one", "hi", age_seconds=999_000),
        _zcode_record("sess_two", "hi", age_seconds=999_000),
        _zcode_record("sess_three", "hi", age_seconds=999_000),
    ])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_one"},
                         {"tool": "zcode", "id": "sess_two"},
                         {"tool": "zcode", "id": "sess_three"}])
    # only the middle session is empty, so the judge sees two lines and its
    # second answer belongs to sess_three (installed after _judge_says, which
    # brings its own snippet stand-in)
    _judge_says(monkeypatch, "1. first answer\n2. third answer\n")
    monkeypatch.setattr(search, "snippet_for",
                        lambda r: "" if r["id"] == "sess_two" else "real content")
    _confirm(monkeypatch, answer="y")

    assert rename.cmd_rename(["--auto", "1", "2", "3"]) == 0

    assert _zcode_title("sess_one") == ("first answer", "custom")
    assert _zcode_title("sess_two") == ("hi", "first_input")
    assert _zcode_title("sess_three") == ("third answer", "custom")


def test_auto_renames_a_step_session(monkeypatch, tmp_path):
    """A step session that opened with "中文" is the case the whole feature
    exists for: the title says nothing, the judge proposes from the content,
    and the name lands as the session_info record step's own picker reads."""
    path = _step_file(tmp_path, monkeypatch, [
        {"type": "session", "version": 3, "id": STEP_SID,
         "timestamp": "2026-10-12T02:47:14.509Z", "cwd": "/home/hunt"},
        {"type": "message", "id": "a8831c82", "parentId": None,
         "timestamp": "2026-10-12T02:47:19.139Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "中文"}]}},
        {"type": "message", "id": "81cc080a", "parentId": "a8831c82",
         "timestamp": "2026-10-12T02:47:23.601Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "好的，后续用中文回复。你的 pi 已更新到 1.1.0。"}]}},
    ])
    _fake_scanners(monkeypatch, [_step_record(path)])
    _cache(monkeypatch, [{"tool": "step", "id": STEP_SID}])
    _judge_says(monkeypatch, "1. pi 升级到 1.1.0\n")
    _confirm(monkeypatch)

    assert rename.cmd_rename(["--auto", "1"]) == 0

    assert step.step_title_with_name(str(path)) == "pi 升级到 1.1.0"


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


def test_the_sweep_counts_empty_sessions_instead_of_carrying_them(
        monkeypatch, tmp_path, capsys):
    """A session holding no words can never be renamed, so carrying it through
    the sweep spends one of the run's sixty slots on it and re-offers it on
    every run from then on. It is counted instead -- and the count is the only
    place it appears, so a batch is not a third empty rows."""
    _make_zcode_db(tmp_path)
    for sid, title in (("sess_hi", "hi"), ("sess_gone", "hi"), ("sess_real", "hi")):
        _zcode_row(sid, title, age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_hi", "hi", age_seconds=999_000),
        _zcode_record("sess_gone", "hi", age_seconds=999_000),
        _zcode_record("sess_real", "hi", age_seconds=999_000),
    ])
    _judge_says(monkeypatch, "1. a real answer\n")
    monkeypatch.setattr(search, "snippet_for",
                        lambda r: "" if r["id"] == "sess_gone" else "real content")
    _confirm(monkeypatch, answer="n")  # look, don't touch

    assert rename.cmd_rename(["--trivial"]) == 0

    out = capsys.readouterr()
    assert "1 session with no content to name" in out.err
    # counted, not tabled -- and the judge was asked about the other two only
    assert "sess_gone" not in out.out
    assert "a real answer" in out.out


def test_trivial_alone_is_the_sweep(monkeypatch, tmp_path):
    """`cw rename --trivial` with no --auto is the same sweep. --trivial picks
    neither a title nor a row, so there is nothing else it could be asking
    for -- and the two-flag spelling was one word longer for no distinction."""
    _make_zcode_db(tmp_path)
    for sid, title in (("sess_hi", "hi"), ("sess_cn", "中文")):
        _zcode_row(sid, title, age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_hi", "hi", age_seconds=999_000),
        _zcode_record("sess_cn", "中文", age_seconds=999_000),
    ])
    calls = []
    _judge_says(monkeypatch, "1. 跨机索引设计\n2. 中文环境配置\n", calls)
    _confirm(monkeypatch, answer="n")

    assert rename.cmd_rename(["--trivial"]) == 0

    assert len(calls) == 1
    assert "hi" in calls[0][1] and "中文" in calls[0][1]


def test_auto_with_neither_rows_nor_trivial_still_says_so(monkeypatch, tmp_path, capsys):
    """--auto alone has nothing to work on: it names no rows and asks for no
    sweep, so it says exactly that rather than sweeping unprompted."""
    _make_zcode_db(tmp_path)
    _zcode_row("sess_abc", "hi", age_seconds=999_000)
    _fake_scanners(monkeypatch, [_zcode_record("sess_abc", "hi", age_seconds=999_000)])
    _cache(monkeypatch, [{"tool": "zcode", "id": "sess_abc"}])
    calls = []
    _judge_says(monkeypatch, "1. anything\n", calls)

    assert rename.cmd_rename(["--auto"]) == 0

    assert calls == []  # the judge was never asked
    assert "no eligible sessions" in capsys.readouterr().err


# ---------- the sweep's notion of a title that says nothing ----------

# Every noise case here is a row this machine's listing actually held, and
# every good one is a long title that must survive the sweep -- the failure
# that matters is the reverse, proposing for a session that already has a
# real name.
NOISE_TITLES = (
    # a pasted shell transcript, with and without the host part
    "(hunt@hunt-OptiPlex-7071)-[~] $ ai update all Updating git install ...",
    "(hunt\\@hunt-OptiPlex-7071)-[\\~] $ df Filesystem 1K-blocks Used Avail",
    "hunt@swc-build:~$ ai update all Updating git install at /home/hunt",
    "$ kimi update error: failed to check for updates: This operation",
    # pasted output with no prompt line at all
    "drwxrwxr-x 36 1001 1001 4096 Aug 14 15:25 mt8390_android13_sdk d",
    "/home/hunt/EDLA/A13/android-gts/tools",
    # a bare link dropped in for context
    "https://platform.stepfun.com/docs/zh/step-plan/integrations/pi",
    "https://stepfun.ai/",
    # a pasted email -- Outlook's Chinese headers and its reply marker
    "发件人: Sam Wood [mailto:Sam.Wood@microtouch.com] 发送时间: Monday, Apr",
    "Subject: Request for Frequency Lock Tools & Test Mode Support – ",
    "[答复_ Zytronics Rain mode setting with MT7 tool - Max Chen 陳桂華 (M",
    # claude-code's own wrappers and an injected cron reminder
    "<!-- reply --> > Are you trying to find the bearer token to past",
    "<!-- attach --> > investigate the crash further, check journal l",
    "[cron:da2e196a-851c-4dc2-bd82-e21f487dee00 skill-collection-revi",
    "<local-command-stdout> This conversation is from a different dir",
    # a logcat line and a CLI's startup banner
    "2026-02-06 17:36:29.646 4759-4759 OpenClawCanvas ai.openclaw.and",
    "▄▄▄▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄▄",
    "Kimi Code · +1 model. ╭─────────────────────────────────────────",
    # an unbroken token: base64, an API key, a bare smb:// path
    "12XFZ75lYpdLKWcKvVd9kZ5METk61qiDoeprelndlJNE13ZXZ88JqeNCBhaU4jvp",
    "2hPHI3syq5wVuqSEm7ti9Bo0oM9JQ7whztAeiLPfsiopA1vqDhRg26H2acedeiT2",
    "smb://10.11.97.239/swc/Common/EDLA/GOTA_EN",
)

GOOD_TITLES = (
    # long, but a request a person wrote
    "I had install zsh and oh my zsh, but I want kali style auto comple",
    "Do you know anything in your resume history about testing about bt",
    "Please study if there are some services that connect to 10.11.108.",
    "Why I can not type chinese in https://www.kimi.com/?chat_enter_method=",
    "Use [http://127.0.0.1:9090](http://127.0.0.1:9090) as the API addr",
    "That explanation gets the timing and the failed assertion right, b",
    # a path or a URL inside a real sentence is not a bare paste
    "修复 /etc/ssh/sshd_config 里的 PermitRootLogin 设置",
    "看着 https://github.com/aa/bb 这个仓库说说发布流程",
    # a banner glyph or two in a real title is not startup art
    "修复 ▄▄▄ 方块显示问题",
    # a date that opens a real title is not a logcat line
    "2026-02-06 发布计划",
    # short but real
    "Git rebase 问题",
    "连通性测试",
)


@pytest.mark.parametrize("title", NOISE_TITLES)
def test_a_title_that_is_a_paste_says_nothing(title):
    assert rename._is_trivial(title), title


@pytest.mark.parametrize("title", GOOD_TITLES)
def test_a_real_title_is_not_proposed_for(title):
    assert not rename._is_trivial(title), title


def test_the_sweep_reaches_the_long_pastes(monkeypatch, tmp_path):
    """The case the shape test exists for: a session whose title is a pasted
    shell transcript is a worse row than one titled "hi", and used to pass
    every check the sweep made -- it is long, so nothing about its length
    said otherwise."""
    _make_zcode_db(tmp_path)
    pasted = "(hunt@hunt-OptiPlex-7071)-[~] $ ai update all Updating git install ..."
    real = "Please study if there are some services that connect to 10.11.108."
    for sid, title in (("sess_paste", pasted), ("sess_real", real)):
        _zcode_row(sid, title, age_seconds=999_000)
    _fake_scanners(monkeypatch, [
        _zcode_record("sess_paste", pasted, age_seconds=999_000),
        _zcode_record("sess_real", real, age_seconds=999_000),
    ])
    calls = []
    _judge_says(monkeypatch, "1. 各 CLI 自更新流程排查\n", calls)
    _confirm(monkeypatch, answer="n")  # look, don't touch

    assert rename.cmd_rename(["--auto", "--trivial"]) == 0

    prompt = calls[0][1]
    assert pasted in prompt
    # the long but genuine title is left alone
    assert "Please study" not in prompt


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
    _snippet_says(monkeypatch)

    assert rename.cmd_rename(["--auto", "1", "-y"]) == 1
    assert "judge" in capsys.readouterr().err
    assert _zcode_title("sess_abc")[0] == "hi"


@pytest.mark.parametrize('output', [
    '1. 蓝牙 HCD 固件加载验证',
    '**1. 蓝牙 HCD 固件加载验证**',
    '- 1：蓝牙 HCD 固件加载验证',
    '1、蓝牙 HCD 固件加载验证',
    '1）蓝牙 HCD 固件加载验证',
])
def test_title_reply_formats(output):
    assert rename._parse_titles(output, 1) == {1: '蓝牙 HCD 固件加载验证'}


@pytest.mark.parametrize('output', ['', 'Unable to connect', '1', '2. wrong session'])
def test_unstructured_reply_never_becomes_a_title(output):
    assert rename._parse_titles(output, 1) == {}


@pytest.mark.parametrize('output, diagnostic', [
    ('', 'returned empty output'),
    ('Unable to connect', 'returned no numbered titles'),
])
def test_unusable_reply_explains_failure(monkeypatch, tmp_path, capsys, output, diagnostic):
    _make_zcode_db(tmp_path)
    _zcode_row('sess_abc', 'Hello')
    _fake_scanners(monkeypatch, [_zcode_record('sess_abc', 'Hello')])
    _cache(monkeypatch, [{'tool': 'zcode', 'id': 'sess_abc'}])
    _judge_says(monkeypatch, output)
    assert rename.cmd_rename(['1']) == 1
    assert diagnostic in capsys.readouterr().err
    assert _zcode_title('sess_abc') == ('Hello', 'first_input')
