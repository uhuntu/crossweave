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
import subprocess
import sys

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
    monkeypatch.setattr(sessions, "workbuddy_light_records", make("workbuddy"))


ALL_TOOLS = {"claude", "codex", "kimi", "step", "zcode", "codebuddy", "workbuddy"}


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


def _remote_cache(tmp_path, monkeypatch, **extra):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    with open(sessions.LIST_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump([dict({"tool": "codex", "id": "r1", "machine": "kestrel"}, **extra)], fh)


def _remote_ssh(monkeypatch, probe_stdout="pre%OS%post", rc=0):
    """Fake the probe (subprocess.run) and the interactive ssh
    (subprocess.call); returns the interactive argv list."""
    monkeypatch.setattr(machines.subprocess, "run",
                        lambda argv, **kw: _Proc(0, stdout=probe_stdout))
    calls = []

    def fake_call(argv):
        calls.append(argv)
        return rc

    monkeypatch.setattr(machines.subprocess, "call", fake_call)
    return calls


def test_a_remote_row_resumes_there_over_ssh(monkeypatch, tmp_path, capsys):
    _remote_cache(tmp_path, monkeypatch)
    calls = _remote_ssh(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        sessions.resume_by_number(1, [])

    assert exc.value.code == 0
    argv = calls[0]
    # interactive: a tty, no BatchMode (the real login may ask), and the
    # machine's own name as the destination for a sync-dir row
    assert argv[:6] == ["ssh", "-o", "ConnectTimeout=8", "-o", "ExitOnForwardFailure=no", "-t"]
    assert "BatchMode=yes" not in argv
    assert argv[6] == "kestrel"
    assert argv[7].startswith("sh -c ") and "resume codex r1" in argv[7]
    assert "opening it there" in capsys.readouterr().err


def test_a_pulled_row_goes_back_through_the_target_it_came_from(monkeypatch, tmp_path):
    _remote_cache(tmp_path, monkeypatch, ssh="241-tunnel")
    calls = _remote_ssh(monkeypatch)
    with pytest.raises(SystemExit):
        sessions.resume_by_number(1, [])
    assert calls[0][6] == "241-tunnel"


def test_words_after_the_row_ride_along(monkeypatch, tmp_path):
    _remote_cache(tmp_path, monkeypatch)
    calls = _remote_ssh(monkeypatch)
    with pytest.raises(SystemExit):
        sessions.resume_by_number(1, ["keep going; don't stop"])
    assert "keep going" in calls[0][7]


def test_a_windows_host_gets_the_cmd_launcher(monkeypatch, tmp_path):
    _remote_cache(tmp_path, monkeypatch)
    calls = _remote_ssh(monkeypatch, probe_stdout="preWindows_NTpost")
    with pytest.raises(SystemExit):
        sessions.resume_by_number(1, [])
    assert calls[0][7] == '"%USERPROFILE%\\.local\\bin\\cw.cmd" resume codex r1'


def test_cmd_refuses_words_it_cannot_quote(monkeypatch, tmp_path, capsys):
    _remote_cache(tmp_path, monkeypatch)
    calls = _remote_ssh(monkeypatch, probe_stdout="preWindows_NTpost")
    with pytest.raises(SystemExit) as exc:
        sessions.resume_by_number(1, ["a & b"])
    assert exc.value.code == 1 and calls == []
    assert "cmd.exe" in capsys.readouterr().err


def test_a_local_cwd_does_not_travel(monkeypatch, tmp_path, capsys):
    _remote_cache(tmp_path, monkeypatch)
    calls = _remote_ssh(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        sessions.resume_by_number(1, [], forced_cwd=str(tmp_path))
    assert exc.value.code == 1 and calls == []
    assert "lives on kestrel" in capsys.readouterr().err


def test_an_unreachable_machine_name_earns_the_ssh_config_hint(monkeypatch, tmp_path, capsys):
    _remote_cache(tmp_path, monkeypatch)
    _remote_ssh(monkeypatch, rc=255)
    with pytest.raises(SystemExit) as exc:
        sessions.resume_by_number(1, [])
    assert exc.value.code == 255
    assert "Host kestrel" in capsys.readouterr().err


def test_the_session_exit_code_is_not_mistaken_for_ssh_failing(monkeypatch, tmp_path, capsys):
    _remote_ssh(monkeypatch, rc=3)
    _remote_cache(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        sessions.resume_by_number(1, [])
    assert exc.value.code == 3
    assert "Host" not in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="runs the POSIX command in sh")
def test_the_posix_command_finds_cw_off_path_and_keeps_words_whole(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    out = tmp_path / "args.txt"
    fake = home / ".local" / "bin" / "cw"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}\n')
    fake.chmod(0o755)
    command = machines.remote_resume_command("codex", "r1", ["it's a $test"], False)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"}
    subprocess.run(["sh", "-c", command], env=env, check=True, timeout=20)
    assert out.read_text().splitlines() == ["resume", "codex", "r1", "it's a $test"]


def test_a_remote_row_handsoff_as_a_clean_refusal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    with open(sessions.LIST_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump([{"tool": "codex", "id": "r1", "machine": "kestrel"}], fh)

    with pytest.raises(SystemExit):
        sessions.handoff_by_number(1, "codex", [])

    assert "lives on kestrel" in capsys.readouterr().err


# ---------- the ssh pull mode ----------

def remote_payload(machine="kestrel", machine_id="f" * 32):
    return {"schema": machines.SCHEMA, "machine": machine, "machine_id": machine_id,
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "cw_version": "0.4.0",
            "sessions": [{"tool": "codex", "id": "r1", "title": "pulled row",
                          "cwd": "/w", "ts": 80}]}


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ssh_runs(monkeypatch, results):
    """results: one entry per ssh attempt the fake allows, starting with
    the cmd-vs-POSIX probe. Returns the list of command argvs the pull
    actually tried."""
    calls = []
    queue = list(results)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(machines.subprocess, "run", fake_run)
    return calls


def _posix_probe():
    return _Proc(0, stdout="pre%OS%post")  # %OS% unexpanded: a POSIX shell


def test_pull_probes_the_shell_then_uses_the_first_attempt_that_answers(monkeypatch, capsys):
    calls = _ssh_runs(monkeypatch, [_posix_probe(),
                                    _Proc(0, stdout=json.dumps(remote_payload()))])

    found = machines.pull_remote_indexes(["kestrel"])

    assert [s["title"] for s in found[0]["sessions"]] == ["pulled row"]
    assert len(calls) == 2
    assert calls[0][8] == machines._WIN_PROBE
    # the ssh shape: BatchMode (never hang at a prompt), connect timeout,
    # the target verbatim -- a config alias works as-is
    assert calls[0][:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            "-o", "ClearAllForwardings=yes"]
    assert calls[0][7] == "kestrel"
    assert calls[1][8] == "cw machines"
    assert capsys.readouterr().err == ""


def test_a_windows_host_gets_the_cmd_form(monkeypatch, capsys):
    calls = _ssh_runs(monkeypatch, [
        _Proc(0, stdout="preWindows_NTpost"),  # %OS% expanded: cmd.exe
        _Proc(0, stdout=json.dumps(remote_payload())),
    ])

    found = machines.pull_remote_indexes(["desktop"])

    assert found and "cw.cmd" in calls[1][8]
    assert "%USERPROFILE%" in calls[1][8]  # only cmd.exe expands it
    assert capsys.readouterr().err == ""


def test_pull_falls_through_a_host_that_lacks_the_command(monkeypatch, capsys):
    calls = _ssh_runs(monkeypatch, [
        _posix_probe(),
        _Proc(127, stderr="bash: cw: command not found"),
        _Proc(0, stdout=json.dumps(remote_payload())),
    ])

    found = machines.pull_remote_indexes(["kestrel"])

    assert len(calls) == 3 and found  # the login-shell attempt answered
    assert "sh -lc" in calls[2][8]


def test_an_old_remote_cw_earns_an_actionable_hint(monkeypatch, capsys):
    old_cw = "cw: unknown tool 'machines' (expected one of claude, ...)"
    _ssh_runs(monkeypatch, [
        _posix_probe(),
        _Proc(127, stderr="bash: cw: command not found"),
        _Proc(1, stderr=old_cw),
        _Proc(1, stderr=old_cw),
        _Proc(1, stderr=old_cw),
    ])

    assert machines.pull_remote_indexes(["kestrel"]) == []
    assert "run `cw update` on it" in capsys.readouterr().err
    # the remote's own words are never shown for this one: they say what
    # happened, not what to do


def test_pull_survives_a_dead_host_at_the_probe(monkeypatch, capsys):
    _ssh_runs(monkeypatch, [subprocess.TimeoutExpired("ssh", 15)])

    assert machines.pull_remote_indexes(["ghost"]) == []
    assert "ghost: timed out" in capsys.readouterr().err


def test_pull_says_why_a_host_answered_without_an_index(monkeypatch, capsys):
    _ssh_runs(monkeypatch,
              [_posix_probe()] + [_Proc(0, stdout="hello, I am not json")] * 4)

    assert machines.pull_remote_indexes(["weird"]) == []
    assert "weird:" in capsys.readouterr().err


def test_pulling_oneself_is_silently_nothing(monkeypatch, capsys):
    calls = _ssh_runs(monkeypatch, [
        _posix_probe(),
        _Proc(0, stdout=json.dumps(remote_payload(machine_id=machines.machine_id()))),
    ])

    assert machines.pull_remote_indexes(["self"]) == []
    assert len(calls) == 2  # recognized at first sight, no cascade
    assert capsys.readouterr().err == ""


def test_the_machines_subcommand_prints_the_index(capsys):
    assert machines.cmd_machines([]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == machines.SCHEMA
    assert data["machine"] == machines.machine_name()

    assert machines.cmd_machines(["wat"]) == 2
    assert "takes no arguments" in capsys.readouterr().err


def test_the_listing_pulls_named_ssh_targets(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    os.makedirs(machines.MACHINES_DIR)  # dir exists too: both transports merge
    _fake_scanners(monkeypatch, [light(sid="s1", title="local work", ts=100)])
    pulled = [{
        "machine": "kestrel", "machine_id": "f" * 32, "path": "ssh kestrel",
        "generated_at": datetime.datetime.now().astimezone(),
        "sessions": [{"tool": "codex", "id": "r1", "title": "pulled row",
                      "cwd": "/w", "ts": 80}],
    }]
    seen = {}
    def fake_pull(targets, **kw):
        seen["targets"] = targets
        return pulled

    monkeypatch.setattr(machines, "pull_remote_indexes", fake_pull)

    sessions.cmd_list(["--machines", "kestrel", "ghost"])

    assert seen["targets"] == ["kestrel", "ghost"]
    out = capsys.readouterr().out
    assert "pulled row" in out and "kestrel" in out
    assert sessions.read_list_cache()[-1] == {"tool": "codex", "id": "r1", "machine": "kestrel"}


def test_a_pulled_row_remembers_its_ssh_target_in_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    _fake_scanners(monkeypatch, [])
    index = {"machine": "kestrel", "machine_id": "f" * 32, "path": "ssh 241-tunnel",
             "ssh": "241-tunnel", "generated_at": datetime.datetime.now().astimezone(),
             "sessions": [{"tool": "codex", "id": "r1", "title": "t", "cwd": "/w", "ts": 80}]}
    monkeypatch.setattr(machines, "pull_remote_indexes", lambda targets, **kw: [index])
    sessions.cmd_list(["--machines", "241-tunnel"])
    assert sessions.read_list_cache()[-1]["ssh"] == "241-tunnel"


def test_the_remote_section_gets_the_same_slice_as_the_local_one(monkeypatch, tmp_path, capsys):
    """One busy machine near its index cap must not drown the table: the
    remote slice matches --limit, and the overflow says where it went."""
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    _fake_scanners(monkeypatch, [light(sid="s1", title="local work", ts=100)])
    pulled = [{
        "machine": "swc-build", "machine_id": "f" * 32, "path": "ssh swc-build",
        "generated_at": datetime.datetime.now().astimezone(),
        "sessions": [{"tool": "codex", "id": f"r{n}", "title": f"remote {n}",
                      "cwd": "/w", "ts": 80 - n} for n in range(5)],
    }]
    monkeypatch.setattr(machines, "pull_remote_indexes", lambda targets, **kw: pulled)

    sessions.cmd_list(["--machines", "swc-build", "--limit", "2"])

    out = capsys.readouterr().out
    assert "and 3 more remote sessions not shown" in out
    assert "remote 1" in out and "remote 4" not in out  # the newest two stayed
    # the cache covers exactly what was printed: the row numbers hold
    assert sessions.read_list_cache() == [
        {"tool": "claude", "id": "s1"},
        {"tool": "codex", "id": "r0", "machine": "swc-build"},
        {"tool": "codex", "id": "r1", "machine": "swc-build"},
    ]


def test_machine_targets_stop_at_the_next_flag(monkeypatch):
    _fake_scanners(monkeypatch, [])
    seen = {}

    def fake_pull(targets, **kw):
        seen["targets"] = targets
        return []

    monkeypatch.setattr(machines, "pull_remote_indexes", fake_pull)

    sessions.cmd_list(["--machines", "kestrel", "ghost", "--tool", "codex"])

    assert seen["targets"] == ["kestrel", "ghost"]
