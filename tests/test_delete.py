import json
import os
import sys

import pytest

from crossweave import cli, codex, common, delete, sessions


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, 'LIST_CACHE_FILE', str(tmp_path / 'cache' / 'list.json'))
    monkeypatch.setattr(common, 'CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setattr(codex, '_codex_path_index', None)
    sid = '12345678-1234-1234-1234-123456789abc'
    paths = []
    for day in ('01', '02'):
        folder = tmp_path / 'codex' / 'sessions' / day
        folder.mkdir(parents=True)
        path = folder / f'rollout-{sid}.jsonl'
        path.write_text('{"type":"session_meta"}\n')
        paths.append(path)
    sessions.write_list_cache([{'tool': 'codex', 'id': sid}])
    return sid, paths


def test_delete_all_rollouts_with_recoverable_backup(store, tmp_path):
    sid, paths = store
    assert delete.cmd_delete(['1', '-y']) == 0
    assert not any(p.exists() for p in paths)
    assert codex.codex_light_records() == []
    manifest_path = next((tmp_path / 'cache' / 'deleted').glob('*/manifest.json'))
    manifest = json.loads(manifest_path.read_text())
    assert manifest['id'] == sid
    assert {f['original'] for f in manifest['files']} == {str(p) for p in paths}
    assert all(os.path.isfile(f['backup']) for f in manifest['files'])
    assert sessions.read_list_cache()[0]['id'] == sid
    assert delete.cmd_delete(['1', '-y']) == 1


def test_no_confirmation_does_not_delete(store, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _: 'n')
    assert delete.cmd_delete(['1']) == 0
    assert all(p.exists() for p in store[1])


def test_noninteractive_requires_yes(store, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    assert delete.cmd_delete(['1']) == 1
    assert all(p.exists() for p in store[1])


def test_remote_refused(store):
    sessions.write_list_cache([{'tool': 'codex', 'id': store[0], 'machine': 'other'}])
    assert delete.cmd_delete(['1', '-y']) == 1
    assert all(p.exists() for p in store[1])


@pytest.mark.parametrize('args', [[], ['-1'], ['1', 'oops'], ['1', '2']])
def test_invalid_args(args):
    assert delete.cmd_delete(args) == 2


@pytest.mark.parametrize('args', [['cw', '1', 'delete', '-y'], ['cw', 'delete', '1', '-y']])
def test_cli_dispatch(args, monkeypatch):
    monkeypatch.setattr(sys, 'argv', args)
    called = []
    monkeypatch.setattr(delete, 'cmd_delete', lambda args: called.append(args) or 0)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert called == [['1', '-y']]


def test_partial_move_rolls_back(store, monkeypatch):
    real_move = delete.shutil.move
    calls = []

    def move(source, destination):
        calls.append(source)
        if len(calls) == 2:
            raise OSError('failed move')
        return real_move(source, destination)

    monkeypatch.setattr(delete.shutil, 'move', move)
    assert delete.cmd_delete(['1', '-y']) == 1
    assert all(p.exists() for p in store[1])


def test_out_of_range(store):
    assert delete.cmd_delete(["0", "-y"]) == 1
    assert delete.cmd_delete(["2", "-y"]) == 1
    assert all(p.exists() for p in store[1])
