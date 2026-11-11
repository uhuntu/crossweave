from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from crossweave import exporter, sessions


@pytest.fixture
def source(monkeypatch, tmp_path):
    project = tmp_path / 'my-wiki'
    (project / 'raw' / 'sources').mkdir(parents=True)
    (project / 'wiki').mkdir()
    state = {'text': '# Crossweave handoff from zcode\n\nSource session: `saved`\n\n## User\n\nOriginal request\n',
             'title': 'RK source investigation'}
    monkeypatch.setitem(sessions.ID_RESOLVERS, 'zcode', lambda prefix: ['saved'])
    monkeypatch.setattr(sessions, 'session_handoff_details', lambda tool, sid: ('/project', state['text']))
    monkeypatch.setattr(sessions, '_find_record', lambda tool, sid: {'tool': tool, 'id': sid})
    monkeypatch.setattr(sessions, '_resolve_title_and_cwd', lambda record: (state['title'], '/project'))
    return project, state


def test_snapshot_is_versioned_and_unchanged_runs_do_not_write(source):
    project, state = source
    path, created = exporter.snapshot(project, 'zcode', 'saved')
    assert created
    original = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    assert path.is_relative_to(project / 'raw' / 'sources' / 'crossweave')
    text = original.decode()
    assert 'source_session: "saved"' in text
    assert 'source_directory: "/project"' in text
    assert 'content_scope: "user_assistant_text"' in text
    assert 'view_command: "cw show zcode saved"' in text
    assert 'Original request' in text
    assert path.stem in text
    assert exporter.snapshot(project, 'zcode', 'saved') == (path, False)
    assert path.stat().st_mtime_ns == stamp
    state['text'] += '\n## Assistant\n\nNew answer\n'
    newer, created = exporter.snapshot(project, 'zcode', 'saved')
    assert created and newer != path
    assert path.read_bytes() == original
    assert 'New answer' in newer.read_text()
    assert not list(path.parent.glob('*.tmp'))


def test_modified_snapshot_is_not_overwritten(source):
    project, _ = source
    path, _ = exporter.snapshot(project, 'zcode', 'saved')
    path.write_text('a human edited this file')
    with pytest.raises(exporter.ExportError, match='modified'):
        exporter.snapshot(project, 'zcode', 'saved')
    assert path.read_text() == 'a human edited this file'


def test_concurrent_exporters_publish_one_complete_snapshot(source):
    project, _ = source
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: exporter.snapshot(project, 'zcode', 'saved'), range(4)))
    assert sum(created for _, created in results) == 1
    assert len({path for path, _ in results}) == 1
    assert results[0][0].read_text().endswith('Original request\n')


def test_unknown_folder_is_not_created(tmp_path):
    missing = tmp_path / 'not-a-project'
    with pytest.raises(exporter.ExportError, match='create a project'):
        exporter.snapshot(missing, 'zcode', 'saved')
    assert not missing.exists()


def test_export_cannot_follow_a_source_folder_symlink_outside_project(source, tmp_path):
    project, _ = source
    outside = tmp_path / 'outside'
    outside.mkdir()
    (project / 'raw' / 'sources' / 'crossweave').symlink_to(outside, target_is_directory=True)
    with pytest.raises(exporter.ExportError, match='outside'):
        exporter.snapshot(project, 'zcode', 'saved')
    assert list(outside.iterdir()) == []


def test_session_id_is_not_a_filesystem_path(source):
    project, _ = source
    target, _ = exporter.snapshot(project, 'zcode', '../../escape')
    assert target.is_relative_to(project / 'raw' / 'sources' / 'crossweave' / 'zcode')
    assert '..' not in target.parent.name


def test_selection_does_not_export_remote_or_removed_tool(monkeypatch):
    for row in [{'tool': 'zcode', 'id': 'saved', 'machine': 'remote'},
                {'tool': 'workbuddy', 'id': 'saved'}]:
        monkeypatch.setattr(sessions, 'read_list_cache', lambda: [row])
        with pytest.raises(exporter.ExportError):
            exporter.resolve_selection(['1'])


