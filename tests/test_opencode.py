"""opencode: a SQLite store (opencode 2.x's session_v2 / session_message).

These tests build a synthetic store of the shapes the reader queries --
copied from a real opencode 2.2.5 database -- and hold it to the contract
every other store meets: listing, titles, turns, last line, search,
handoff and resume.
"""

import json
import sqlite3

import pytest

from crossweave import cli, doctor, opencode, search, sessions

SID = "ses_efac8cd27ffeC7w35ENK0ZWXKW"
SID2 = "ses_efa998ee1ffeNZUIuNq0OfLGCa"
CWD = "/home/hunt/work/JSearch"
MS = 1_791_088_866_518

SCHEMA = """
CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
    slug TEXT, directory TEXT, title TEXT, version TEXT,
    time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
    seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
"""


@pytest.fixture(autouse=True)
def _fresh_connection():
    opencode._drop_connection()
    yield
    opencode._drop_connection()


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.con = sqlite3.connect(self.path)
        self.con.executescript(SCHEMA)
        self.seq = 0

    def session(self, sid, title="A title", cwd=CWD, parent=None, archived=None,
                created=MS, updated=MS + 5_000):
        self.con.execute(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, "p1", parent, "slug", cwd, title, "2.2.5", created, updated, archived))
        return self

    def _row(self, sid, kind, data):
        self.seq += 1
        self.con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                         ("m%d" % self.seq, sid, kind, self.seq, MS, MS, json.dumps(data)))
        return self

    def user(self, sid, text):
        return self._row(sid, "user", {"text": text, "metadata": {"displayText": text},
                                       "files": [], "agents": []})

    def assistant(self, sid, *blocks):
        return self._row(sid, "assistant", {"agent": "build", "content": list(blocks)})

    def system(self, sid, text):
        return self._row(sid, "system", {"text": text, "metadata": {"notice": "instructions"}})

    def done(self, monkeypatch):
        self.con.commit()
        self.con.close()
        monkeypatch.setattr(opencode, "OPENCODE_DB", self.path)
        return self


def text(t):
    return {"type": "text", "text": t}


def tool(name, inp, out):
    return {"type": "tool", "id": "call_1", "name": name,
            "state": {"status": "completed", "input": inp, "content": [text(out)]}}


def basic(tmp_path, monkeypatch):
    return (Store(tmp_path / "opencode.db")
            .session(SID, title="Kilo worktree keeps being recreated")
            .user(SID, "why does it always recreate the .kilo worktree?")
            .assistant(SID, {"type": "reasoning", "text": "look around"},
                       tool("bash", {"command": "git worktree list"}, "typhoon-cacao [kilo]"),
                       text("A Kilo extension makes it."))
            .system(SID, "The Code Mode tool catalog has changed.")
            .user(SID, "remove it then")
            .assistant(SID, text("Done -- the worktree is gone."))
            .done(monkeypatch))


def test_light_records_read_id_cwd_title_and_times(tmp_path, monkeypatch):
    basic(tmp_path, monkeypatch)
    [r] = opencode.opencode_light_records()
    assert r["tool"] == "opencode" and r["id"] == SID
    assert r["title"] == "Kilo worktree keeps being recreated"
    assert r["ts"] == (MS + 5_000) // 1000 and r["started"] == MS // 1000
    assert r["path"] is None


def test_a_missing_store_lists_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode, "OPENCODE_DB", str(tmp_path / "absent.db"))
    assert opencode.opencode_light_records() == []
    assert doctor._check_opencode()[0] == doctor.MISSING


def test_subagent_and_archived_sessions_wait_for_all(tmp_path, monkeypatch):
    (Store(tmp_path / "opencode.db")
     .session(SID).session("ses_child", parent=SID).session("ses_old", archived=MS)
     .done(monkeypatch))
    assert [r["id"] for r in opencode.opencode_light_records()] == [SID]
    assert len(opencode.opencode_light_records(show_all=True)) == 3


def test_a_placeholder_title_gives_way_to_the_first_genuine_prompt(tmp_path, monkeypatch):
    (Store(tmp_path / "opencode.db")
     .session(SID, title="New session - 2026-11-06T12:00:00.000Z")
     .user(SID, "hi").user(SID, "fix the flaky upload test")
     .done(monkeypatch))
    [r] = opencode.opencode_light_records()
    assert r["title"] == "fix the flaky upload test"


