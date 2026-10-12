"""cw machines: the multi-machine session index as a set of contracts.

The index files meet in a directory the user syncs themselves, so every
reader here also reads files written by a different machine, an older cw,
or a sync tool that dropped a file mid-flight. These tests hold the
module to: publish only where the directory exists, publish atomically
and fully self-describing (the reader needs nothing but JSON), refuse
exactly what cannot be trusted -- one stderr line each, never fatal --
and coalesce against the local set so a mirrored machine stays fully
resumable rather than read-only.
"""

import datetime
import json
import os
import socket

import pytest

from crossweave import machines, sessions


@pytest.fixture(autouse=True)
def _machines_env(monkeypatch, tmp_path):
    monkeypatch.setattr(machines, "MACHINES_DIR", str(tmp_path / "machines"))
    monkeypatch.setattr(machines, "MACHINE_ID_FILE", str(tmp_path / "cache" / "machine_id"))


@pytest.fixture(autouse=True)
def _stub_title_resolution(monkeypatch):
    """Publish resolves titles through the listing's own machinery; the
    machines tests stub it so they pin the index contract, not every
    tool's title reader."""
    monkeypatch.setattr(sessions, "_resolve_title_and_cwd",
                        lambda r: (r.get("title") or "(no title)",
                                   r.get("cwd") or "?"))


def light(tool="claude", sid="a", title=None, cwd=None, ts=0):
    return {"tool": tool, "id": sid, "title": title, "cwd": cwd, "ts": ts}


def write_foreign(name, body):
    os.makedirs(machines.MACHINES_DIR, exist_ok=True)
    with open(os.path.join(machines.MACHINES_DIR, name), "w", encoding="utf-8") as fh:
        json.dump(body, fh)