@pytest.fixture
def wiki_api(source):
    project, _ = source
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append(('GET', self.path, self.headers.get('Authorization')))
            if self.path == '/api/v1/redirect':
                self.send_response(302)
                self.send_header('Location', 'https://example.invalid/collect')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'projects': [
                {'id': 'wiki/test', 'path': str(project)}]}).encode())
        def do_POST(self):
            calls.append(('POST', self.path, self.headers.get('Authorization')))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"result":{"queued":1}}')
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except PermissionError:
        pytest.skip('loopback sockets unavailable in sandbox')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', calls
    server.shutdown()
    server.server_close()
    thread.join()


def test_cli_exports_then_requests_matching_project_rescan(source, wiki_api, monkeypatch, capsys):
    project, _ = source
    url, calls = wiki_api
    monkeypatch.setenv(exporter.TOKEN_ENV, 'test-sensitive-token')
    args = ['zcode', 'sav', '--llm-wiki', str(project), '--rescan', '--api-url', url]
    assert exporter.cmd_export(args) == 0
    assert calls == [('GET', '/api/v1/projects', 'Bearer test-sensitive-token'),
                     ('POST', '/api/v1/projects/wiki%2Ftest/sources/rescan', 'Bearer test-sensitive-token')]
    assert 'test-sensitive-token' not in capsys.readouterr().out
    files = list(project.glob('raw/sources/crossweave/**/*.md'))
    assert len(files) == 1
    assert 'test-sensitive-token' not in files[0].read_text()
    assert exporter.cmd_export(args) == 0
    assert 'unchanged:' in capsys.readouterr().out


def test_rescan_refuses_wrong_project(source, wiki_api):
    project, _ = source
    url, calls = wiki_api
    with pytest.raises(exporter.ExportError, match='match an API project'):
        exporter.WikiClient(url).rescan(project, 'some-other-project')
    assert [method for method, _, _ in calls] == ['GET']


def test_failed_rescan_keeps_snapshot_for_retry(source, monkeypatch, capsys):
    project, _ = source
    def fail(*args):
        raise exporter.ExportError('API unavailable')
    monkeypatch.setattr(exporter.WikiClient, 'rescan', fail)
    assert exporter.cmd_export(['zcode', 'saved', '--llm-wiki', str(project), '--rescan']) == 1
    assert len(list(project.glob('raw/sources/crossweave/**/*.md'))) == 1
    assert 'snapshot is saved' in capsys.readouterr().err


@pytest.mark.parametrize('url', ['https://example.com', 'http://localhost@evil.test',
                                  'http://127.0.0.1:19828?token=secret', 'file:///tmp/wiki',
                                  'http://localhost:bad', 'http://127.0.0.1/private'])
def test_api_endpoint_remains_local(url):
    with pytest.raises(exporter.ExportError):
        exporter.WikiClient(url, 'sensitive-token')


def test_client_does_not_forward_token_through_redirect(wiki_api):
    url, calls = wiki_api
    with pytest.raises(exporter.ExportError, match='HTTP 302'):
        exporter.WikiClient(url, 'private-token').request('GET', '/redirect')
    assert calls == [('GET', '/api/v1/redirect', 'Bearer private-token')]


def test_snapshot_body_time_fields_are_not_ignored(source):
    project, state = source
    state['text'] += 'exported_at: this is original conversation content\n'
    path, _ = exporter.snapshot(project, 'zcode', 'saved')
    path.write_text(path.read_text().replace('this is original conversation content', 'tampered'))
    with pytest.raises(exporter.ExportError, match='modified'):
        exporter.snapshot(project, 'zcode', 'saved')


@pytest.mark.parametrize('token,code,expected', [
    ('', 401, 'set LLM_WIKI_API_TOKEN'),
    ('private-token', 401, 'supplied token was rejected'),
    ('private-token', 404, 'headless versions lack'),
])
def test_api_errors_distinguish_missing_token_rejected_token_and_incompatible_service(monkeypatch, token, code, expected):
    from urllib.error import HTTPError
    client = exporter.WikiClient(token=token)
    def fail(*args, **kwargs):
        raise HTTPError('http://127.0.0.1:19828/api/v1/projects', code, 'error', {}, None)
    monkeypatch.setattr(client.opener, 'open', fail)
    with pytest.raises(exporter.ExportError) as exc:
        client.request('GET', '/projects')
    assert expected in str(exc.value)
    assert 'private-token' not in str(exc.value)
