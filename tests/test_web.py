import http.client
import json
import sys
import threading

import pytest

from crossweave import web


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(web, 'list_sessions', lambda: [dict(tool='zcode', id='sess_test', title='Test')])
    def read(tool, sid):
        if sid != 'sess_test' or tool not in web.chat.CHAT_TOOLS:
            raise ValueError('会话不存在')
        return dict(tool=tool, id=sid, cwd=str(tmp_path), markdown='# saved',
                    messages=[dict(role='user', text='hello')], can_chat=True)
    monkeypatch.setattr(web, 'conversation', read)
    try:
        server = web.Workspace(('127.0.0.1', 0))
    except PermissionError:
        pytest.skip('loopback sockets unavailable in sandbox')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, method, path, data=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    conn.request(method, path, body=None if data is None else json.dumps(data), headers=headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def auth(server):
    return {'Origin': f'http://127.0.0.1:{server.server_port}',
            'X-CW-Token': server.token, 'Content-Type': 'application/json'}


def test_workspace_serves_assets_and_history(workspace):
    status, headers, body = request(workspace, 'GET', '/')
    assert status == 200
    assert workspace.token.encode() in body
    assert '__CW_TOKEN__' not in body.decode()
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert request(workspace, 'GET', '/app.js')[0] == 200
    assert request(workspace, 'GET', '/style.css')[0] == 200
    assert json.loads(request(workspace, 'GET', '/api/sessions')[2])[0]['id'] == 'sess_test'
    result = request(workspace, 'GET', '/api/conversation?tool=zcode&id=sess_test')
    assert json.loads(result[2])['messages'][0]['text'] == 'hello'
    assert request(workspace, 'GET', '/api/conversation?tool=zcode&id=missing')[0] == 404
    assert request(workspace, 'GET', '/../../pyproject.toml')[0] == 404


@pytest.mark.parametrize('headers', [{}, {'Origin': 'https://evil.example'}, {'Host': 'evil.example'}])
def test_external_requests_cannot_start_commands(workspace, monkeypatch, headers):
    monkeypatch.setattr(web.subprocess, 'Popen', lambda *args, **kw: pytest.fail('command started'))
    assert request(workspace, 'POST', '/api/chat', dict(id='sess_test', text='hello'), headers)[0] == 403


def test_rebinding_host_blocked(workspace):
    assert request(workspace, 'GET', '/api/sessions', headers={'Host': 'evil.example'})[0] == 403


def test_stream_preserves_session_cwd_and_unicode(workspace, monkeypatch):
    calls = []
    def spawnable(argv):
        calls.append(argv)
        return [sys.executable, '-c', 'import sys; sys.stdout.buffer.write("你好".encode()); sys.stdout.flush()']
    monkeypatch.setattr(web.sessions, 'spawnable', spawnable)
    status, _, body = request(workspace, 'POST', '/api/chat',
                              dict(id='sess_test', text='continue'), auth(workspace))
    assert status == 200
    events = [json.loads(line) for line in body.splitlines()]
    assert ''.join(e.get('output', '') for e in events) == '你好'
    assert events[-1] == dict(done=True, code=0)
    assert calls == [['zcode', '--resume', 'sess_test', '-p', 'continue']]


def test_concurrent_turn_and_invalid_message_rejected(workspace):
    workspace.active.add(('zcode', 'sess_test'))
    assert request(workspace, 'POST', '/api/chat', dict(id='sess_test', text='hello'), auth(workspace))[0] == 409
    for data in [dict(id='sess_test', text=''), dict(id='missing', text='hi'), ['wrong shape']]:
        assert request(workspace, 'POST', '/api/chat', data, auth(workspace))[0] == 400


def test_shutdown_terminates_running_children():
    proc = web.subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                start_new_session=web.os.name == 'posix')
    try:
        web.stop_process(proc)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize('tool', ['claude', 'codex', 'kimi', 'step', 'pi'])
def test_browser_chat_dispatches_selected_tool(workspace, monkeypatch, tool):
    calls = []
    def command(argv):
        calls.append(argv)
        return [sys.executable, '-c', 'import os; print(os.getcwd())']
    monkeypatch.setattr(web.sessions, 'spawnable', command)
    status, _, body = request(workspace, 'POST', '/api/chat',
                              dict(tool=tool, id='sess_test', text='hi'), auth(workspace))
    assert status == 200
    assert calls == [web.chat.turn_command(tool, 'sess_test', 'hi')]
    events = [json.loads(line) for line in body.splitlines()]
    assert events[-1]['code'] == 0
    assert str(workspace.read(web.conversation, tool, 'sess_test')['cwd']) in ''.join(
        e.get('output', '') for e in events)


def test_unsupported_tool_cannot_execute(workspace):
    assert request(workspace, 'POST', '/api/chat',
                   dict(tool='workbuddy', id='sess_test', text='hi'), auth(workspace))[0] == 400


