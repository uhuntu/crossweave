"""An ordinary `cw` keeps the vault up to date once a day, in the
background: notes for quiet sessions, the project pages they put behind,
and -- with CW_LLM_WIKI -- the LLM Wiki export. Never waited on."""
import json
import sys
import time

import pytest

from crossweave import autoexport, cli, exporter, note, wiki

# Taken before any fixture replaces it.
REAL_SPAWN = autoexport._spawn


@pytest.fixture
def vault(monkeypatch, tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setenv("CW_VAULT", str(v))
    monkeypatch.delenv(autoexport.ENV, raising=False)
    monkeypatch.delenv(autoexport.WIKI_ENV, raising=False)
    monkeypatch.delenv(autoexport.CHILD_ENV, raising=False)
    monkeypatch.delenv("CW_LLM_WIKI", raising=False)
    spawned = []
    monkeypatch.setattr(autoexport, "_spawn", lambda: spawned.append(1))
    return v, spawned


@pytest.fixture
def llm_wiki(vault, monkeypatch, tmp_path):
    root = tmp_path / "my-wiki"
    (root / "wiki").mkdir(parents=True)
    (root / "raw" / "sources").mkdir(parents=True)
    monkeypatch.setenv("CW_LLM_WIKI", str(root))
    return root


def state():
    return json.loads(autoexport.state_path().read_text(encoding="utf-8"))[autoexport.KEY]


# ---------- when it starts ----------

def test_a_vault_alone_is_enough_and_the_start_is_said(vault, capsys):
    _, spawned = vault
    autoexport.maybe_start()
    assert spawned == [1]
    assert state()["started"] > time.time() - 5
    err = capsys.readouterr().err
    assert "daily upkeep in the background -- notes, project pages (log:" in err
    assert "LLM Wiki" not in err
    autoexport.maybe_start()  # running now, and within the day anyway
    assert spawned == [1]


def test_the_llm_wiki_step_is_named_when_there_is_one(llm_wiki, vault, capsys):
    autoexport.maybe_start()
    assert "notes, project pages, LLM Wiki" in capsys.readouterr().err


def test_llm_wiki_export_off_leaves_the_vault_upkeep_running(llm_wiki, vault, monkeypatch, capsys):
    monkeypatch.setenv(autoexport.WIKI_ENV, "off")
    autoexport.maybe_start()
    assert vault[1] == [1]
    assert "LLM Wiki" not in capsys.readouterr().err


def test_nothing_without_a_vault_or_a_wiki(monkeypatch, tmp_path):
    monkeypatch.setenv("CW_VAULT", str(tmp_path / "missing"))
    monkeypatch.delenv("CW_LLM_WIKI", raising=False)
    monkeypatch.delenv(autoexport.ENV, raising=False)
    spawned = []
    monkeypatch.setattr(autoexport, "_spawn", lambda: spawned.append(1))
    autoexport.maybe_start()
    assert spawned == []


def test_the_next_run_waits_for_the_interval(vault, monkeypatch):
    _, spawned = vault
    monkeypatch.setenv(autoexport.ENV, "12h")
    autoexport._write_state({autoexport.KEY: {
        "started": time.time() - 11 * 3600, "finished": time.time() - 11 * 3600, "result": "ok"}})
    autoexport.maybe_start()
    assert spawned == []
    autoexport._write_state({autoexport.KEY: {
        "started": time.time() - 13 * 3600, "finished": time.time() - 13 * 3600, "result": "ok"}})
    autoexport.maybe_start()
    assert spawned == [1]


def test_a_run_that_never_finished_is_not_waited_on_forever(vault):
    _, spawned = vault
    long_ago = time.time() - 2 * 86400
    autoexport._write_state({autoexport.KEY: {"started": long_ago, "finished": long_ago - 86400}})
    autoexport.maybe_start()
    assert spawned == [1]


@pytest.mark.parametrize("setting", ["off", "0", "no"])
def test_it_can_be_turned_off(vault, monkeypatch, setting):
    _, spawned = vault
    monkeypatch.setenv(autoexport.ENV, setting)
    autoexport.maybe_start()
    assert spawned == []


def test_an_unreadable_interval_is_said_and_treated_as_off(vault, monkeypatch, capsys):
    _, spawned = vault
    monkeypatch.setenv(autoexport.ENV, "daily")
    autoexport.maybe_start()
    assert spawned == []
    assert "not a span" in capsys.readouterr().err


def test_nothing_inside_the_upkeep_itself(vault, monkeypatch):
    _, spawned = vault
    monkeypatch.setenv(autoexport.CHILD_ENV, "1")
    autoexport.maybe_start()
    assert spawned == []


def test_a_failed_run_is_named_once_and_tried_again_within_the_hour(vault, capsys):
    _, spawned = vault
    now = time.time()
    autoexport._write_state({autoexport.KEY: {
        "started": now - 1800, "finished": now - 1790, "result": "failed",
        "error": "1 note(s) not written: judge unavailable"}})
    autoexport.maybe_start()
    err = capsys.readouterr().err
    assert "last daily upkeep failed (1 note(s) not written: judge unavailable)" in err
    assert spawned == []
    autoexport.maybe_start()
    assert "failed" not in capsys.readouterr().err
    autoexport._write_state({autoexport.KEY: {
        "started": now - 3700, "finished": now - 3690, "result": "failed", "notified": True}})
    autoexport.maybe_start()
    assert spawned == [1]


@pytest.mark.parametrize("argv", [[], ["sessions"], ["full"]])
def test_a_listing_checks_for_due_upkeep(vault, monkeypatch, argv):
    _, spawned = vault
    monkeypatch.setattr(cli.sessions, "cmd_list", lambda args: None)
    monkeypatch.setattr(sys, "argv", ["cw", *argv])
    cli.main()
    assert spawned == [1]


def test_a_fresh_session_checks_too(vault, monkeypatch):
    _, spawned = vault
    monkeypatch.setattr(cli.sessions, "exec_or_die", lambda cmd: None)
    monkeypatch.setattr(sys, "argv", ["cw", "claude"])
    cli.main()
    assert spawned == [1]


def test_the_spawned_process_is_detached_and_logs(vault, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(autoexport.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    REAL_SPAWN()
    argv, kw = calls[0]
    assert argv[0] == sys.executable and "autoexport.run()" in argv[2]
    assert kw["env"][autoexport.CHILD_ENV] == "1"
    assert kw["stdin"] is autoexport.subprocess.DEVNULL
    assert ("start_new_session" in kw) != ("creationflags" in kw)
    assert str(autoexport.log_path()).startswith(str(tmp_path))  # the test's, not the machine's


# ---------- what a run does ----------

@pytest.fixture
def steps(vault, monkeypatch):
    """The three steps' machinery faked: two quiet units, one with a fresh note."""
    done = []
    units = [{"members": [{"tool": "zcode", "id": "a"}], "title": "A"},
             {"members": [{"tool": "zcode", "id": "b"}], "title": "B"}]
    monkeypatch.setattr(exporter, "pick_sessions", lambda since, under, quiet: (
        done.append(("pick", since, quiet)) or (units, {}, [])))
    monkeypatch.setattr(exporter, "notes_state", lambda v: {})
    monkeypatch.setattr(exporter, "fresh_note", lambda u, st: ({}, "x") if u["title"] == "A" else None)
    monkeypatch.setattr(exporter, "write_notes", lambda need, v, judge, explicit=False, prog="": (
        done.append(("notes", [u["title"] for u in need])) or ({id(need[0]): ({}, "y")}, [])))
    monkeypatch.setattr(wiki, "stale_projects", lambda v: ["board"])
    monkeypatch.setattr(wiki, "build_project", lambda v, name, judge: done.append(("project", name)) or "p")
    monkeypatch.setattr(exporter, "_export_all", lambda opts, client: done.append(("export", opts.content)) or 0)
    monkeypatch.setattr(note, "unsorted_home_notes", lambda v: ["one"])
    monkeypatch.setattr(note, "sort_home", lambda v, judge, yes=False: done.append(("sort", yes)) or [])
    return done


def test_a_run_writes_the_missing_notes_then_the_project_pages(steps, vault):
    assert autoexport.run() == 0
    assert steps[0] == ("pick", autoexport.FIRST_SPAN, 86400)
    assert steps[1:] == [("notes", ["B"]), ("sort", True), ("project", "board")]
    assert state()["result"] == "ok" and state()["succeeded"]


def test_with_an_llm_wiki_the_notes_are_exported_last(steps, llm_wiki):
    assert autoexport.run() == 0
    assert [s[0] for s in steps] == ["pick", "notes", "sort", "project", "export"]
    assert steps[-1] == ("export", exporter.CONTENT_NOTES)


def test_a_failed_note_marks_the_run_failed_but_the_next_steps_still_run(steps, vault, monkeypatch):
    monkeypatch.setattr(exporter, "write_notes", lambda need, v, judge, explicit=False, prog="": (
        {}, [(need[0], exporter.ExportError("judge unavailable"))]))
    assert autoexport.run() == 1
    assert ("project", "board") in steps
    assert state()["result"] == "failed"
    assert state()["error"] == "1 note(s) not written: judge unavailable"
    assert "succeeded" not in state()


def test_a_step_that_raises_does_not_stop_the_others(steps, vault, monkeypatch):
    def boom(*a):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(exporter, "pick_sessions", boom)
    assert autoexport.run() == 1
    assert ("project", "board") in steps
    assert "notes: RuntimeError: store unreadable" == state()["error"]


def test_the_run_reaches_back_to_what_the_last_success_could_not_take(steps, vault):
    autoexport._write_state({autoexport.KEY: {
        "started": time.time(), "succeeded": time.time() - 10 * 86400}})
    autoexport.run()
    assert steps[0][1] >= 11 * 86400
