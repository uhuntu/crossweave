"""With CW_LLM_WIKI set, ordinary `cw` use keeps the wiki fed: a background
`cw export --all` once a day, never waited on."""
import json
import sys
import time

import pytest

from crossweave import autoexport, cli, exporter

# Taken before any fixture replaces it.
REAL_SPAWN = autoexport._spawn


@pytest.fixture
def wiki(monkeypatch, tmp_path):
    root = tmp_path / 'my-wiki'
    (root / 'wiki').mkdir(parents=True)
    (root / 'raw' / 'sources').mkdir(parents=True)
    monkeypatch.setenv('CW_LLM_WIKI', str(root))
    monkeypatch.delenv(autoexport.ENV, raising=False)
    monkeypatch.delenv(autoexport.CHILD_ENV, raising=False)
    spawned = []
    monkeypatch.setattr(autoexport, '_spawn', spawned.append)
    return root, spawned


def state(root):
    return json.loads(autoexport.state_path().read_text(encoding='utf-8'))[autoexport._key(root)]


def test_a_due_export_starts_in_the_background_and_says_so(wiki, capsys):
    root, spawned = wiki
    autoexport.maybe_start()
    assert spawned == [root]
    assert state(root)['started'] > time.time() - 5
    assert 'exporting quiet sessions to LLM Wiki in the background' in capsys.readouterr().err
    autoexport.maybe_start()  # running now, and within the day anyway
    assert spawned == [root]


def test_the_next_one_waits_for_the_interval(wiki, monkeypatch):
    root, spawned = wiki
    monkeypatch.setenv(autoexport.ENV, '12h')
    autoexport._write_state({autoexport._key(root): {
        'started': time.time() - 11 * 3600, 'finished': time.time() - 11 * 3600, 'result': 'ok'}})
    autoexport.maybe_start()
    assert spawned == []
    autoexport._write_state({autoexport._key(root): {
        'started': time.time() - 13 * 3600, 'finished': time.time() - 13 * 3600, 'result': 'ok'}})
    autoexport.maybe_start()
    assert spawned == [root]


def test_a_run_that_never_finished_is_not_waited_on_forever(wiki):
    root, spawned = wiki
    long_ago = time.time() - 2 * 86400
    autoexport._write_state({autoexport._key(root): {'started': long_ago, 'finished': long_ago - 86400}})
    autoexport.maybe_start()
    assert spawned == [root]


@pytest.mark.parametrize('setting', ['off', '0', 'no'])
def test_it_can_be_turned_off(wiki, monkeypatch, setting):
    root, spawned = wiki
    monkeypatch.setenv(autoexport.ENV, setting)
    autoexport.maybe_start()
    assert spawned == []


def test_an_unreadable_interval_is_said_and_treated_as_off(wiki, monkeypatch, capsys):
    root, spawned = wiki
    monkeypatch.setenv(autoexport.ENV, 'daily')
    autoexport.maybe_start()
    assert spawned == []
    assert 'not a span' in capsys.readouterr().err


def test_nothing_without_a_wiki_or_inside_the_export_itself(wiki, monkeypatch):
    root, spawned = wiki
    monkeypatch.setenv(autoexport.CHILD_ENV, '1')
    autoexport.maybe_start()
    monkeypatch.delenv(autoexport.CHILD_ENV)
    monkeypatch.delenv('CW_LLM_WIKI')
    autoexport.maybe_start()
    assert spawned == []


def test_a_failed_run_is_named_once(wiki, capsys):
    root, spawned = wiki
    now = time.time()
    autoexport._write_state({autoexport._key(root): {
        'started': now - 60, 'finished': now - 30, 'result': 'failed',
        'error': 'LLM Wiki API returned HTTP 401'}})
    autoexport.maybe_start()
    err = capsys.readouterr().err
    assert 'last LLM Wiki auto-export failed (LLM Wiki API returned HTTP 401)' in err
    assert str(autoexport.log_path()) in err
    autoexport.maybe_start()
    assert 'failed' not in capsys.readouterr().err


def test_the_run_reaches_back_to_what_the_last_success_could_not_take(wiki, monkeypatch):
    root, _ = wiki
    seen = []
    def export_all(opts, client):
        seen.append(opts)
        return 0
    monkeypatch.setattr(exporter, '_export_all', export_all)
    assert autoexport.run(root) == 0
    first = seen[-1]
    assert (first.since, first.older_than, first.yes, first.rescan) == ('168h', '1d', True, True)
    mine = state(root)
    assert mine['result'] == 'ok' and mine['succeeded']
    # Ten days later: the window covers the ten days and the quiet day before them.
    mine['succeeded'] -= 10 * 86400
    mine['started'] = time.time()
    autoexport._write_state({autoexport._key(root): mine})
    assert autoexport.run(root) == 0
    assert int(seen[-1].since[:-1]) * 3600 >= 11 * 86400


def test_a_failed_run_is_recorded_with_its_reason(wiki, monkeypatch):
    root, _ = wiki
    def export_all(opts, client):
        print('cw export: 3 exported, 0 unchanged, 0 failed', file=sys.stderr)
        print('cw export: LLM Wiki API unavailable; start the app', file=sys.stderr)
        print('The snapshots are saved.', file=sys.stderr)
        return 1
    monkeypatch.setattr(exporter, '_export_all', export_all)
    assert autoexport.run(root) == 1
    assert state(root)['result'] == 'failed'
    assert state(root)['error'] == 'LLM Wiki API unavailable; start the app'
    assert 'succeeded' not in state(root)
    assert not isinstance(sys.stderr, autoexport._LastError)


@pytest.mark.parametrize('argv', [[], ['sessions'], ['full']])
def test_a_listing_checks_for_a_due_export(wiki, monkeypatch, argv):
    root, spawned = wiki
    monkeypatch.setattr(cli.sessions, 'cmd_list', lambda args: None)
    monkeypatch.setattr(sys, 'argv', ['cw', *argv])
    cli.main()
    assert spawned == [root]


def test_a_fresh_session_checks_too(wiki, monkeypatch):
    root, spawned = wiki
    monkeypatch.setattr(cli.sessions, 'exec_or_die', lambda cmd: None)
    monkeypatch.setattr(sys, 'argv', ['cw', 'claude'])
    cli.main()
    assert spawned == [root]


def test_the_spawned_process_is_detached_and_logs(wiki, monkeypatch):
    root, _ = wiki
    calls = []
    monkeypatch.setattr(autoexport.subprocess, 'Popen', lambda argv, **kw: calls.append((argv, kw)))
    REAL_SPAWN(root)
    assert autoexport.log_path().exists()
    assert str(autoexport.log_path()).startswith(str(root.parent))  # the test's, not the machine's
    argv, kw = calls[0]
    assert argv[0] == sys.executable and 'autoexport.run' in argv[2]
    assert kw['env'][autoexport.CHILD_ENV] == '1'
    assert kw['stdin'] is autoexport.subprocess.DEVNULL
    assert ('start_new_session' in kw) != ('creationflags' in kw)


def test_a_failed_run_is_tried_again_within_the_hour(wiki, capsys):
    root, spawned = wiki
    now = time.time()
    autoexport._write_state({autoexport._key(root): {
        'started': now - 1800, 'finished': now - 1790, 'result': 'failed', 'notified': True}})
    autoexport.maybe_start()
    assert spawned == []
    autoexport._write_state({autoexport._key(root): {
        'started': now - 3700, 'finished': now - 3690, 'result': 'failed', 'notified': True}})
    autoexport.maybe_start()
    assert spawned == [root]