def test_turns_count_only_what_the_user_said(tmp_path, monkeypatch):
    basic(tmp_path, monkeypatch)
    assert sessions.session_turns({"tool": "opencode", "id": SID}) == 2


def test_the_last_message_skips_injected_rows(tmp_path, monkeypatch):
    basic(tmp_path, monkeypatch)
    assert sessions.session_last_message({"tool": "opencode", "id": SID}) == \
        "Done -- the worktree is gone."


def test_the_literal_scan_reaches_tool_output(tmp_path, monkeypatch):
    basic(tmp_path, monkeypatch)
    recs = opencode.opencode_light_records()
    assert sessions.literal_matches(recs, "typhoon-cacao") == recs
    assert sessions.literal_matches(recs, "nowhere-said") == []


def test_search_gathers_and_snippets_opencode(tmp_path, monkeypatch):
    basic(tmp_path, monkeypatch)
    [r] = search.gather_candidates("opencode")
    snippet = search.snippet_for(r)
    assert "git worktree list" in snippet and "Kilo extension" in snippet


def test_handoff_exports_what_was_said(tmp_path, monkeypatch):
    basic(tmp_path, monkeypatch)
    cwd, md = sessions.session_handoff_details("opencode", SID)
    assert cwd == opencode._directory(CWD)
    assert "## User\n\nwhy does it always" in md
    assert "A Kilo extension makes it." in md
    assert "typhoon-cacao" not in md  # tool output stays out
    assert "Code Mode" not in md


def test_a_handoff_into_opencode_goes_through_prompt(monkeypatch, tmp_path):
    basic(tmp_path, monkeypatch)
    monkeypatch.setattr(sessions, "session_handoff_details", lambda t, s: (None, "# x"))
    monkeypatch.setattr(sessions, "write_handoff_export", lambda *a: "/tmp/x.md")
    ran = []
    monkeypatch.setattr(sessions, "exec_or_die", ran.append)
    sessions.perform_handoff("claude", "abc", "opencode", [])
    assert ran[0][0] == "opencode" and ran[0][1] == "--prompt"
    assert ran[0][2].startswith("Continue the work from this claude session (abc)")


def test_a_handed_off_session_takes_its_sources_topic(tmp_path, monkeypatch):
    seed = "Continue the work from this opencode session (%s). Read it." % SID
    (Store(tmp_path / "opencode.db")
     .session(SID, title="Kilo worktree keeps being recreated").user(SID, "why?")
     .session(SID2, title="Continuing work from previous session").user(SID2, seed)
     .done(monkeypatch))
    titles = {r["id"]: sessions.resolve_row(r)[5] for r in opencode.opencode_light_records()}
    assert titles[SID2] == "(handoff) Kilo worktree keeps being recreated"


def test_resume_resolves_a_prefix_with_or_without_ses(tmp_path, monkeypatch):
    basic(tmp_path, monkeypatch)
    assert opencode.opencode_resolve("efac8") == [SID]
    assert opencode.opencode_resolve("ses_efac") == [SID]
    ran = []
    monkeypatch.setattr(sessions, "exec_or_die", ran.append)
    monkeypatch.setattr(sessions.os, "chdir", lambda d: None)
    sessions.cmd_resume(["opencode", "efac8"])
    assert ran == [["opencode", "--session", SID]]


def test_doctor_names_a_legacy_store(tmp_path, monkeypatch):
    path = tmp_path / "opencode.db"
    con = sqlite3.connect(str(path))
    con.executescript("CREATE TABLE session (id TEXT); CREATE TABLE message (id TEXT);"
                      "CREATE TABLE part (id TEXT);")
    con.close()
    monkeypatch.setattr(opencode, "OPENCODE_DB", str(path))
    status, _count, note = doctor._check_opencode()
    assert status == doctor.BROKEN and "1.x" in note


def test_build_command_maps_the_common_flags():
    assert cli.build_command("opencode", ["-c", "-y"]) == ["opencode", "-c", "--auto"]
    assert cli.build_command("opencode", ["-p", "-m", "a/b", "hi"]) == \
        ["opencode", "run", "-m", "a/b", "hi"]
    assert cli.build_command("opencode", ["fix", "it"]) == ["opencode", "--prompt", "fix it"]
    with pytest.raises(cli.UsageError):
        cli.build_command("opencode", ["-m", "a/b"])


def test_a_lone_directory_stays_opencodes_positional(tmp_path):
    assert cli.build_command("opencode", [str(tmp_path)]) == ["opencode", str(tmp_path)]