def read_index(name):
    with open(os.path.join(machines.MACHINES_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def foreign_index(machine="kestrel", sessions_data=None, machine_id="other", **overrides):
    body = {
        "schema": machines.SCHEMA,
        "machine": machine,
        "machine_id": machine_id,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cw_version": "0.0.1",
        "sessions": sessions_data if sessions_data is not None else [],
    }
    body.update(overrides)
    return body


# ---------- machine identity ----------

def test_machine_name_sanitizes_the_hostname(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Hunt's MacBook Pro")
    assert machines.machine_name() == "Hunt-s-MacBook-Pro"


def test_an_unnameable_host_falls_back(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert machines.machine_name() == "machine"


def test_the_name_override_is_sanitized_too(monkeypatch):
    # it becomes a filename in a directory other machines read
    monkeypatch.setenv("CROSSWEAVE_MACHINE_NAME", "lab / tmp")
    assert machines.machine_name() == "lab---tmp"
    monkeypatch.setenv("CROSSWEAVE_MACHINE_NAME", "###")
    assert machines.machine_name() == "machine"


def test_machine_id_is_minted_once_and_stable():
    first = machines.machine_id()
    assert len(first) == 32 and int(first, 16) >= 0
    assert machines.machine_id() == first
    with open(machines.MACHINE_ID_FILE, encoding="utf-8") as fh:
        assert fh.read().strip() == first


# ---------- publish ----------

def test_publish_is_a_noop_while_the_directory_does_not_exist(tmp_path):
    assert machines.write_own_index([light()]) is None
    assert os.listdir(tmp_path) == []  # nothing created, not even the dir


def test_publish_writes_a_self_describing_index():
    os.makedirs(machines.MACHINES_DIR)
    path = machines.write_own_index([light(sid="s1", title="fix the lock", cwd="/w", ts=50)])

    assert path == machines.own_index_path()
    data = read_index(os.path.basename(path))
    assert data["schema"] == machines.SCHEMA
    assert data["machine"] == machines.machine_name()
    assert data["machine_id"] == machines.machine_id()
    datetime.datetime.fromisoformat(data["generated_at"])  # parseable is the contract
    assert data["sessions"] == [
        {"tool": "claude", "id": "s1", "title": "fix the lock", "cwd": "/w", "ts": 50},
    ]


def test_publish_is_newest_first_and_capped(monkeypatch):
    monkeypatch.setattr(machines, "INDEX_CAP", 3)
    os.makedirs(machines.MACHINES_DIR)
    machines.write_own_index([light(sid=f"s{n}", ts=n) for n in range(5)])

    data = read_index(machines.machine_name() + ".json")
    assert [s["id"] for s in data["sessions"]] == ["s4", "s3", "s2"]


def test_publish_skips_the_rows_the_default_listing_hides():
    os.makedirs(machines.MACHINES_DIR)
    machines.write_own_index([light(sid="tool-started",
                                    title=sessions.TOOL_STARTED_TITLES[0]),
                              light(sid="real", title="real work")])

    data = read_index(machines.machine_name() + ".json")
    assert [s["id"] for s in data["sessions"]] == ["real"]


def test_publish_collapses_duplicate_ids_keeping_the_newest():
    os.makedirs(machines.MACHINES_DIR)
    machines.write_own_index([light(sid="s1", ts=10), light(sid="s1", ts=99)])

    data = read_index(machines.machine_name() + ".json")
    assert [s["ts"] for s in data["sessions"]] == [99]


def test_a_record_that_fails_resolution_loses_its_title_not_the_row(monkeypatch):
    def explodes(r):
        if r["id"] == "bad":
            raise TypeError("dict where a string should be")
        return (r.get("title") or "(no title)", r.get("cwd") or "?")

    monkeypatch.setattr(sessions, "_resolve_title_and_cwd", explodes)
    os.makedirs(machines.MACHINES_DIR)
    machines.write_own_index([light(sid="bad"), light(sid="good", title="ok")])

    data = read_index(machines.machine_name() + ".json")
    by_id = {s["id"]: s for s in data["sessions"]}
    assert by_id["bad"]["title"] == "(no title)"
    assert by_id["good"]["title"] == "ok"


# ---------- read ----------

def test_reading_without_the_directory_is_an_empty_answer():
    assert machines.read_remote_indexes() == []


def test_the_local_machines_own_file_is_skipped_silently(capsys):
    os.makedirs(machines.MACHINES_DIR)
    machines.write_own_index([light()])

    assert machines.read_remote_indexes() == []
    assert capsys.readouterr().err == ""


def test_an_unknown_schema_is_skipped_with_a_note(capsys):
    write_foreign("old.json", foreign_index(schema=99))

    assert machines.read_remote_indexes() == []
    assert "unknown schema" in capsys.readouterr().err


def test_a_torn_or_foreign_file_is_skipped_silently(capsys):
    os.makedirs(machines.MACHINES_DIR)
    with open(os.path.join(machines.MACHINES_DIR, "torn.json"), "w") as fh:
        fh.write('{"schema": 1, "mach')  # cut mid-write by the sync tool

    assert machines.read_remote_indexes() == []
    assert capsys.readouterr().err == ""


def test_an_unreadable_generated_at_is_skipped_with_a_note(capsys):
    write_foreign("weird.json", foreign_index(generated_at="not a date"))

    assert machines.read_remote_indexes() == []
    assert "unreadable generated_at" in capsys.readouterr().err


def test_a_stale_index_is_skipped_with_a_note(capsys):
    now = datetime.datetime.now().astimezone()
    stale = (now - datetime.timedelta(days=15)).isoformat(timespec="seconds")
    write_foreign("ghost.json", foreign_index(generated_at=stale))

    assert machines.read_remote_indexes(now=now) == []
    assert "days old" in capsys.readouterr().err


def test_the_staleness_boundary_is_inclusive():
    now = datetime.datetime.now().astimezone()
    fresh = (now - datetime.timedelta(seconds=machines.STALE_MACHINE_SECONDS - 60))
    write_foreign("fresh.json", foreign_index(machine="fresh",
                                              generated_at=fresh.isoformat(timespec="seconds")))

    found = machines.read_remote_indexes(now=now)
    assert [f["machine"] for f in found] == ["fresh"]


def test_malformed_session_rows_are_dropped_and_the_machine_name_falls_back():
    write_foreign("machine.json", foreign_index(machine="", sessions_data=[
        "not a dict",
        {"tool": "claude"},                                # no id
        {"id": "x"},                                       # no tool
        {"tool": "codex", "id": "ok1", "title": 7, "cwd": None, "ts": "x"},
        {"tool": "kimi", "id": "ok2", "title": "real", "cwd": "/w", "ts": 5},
    ]))

    found = machines.read_remote_indexes()
    assert found[0]["machine"] == "machine"  # the filename, sans .json
    assert found[0]["sessions"] == [
        {"tool": "codex", "id": "ok1", "title": "(no title)", "cwd": "?", "ts": 0},
        {"tool": "kimi", "id": "ok2", "title": "real", "cwd": "/w", "ts": 5},
    ]


# ---------- merge ----------

def test_merge_drops_sessions_the_local_store_also_holds():
    """A mirrored machine's rows are local rows: resumable, handoff-able.
    The remote entry carries nothing the local one lacks."""
    indexes = [{"machine": "kestrel",
                "sessions": [light(sid="mirrored"), light(sid="only-there")]}]

    merged = machines.remote_sessions(indexes, {("claude", "mirrored")})

    assert [(s["id"], s["machine"]) for s in merged] == [("only-there", "kestrel")]


def test_merge_dedupes_two_remote_machines_sharing_a_store():
    indexes = [
        {"machine": "zulu", "sessions": [light(sid="shared", ts=10)]},
        {"machine": "alfa", "sessions": [light(sid="shared", ts=99)]},
    ]

    merged = machines.remote_sessions(indexes, set())

    # newest ts wins; whichever index arrived first
    assert [(s["machine"], s["ts"]) for s in merged] == [("alfa", 99)]


def test_merge_orders_newest_first_with_a_deterministic_tiebreak():
    indexes = [{"machine": "kestrel", "sessions": [
        light(sid="b", ts=5), light(sid="a", ts=5), light(sid="c", ts=9),
    ]}]

    merged = machines.remote_sessions(indexes, set())

    assert [s["id"] for s in merged] == ["c", "a", "b"]


# ---------- the listing wiring ----------

def _fake_scanners(monkeypatch, records, called=None):
    """Each tool's light-record reader returns the records that name it;
    `called` collects which gatherers ran."""
    def make(tool):
        def gather(show_all=False):
            if called is not None:
                called.add(tool)
            return [dict(r) for r in records if r.get("tool") == tool]
        return gather
    monkeypatch.setattr(sessions, "claude_light_records", make("claude"))
    monkeypatch.setattr(sessions, "codex_light_records", make("codex"))
    monkeypatch.setattr(sessions, "kimi_light_records", make("kimi"))
    monkeypatch.setattr(sessions, "step_light_records", make("step"))
    monkeypatch.setattr(sessions, "zcode_light_records", make("zcode"))
    monkeypatch.setattr(sessions, "codebuddy_light_records", make("codebuddy"))


ALL_TOOLS = {"claude", "codex", "kimi", "step", "zcode", "codebuddy"}


def test_a_plain_listing_publishes_the_index(monkeypatch):
    os.makedirs(machines.MACHINES_DIR)
    _fake_scanners(monkeypatch, [light(sid="s1", title="local work", ts=100)])

    sessions.cmd_list([])

    data = read_index(machines.machine_name() + ".json")
    assert data["sessions"] == [
        {"tool": "claude", "id": "s1", "title": "local work", "cwd": "?", "ts": 100},
    ]


def test_without_the_directory_a_tool_filter_gathers_only_that_tool(monkeypatch):
    called = set()
    _fake_scanners(monkeypatch, [], called)

    sessions.cmd_list(["--tool", "codex"])

    assert called == {"codex"}  # today's cheap gather, byte for byte


def test_with_the_directory_a_tool_filter_still_publishes_everything(monkeypatch):
    os.makedirs(machines.MACHINES_DIR)
    called = set()
    _fake_scanners(monkeypatch, [light(sid="s")], called)

    sessions.cmd_list(["--tool", "codex"])

    assert called == ALL_TOOLS  # the index describes the machine, not the question
    data = read_index(machines.machine_name() + ".json")
    assert [s["id"] for s in data["sessions"]] == ["s"]


def test_the_machines_flag_merges_and_the_cache_covers_the_union(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    os.makedirs(machines.MACHINES_DIR)
    _fake_scanners(monkeypatch, [light(sid="s1", title="local work", ts=100),
                                 light(sid="m1", title="mirrored work", ts=90)])
    write_foreign("kestrel.json", foreign_index(machine="kestrel", sessions_data=[
        {"tool": "claude", "id": "m1", "title": "mirrored work", "cwd": "/w", "ts": 90},
        {"tool": "codex", "id": "r1", "title": "remote only", "cwd": "/w", "ts": 80},
    ]))

    sessions.cmd_list(["--machines"])

    out = capsys.readouterr().out
    assert "on other machines:" in out
    assert "remote only" in out and "kestrel" in out
    # a row the local store also holds is a local row, never printed twice
    assert out.count("m1") == 1
    assert sessions.read_list_cache() == [
        {"tool": "claude", "id": "s1"},
        {"tool": "claude", "id": "m1"},
        {"tool": "codex", "id": "r1", "machine": "kestrel"},
    ]


def test_a_remote_row_resumes_as_a_clean_refusal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    with open(sessions.LIST_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump([{"tool": "codex", "id": "r1", "machine": "kestrel"}], fh)

    with pytest.raises(SystemExit):
        sessions.resume_by_number(1, [])

    assert "lives on kestrel" in capsys.readouterr().err


def test_a_remote_row_handsoff_as_a_clean_refusal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    with open(sessions.LIST_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump([{"tool": "codex", "id": "r1", "machine": "kestrel"}], fh)

    with pytest.raises(SystemExit):
        sessions.handoff_by_number(1, "codex", [])

    assert "lives on kestrel" in capsys.readouterr().err
