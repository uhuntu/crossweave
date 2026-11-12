from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
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
    from crossweave import search
    monkeypatch.setattr(search, 'gather_candidates', lambda tool: [])  # never the real stores
    return project, state


def test_snapshot_keeps_one_latest_file_per_session(source):
    project, state = source
    path, created = exporter.snapshot(project, 'zcode', 'saved')
    assert created
    assert path == project / 'raw/sources/crossweave/zcode/saved/session.md'
    original = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    text = original.decode()
    assert 'source_session: "saved"' in text
    assert 'source_directory: "/project"' in text
    assert 'content_scope: "user_assistant_text"' in text
    assert 'view_command: "cw show zcode saved"' in text
    assert 'Original request' in text
    assert 'source_revision: "' in text
    assert exporter.snapshot(project, 'zcode', 'saved') == (path, False)
    assert path.stat().st_mtime_ns == stamp
    state['text'] += '\n## Assistant\n\nNew answer\n'
    newer, created = exporter.snapshot(project, 'zcode', 'saved')
    assert created and newer == path
    assert 'New answer' in path.read_text()
    assert list(path.parent.glob('*.md')) == [path]
    history = list(exporter.history_root().glob('zcode/saved/*.md'))
    assert len(history) == 1
    assert history[0].read_bytes() == original
    assert not list(path.parent.glob('*.tmp'))


def test_older_hash_revisions_are_moved_out_of_sources(source):
    project, _ = source
    folder = project / 'raw/sources/crossweave/zcode/saved'
    folder.mkdir(parents=True)
    stale = folder / ('a' * 64 + '.md')
    stale.write_text('---\nkind: "crossweave-session-snapshot"\n'
                     'source_revision: "' + 'b' * 64 + '"\n---\nold\n')
    exporter.snapshot(project, 'zcode', 'saved')
    assert not stale.exists()
    assert list(folder.glob('*.md')) == [folder / 'session.md']
    assert (exporter.history_root() / 'zcode/saved' / ('b' * 64 + '.md')).is_file()


def test_foreign_session_md_is_not_overwritten(source):
    project, _ = source
    folder = project / 'raw/sources/crossweave/zcode/saved'
    folder.mkdir(parents=True)
    target = folder / 'session.md'
    target.write_text('notes a human wrote here')
    with pytest.raises(exporter.ExportError, match='not a crossweave export'):
        exporter.snapshot(project, 'zcode', 'saved')
    assert target.read_text() == 'notes a human wrote here'


def test_hand_edited_snapshot_is_kept_in_history_before_replacement(source):
    project, state = source
    path, _ = exporter.snapshot(project, 'zcode', 'saved')
    path.write_text(path.read_text().replace('Original request', 'hand edited'))
    state['text'] += '\n## Assistant\n\nNew answer\n'
    newer, created = exporter.snapshot(project, 'zcode', 'saved')
    assert created and newer == path
    assert 'New answer' in path.read_text()
    archived = ''.join(p.read_text() for p in exporter.history_root().glob('zcode/saved/*.md'))
    assert 'hand edited' in archived


def test_concurrent_exporters_leave_one_complete_file(source):
    project, _ = source
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: exporter.snapshot(project, 'zcode', 'saved'), range(4)))
    assert {path for path, _ in results} == {project / 'raw/sources/crossweave/zcode/saved/session.md'}
    target = results[0][0]
    assert target.read_text().endswith('Original request\n')
    assert list(target.parent.glob('*.md')) == [target]
    assert not list(target.parent.glob('*.tmp'))


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


def test_only_frontmatter_time_is_ignored(source):
    project, state = source
    state['text'] += 'exported_at: this is original conversation content\n'
    path, _ = exporter.snapshot(project, 'zcode', 'saved')
    path.write_text(path.read_text().replace('this is original conversation content', 'tampered'))
    newer, created = exporter.snapshot(project, 'zcode', 'saved')
    assert created and newer == path
    assert 'this is original conversation content' in newer.read_text()


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