def test_rename_and_delete_endpoints_enforce_auth_and_work(workspace, monkeypatch):
    # Without auth -> 403
    assert request(workspace, 'POST', '/api/rename', dict(tool='claude', id='s1', title='New'))[0] == 403
    assert request(workspace, 'POST', '/api/delete', dict(tool='claude', id='s1'))[0] == 403

    monkeypatch.setattr(web, 'rename_session', lambda tool, sid, title: dict(ok=True, title=title.strip()))
    monkeypatch.setattr(web, 'delete_session', lambda tool, sid: dict(ok=True, backup='/tmp/bak'))

    status, _, body = request(workspace, 'POST', '/api/rename',
                              dict(tool='claude', id='s1', title='Better title'), auth(workspace))
    assert status == 200 and json.loads(body) == dict(ok=True, title='Better title')

    status, _, body = request(workspace, 'POST', '/api/delete',
                              dict(tool='claude', id='s1'), auth(workspace))
    assert status == 200 and json.loads(body) == dict(ok=True, backup='/tmp/bak')

    # Active session rejected with 409
    workspace.active.add(('claude', 's1'))
    assert request(workspace, 'POST', '/api/rename',
                   dict(tool='claude', id='s1', title='X'), auth(workspace))[0] == 409
    assert request(workspace, 'POST', '/api/delete',
                   dict(tool='claude', id='s1'), auth(workspace))[0] == 409


def test_conversation_returns_messages_for_all_tools_and_extra_home_flags(monkeypatch, tmp_path):
    win_home = tmp_path / 'win_user'
    f = win_home / '.claude' / 'projects' / 'C--Users-Admin' / 'c-win-1.jsonl'
    f.parent.mkdir(parents=True)
    f.write_text(
        json.dumps({'type': 'user', 'cwd': r'C:\Users\Admin', 'message': {'content': 'Hello from Win'}}) + '\n' +
        json.dumps({'type': 'assistant', 'message': {'content': 'Hi back!'}}) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('CROSSWEAVE_EXTRA_HOMES', str(win_home))
    info = web.conversation('claude', 'c-win-1')
    assert info['messages'] == [
        {'role': 'user', 'text': 'Hello from Win'},
        {'role': 'assistant', 'text': 'Hi back!'},
    ]
    assert info['extra_home'] == str(win_home)
    assert info['can_chat'] is False
    assert info['can_rename'] is False
    assert info['can_delete'] is True


def test_open_app_window_and_gui_subcommand(monkeypatch, tmp_path):
    launched = []
    fake_proc = object()
    profile = tmp_path / 'gui-profile'
    monkeypatch.setattr(web, 'APP_PROFILE_DIR', str(profile))
    monkeypatch.setattr(web, '_find_app_browser', lambda: '/usr/bin/google-chrome')
    monkeypatch.setattr(web.subprocess, 'Popen', lambda argv, **kw: (launched.append(argv), fake_proc)[1])
    assert web.open_app_window('http://127.0.0.1:8765') is fake_proc
    assert launched[0][:3] == [
        '/usr/bin/google-chrome',
        '--app=http://127.0.0.1:8765',
        f'--user-data-dir={profile}',
    ]

    opened = []
    monkeypatch.setattr(web, '_find_app_browser', lambda: None)
    monkeypatch.setattr(web.webbrowser, 'open', lambda url: opened.append(url))
    assert web.open_app_window('http://127.0.0.1:8765') is None
    assert opened == ['http://127.0.0.1:8765']

    from crossweave import cli
    seen = []
    monkeypatch.setattr(web, 'cmd_web', lambda args: (seen.append(args), 0)[1])
    monkeypatch.setattr('sys.argv', ['cw', 'gui', '--home', '/mnt/win'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen == [['--app', '--home', '/mnt/win']]


def test_install_and_uninstall_desktop(monkeypatch, tmp_path, capsys):
    desktop = tmp_path / 'applications' / 'crossweave.desktop'
    icon = tmp_path / 'icons' / 'crossweave.svg'
    monkeypatch.setattr(web, 'DESKTOP_FILE', str(desktop))
    monkeypatch.setattr(web, 'ICON_FILE', str(icon))
    monkeypatch.setattr(web.shutil, 'which', lambda name: '/home/hunt/.local/bin/cw' if name == 'cw' else None)

    assert web.cmd_web(['--app', '--install-desktop', '--home', '/mnt/win']) == 0
    assert desktop.is_file()
    assert icon.is_file()
    content = desktop.read_text(encoding='utf-8')
    assert 'Name=Crossweave' in content
    assert 'Exec=/home/hunt/.local/bin/cw gui --home /mnt/win' in content
    assert f'Icon={icon}' in content
    assert 'StartupWMClass=crossweave' in content
    assert 'Terminal=false' in content
    assert '<svg' in icon.read_text(encoding='utf-8')

    assert web.cmd_web(['--app', '--uninstall-desktop']) == 0
    assert not desktop.exists()
    assert not icon.exists()