def test_replace_rides_out_a_windows_sharing_violation(monkeypatch, tmp_path):
    """Windows CI: concurrent exporters failed with WinError 5 swapping in
    session.md while another one held it open. The swap retries there."""
    calls = []
    real_replace = os.replace

    def flaky(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError(5, 'Access is denied')
        real_replace(src, dst)

    monkeypatch.setattr(exporter.os, 'name', 'nt')
    monkeypatch.setattr(exporter.os, 'replace', flaky)
    monkeypatch.setattr(exporter.time, 'sleep', lambda _: None)
    src, dst = tmp_path / 'a.tmp', tmp_path / 'session.md'
    src.write_text('new')
    exporter._replace(src, dst)
    assert dst.read_text() == 'new' and len(calls) == 3


def test_replace_does_not_retry_a_posix_permission_error(monkeypatch, tmp_path):
    calls = []

    def denied(src, dst):
        calls.append(src)
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(exporter.os, 'name', 'posix')
    monkeypatch.setattr(exporter.os, 'replace', denied)
    with pytest.raises(PermissionError):
        exporter._replace(tmp_path / 'a', tmp_path / 'b')
    assert len(calls) == 1


@pytest.fixture
def many(source, monkeypatch, tmp_path):
    """Four local sessions for --all: one worth exporting in each of two
    directories, one live, one a codex review subagent, one too short."""
    from crossweave import rename, search

    project, state = source
    now = __import__('time').time()
    work, other = tmp_path / 'work', tmp_path / 'other'
    work.mkdir()
    other.mkdir()
    records = [
        {'tool': 'zcode', 'id': 'live', 'ts': now, 'cwd': str(work), 'turns': 9},
        {'tool': 'zcode', 'id': 'here', 'ts': now - 3600, 'cwd': str(work / 'sub'), 'turns': 9},
        {'tool': 'codex', 'id': 'review', 'ts': now - 3600, 'cwd': str(work), 'turns': 9},
        {'tool': 'zcode', 'id': 'short', 'ts': now - 3600, 'cwd': str(work), 'turns': 1},
        {'tool': 'zcode', 'id': 'there', 'ts': now - 10 * 86400, 'cwd': str(other), 'turns': 9},
    ]
    monkeypatch.setattr(search, 'gather_candidates', lambda tool: list(records))
    monkeypatch.setattr(sessions, '_resolve_title_and_cwd',
                        lambda r: ('Guardian review' if r['id'] == 'review' else 'T ' + r['id'], r['cwd']))
    monkeypatch.setattr(sessions, 'codex_is_subagent', lambda sid: False)
    monkeypatch.setattr(sessions, 'session_turns', lambda r: r['turns'])
    monkeypatch.setattr(rename, '_is_live', lambda r: r['id'] == 'live')
    monkeypatch.setattr(sessions, 'session_parents', lambda r: [])
    monkeypatch.setattr(sessions, 'session_handoff_details',
                        lambda tool, sid: (state.get('cwd'), state['text'] + sid + '\n'))
    seen = []
    monkeypatch.setattr(sessions, '_find_record', lambda tool, sid: seen.append(sid))
    return project, work, seen


def test_all_exports_what_a_wiki_should_ingest(many, capsys):
    project, _, seen = many
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    names = sorted(p.parent.name for p in project.glob('raw/sources/crossweave/*/*/session.md'))
    assert names == ['here', 'there']
    assert seen == []  # the scanned record is passed through, not looked up again
    err = capsys.readouterr().err
    assert '2 sources from 2 sessions (skipping 1 live, 1 under 3 turns, 1 tool-started, 0 with no text)' in err
    assert '2 exported, 0 unchanged, 0 failed' in err
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    assert '0 exported, 2 unchanged' in capsys.readouterr().err


def test_all_scopes_by_since_and_directory(many, capsys):
    project, work, _ = many
    assert exporter.cmd_export(['--all', '--since', '2d', '--dry-run', '--llm-wiki', str(project)]) == 0
    assert capsys.readouterr().out.split() == ['zcode', 'here', 'T', 'here']
    assert exporter.cmd_export(['--all', '--cwd', str(work), '--dry-run', '--llm-wiki', str(project)]) == 0
    assert 'there' not in capsys.readouterr().out
    assert not list(project.glob('raw/sources/crossweave/**/*.md'))


def test_all_asks_before_exporting_without_a_terminal(many, capsys):
    project, _, _ = many
    assert exporter.cmd_export(['--all', '--llm-wiki', str(project)]) == 1
    assert 'pass -y' in capsys.readouterr().err
    assert not list(project.glob('raw/sources/crossweave/**/*.md'))


def test_all_rescans_once_per_run(many, wiki_api, capsys):
    project, _, _ = many
    url, calls = wiki_api
    args = ['--all', '-y', '--llm-wiki', str(project), '--rescan', '--api-url', url]
    assert exporter.cmd_export(args) == 0
    assert [m for m, _, _ in calls] == ['GET', 'POST']


def test_a_failed_rescan_is_retried_by_running_the_command_again(many, wiki_api, monkeypatch, capsys):
    project, _, _ = many
    url, calls = wiki_api
    args = ['--all', '-y', '--llm-wiki', str(project), '--rescan', '--api-url', url]
    real = exporter.WikiClient.rescan
    def unauthorized(*a):
        raise exporter.ExportError('LLM Wiki API returned HTTP 401')
    monkeypatch.setattr(exporter.WikiClient, 'rescan', unauthorized)
    assert exporter.cmd_export(args) == 1
    assert 'Run the same command again' in capsys.readouterr().err
    monkeypatch.setattr(exporter.WikiClient, 'rescan', real)
    # Nothing changed since, and the retry still reaches the app.
    assert exporter.cmd_export(args) == 0
    assert '0 exported, 2 unchanged' in capsys.readouterr().err
    assert [m for m, _, _ in calls] == ['GET', 'POST']


@pytest.mark.parametrize('args, message', [
    (['3', '--all'], 'drop --all'),
    (['--since', '7d', '3'], 'only applies with --all'),
    (['--older-than', '1d', '3'], 'only applies with --all'),
    ([], 'or pass --all'),
])
def test_all_flags_do_not_mix_with_a_single_selection(source, args, message, capsys):
    project, _ = source
    with pytest.raises(SystemExit):
        exporter.cmd_export(args + ['--llm-wiki', str(project)])
    assert message in capsys.readouterr().err


def test_all_rejects_a_bad_span(many, capsys):
    project, _, _ = many
    assert exporter.cmd_export(['--all', '--since', 'week', '--llm-wiki', str(project)]) == 1
    assert '--since takes a span' in capsys.readouterr().err


SEED = ('Continue the work from this {tool} session ({sid}). Read the complete '
        'conversation export at /tmp/x.md. First briefly summarize.')


@pytest.fixture
def chain(source, monkeypatch, tmp_path):
    """A handoff chain a (claude) -> b (kimi) -> c (claude), plus d on its own."""
    from crossweave import rename, search

    project, _ = source
    now = __import__('time').time()
    records = {
        'a': {'tool': 'claude', 'id': 'a', 'ts': now - 3 * 86400, 'title': 'DevGetter 上架'},
        'b': {'tool': 'kimi', 'id': 'b', 'ts': now - 2 * 86400, 'title': '(handoff) DevGetter 上架'},
        'c': {'tool': 'claude', 'id': 'c', 'ts': now - 3600, 'title': 'OEMConfig 静态IP'},
        'd': {'tool': 'step', 'id': 'd', 'ts': now - 3600, 'title': 'alone'},
    }
    parents = {'b': [('claude', 'a')], 'c': [('kimi', 'b')]}
    texts = {
        'a': '## User\n\nship it\n\n## Assistant\n\nfirst answer\n',
        'b': '## User\n\n' + SEED.format(tool='claude', sid='a') + '\n\n## Assistant\n\nsecond answer\n',
        'c': '## User\n\nnext step\n\n## Assistant\n\nthird answer\n',
        'd': '## User\n\nalone question\n\n## Assistant\n\nalone answer\n',
    }
    live = set()
    monkeypatch.setattr(search, 'gather_candidates',
                        lambda tool: sorted(records.values(), key=lambda r: -r['ts']))
    monkeypatch.setattr(sessions, 'session_parents', lambda r: parents.get(r['id'], []))
    monkeypatch.setattr(sessions, '_resolve_title_and_cwd', lambda r: (r['title'], str(tmp_path)))
    monkeypatch.setattr(sessions, 'codex_is_subagent', lambda sid: False)
    monkeypatch.setattr(sessions, 'session_turns', lambda r: 5)
    monkeypatch.setattr(rename, '_is_live', lambda r: r['id'] in live)
    monkeypatch.setattr(sessions, 'session_handoff_details', lambda tool, sid: (
        str(tmp_path), f'# Crossweave handoff from {tool}\n\nSource session: `{sid}`\n\n' + texts[sid]))
    monkeypatch.setattr(sessions, '_find_record', lambda tool, sid: records.get(sid))
    monkeypatch.setitem(sessions.ID_RESOLVERS, 'kimi', lambda prefix: [prefix])
    return project, records, parents, texts, live


def _sources(project):
    return sorted(p.relative_to(project / 'raw/sources/crossweave').as_posix()
                  for p in project.glob('raw/sources/crossweave/**/*.md'))


def test_a_handoff_chain_goes_in_as_one_source(chain, capsys):
    project, *_ = chain
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    assert _sources(project) == ['step/d/session.md', 'topic/claude-a/topic.md']
    text = (project / 'raw/sources/crossweave/topic/claude-a/topic.md').read_text(encoding='utf-8')
    assert 'kind: "crossweave-topic-snapshot"' in text
    assert 'source_sessions: ["claude:a", "kimi:b", "claude:c"]' in text
    # Named by the title the chain kept, not by the newest session's.
    assert 'title: "DevGetter 上架"' in text
    assert 'resume_command: "cw resume claude c"' in text
    assert text.index('first answer') < text.index('second answer') < text.index('third answer')
    assert 'Read the complete conversation export' not in text
    assert '_(cw handoff: continues the claude session `a` above)_\n\n### Assistant' in text
    assert '\n## User' not in text and '\n### User' in text
    assert '2 sources from 4 sessions, 1 of them topics' in capsys.readouterr().err
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    assert '0 exported, 2 unchanged' in capsys.readouterr().err


def test_a_session_exported_alone_is_retired_once_its_topic_is_exported(chain, capsys):
    project, records, *_ = chain
    exporter.snapshot(project, 'claude', 'a')
    alone = project / 'raw/sources/crossweave/claude/a/session.md'
    before = alone.read_bytes()
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    assert not alone.exists() and not alone.parent.exists()
    assert 'claude/a/session.md' not in _sources(project)
    assert [p.read_bytes() for p in exporter.history_root().glob('claude/a/*.md')] == [before]
    assert '1 superseded sources retired' in capsys.readouterr().err


def test_a_topic_whose_oldest_session_changes_leaves_no_second_copy(chain, capsys):
    project, records, parents, texts, _ = chain
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    now = __import__('time').time()
    records['z'] = {'tool': 'claude', 'id': 'z', 'ts': now - 9 * 86400, 'title': 'DevGetter 上架'}
    texts['z'] = '## User\n\nearliest\n\n## Assistant\n\nzero answer\n'
    parents['a'] = [('claude', 'z')]
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    assert _sources(project) == ['step/d/session.md', 'topic/claude-z/topic.md']


def test_a_topic_with_a_live_session_waits_and_counts_its_window_by_any_member(chain, capsys):
    project, records, parents, texts, live = chain
    assert exporter.cmd_export(['--all', '--since', '1d', '--dry-run', '--llm-wiki', str(project)]) == 0
    out = capsys.readouterr().out
    assert 'topic    3 sessions  DevGetter 上架  (claude a, kimi b, claude c)' in out
    live.add('b')
    assert exporter.cmd_export(['--all', '--dry-run', '--llm-wiki', str(project)]) == 0
    captured = capsys.readouterr()
    assert 'topic' not in captured.out and 'skipping 3 live' in captured.err


def test_selecting_one_session_of_a_topic_exports_the_topic(chain, capsys):
    project, *_ = chain
    assert exporter.cmd_export(['kimi', 'b', '--llm-wiki', str(project)]) == 0
    assert _sources(project) == ['topic/claude-a/topic.md']
    assert 'exporting the topic "DevGetter 上架" as one source' in capsys.readouterr().err


def test_a_topic_file_cw_did_not_write_is_left_alone(chain, capsys):
    project, *_ = chain
    folder = project / 'raw/sources/crossweave/topic/claude-a'
    folder.mkdir(parents=True)
    (folder / 'topic.md').write_text('my own notes\n', encoding='utf-8')
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 1
    assert (folder / 'topic.md').read_text(encoding='utf-8') == 'my own notes\n'
    assert 'not a crossweave export' in capsys.readouterr().err


def test_terminal_output_with_crlf_is_unchanged_on_the_next_export(source):
    project, state = source
    state['text'] += '\n## User\n\n<bash-stderr>** WARNING: key exchange\r\n** next line\r\n</bash-stderr>\n'
    path, created = exporter.snapshot(project, 'zcode', 'saved')
    assert created
    assert b'\r' not in path.read_bytes()
    assert exporter.snapshot(project, 'zcode', 'saved') == (path, False)


def test_a_snapshot_an_older_cw_wrote_with_crlf_is_still_recognised(source):
    project, _ = source
    path, _ = exporter.snapshot(project, 'zcode', 'saved')
    path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
    assert exporter.snapshot(project, 'zcode', 'saved') == (path, True)
    assert b'\r' not in path.read_bytes()


def test_a_windows_long_path_prefix_is_not_read_as_outside(monkeypatch, tmp_path):
    monkeypatch.setattr(exporter.Path, 'resolve', lambda self: Path('\\\\?\\C:\\wiki\\raw'))
    assert str(exporter._real(tmp_path)) == str(Path('C:\\wiki\\raw'))


NO_TEXT = ('# Crossweave handoff from kimi\n\nSource session: `{sid}`\n\n'
           '_No textual user or assistant messages were found._\n')


def test_a_session_with_no_text_is_skipped_and_its_old_source_retired(chain, monkeypatch, capsys):
    project, records, parents, texts, _ = chain
    exporter.snapshot(project, 'step', 'd')  # went in before the check existed
    old = project / 'raw/sources/crossweave/step/d/session.md'
    assert old.exists()
    real = exporter.sessions.session_handoff_details
    monkeypatch.setattr(exporter.sessions, 'session_handoff_details', lambda tool, sid: (
        (None, NO_TEXT.format(sid=sid)) if sid in ('d', 'b') else real(tool, sid)))
    assert exporter.cmd_export(['--all', '-y', '--llm-wiki', str(project)]) == 0
    err = capsys.readouterr().err
    assert '2 with no text' in err
    assert not old.exists()
    assert list(exporter.history_root().glob('step/d/*.md'))
    # b drops out of its topic; a and c still go in together.
    assert _sources(project) == ['topic/claude-a/topic.md']
    text = (project / 'raw/sources/crossweave/topic/claude-a/topic.md').read_text(encoding='utf-8')
    assert 'source_sessions: ["claude:a", "claude:c"]' in text


def test_older_than_holds_back_a_topic_still_settling(chain, capsys):
    project, records, *_ = chain
    # c, the newest of the chain, is an hour old; d too. Neither is quiet for a day.
    assert exporter.cmd_export(['--all', '--older-than', '1d', '--dry-run',
                                '--llm-wiki', str(project)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ''
    assert '4 active within 1d' in captured.err
    records['c']['ts'] -= 2 * 86400
    records['d']['ts'] -= 2 * 86400
    assert exporter.cmd_export(['--all', '--older-than', '1d', '--dry-run',
                                '--llm-wiki', str(project)]) == 0
    assert 'topic    3 sessions' in capsys.readouterr().out


@pytest.mark.parametrize('args, message', [
    (['--older-than', 'soon'], '--older-than takes a span'),
    (['--since', '1d', '--older-than', '2d'], 'leaves nothing of --since 1d'),
])
def test_older_than_refuses_what_cannot_match(chain, args, message, capsys):
    project, *_ = chain
    assert exporter.cmd_export(['--all', *args, '--llm-wiki', str(project)]) == 1
    assert message in capsys.readouterr().err
