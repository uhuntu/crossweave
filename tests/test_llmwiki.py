"""An LLM Wiki read back into a new session: `cw context` and a fresh
`cw <tool>` carry the pages it built about this project."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import pytest

from crossweave import cli, exporter, llmwiki


def page(title, body, front=True):
    head = f'---\ntype: entity\ntitle: "{title}"\n---\n' if front else ''
    return f'{head}# {title}\n\n{body}\n'


@pytest.fixture
def wiki(monkeypatch, tmp_path):
    """A wiki with a page named for the project `board`, one that mentions
    it, one that does not, and the app's own bookkeeping pages; cwd is the
    project, and the API is somewhere nothing listens."""
    root = tmp_path / 'my-wiki'
    pages = {
        'entities/board.md': page('Board', 'The board boots from SPI.'),
        'concepts/usb.md': page('USB 枚举', 'Enumeration on the board fails cold. board board'),
        'concepts/other.md': page('Other', 'Nothing about it.'),
        'index.md': page('Index', 'board board board board'),
        'log.md': page('Log', 'board board board board'),
        'overview.md': page('Overview', 'board board board board'),
    }
    for rel, text in pages.items():
        (root / 'wiki' / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / 'wiki' / rel).write_text(text, encoding='utf-8')
    (root / 'raw' / 'sources').mkdir(parents=True)
    work = tmp_path / 'board'
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv('CW_LLM_WIKI', str(root))
    monkeypatch.setenv('CW_LLM_WIKI_API', 'http://127.0.0.1:9')
    monkeypatch.delenv('CW_CONTEXT', raising=False)
    return root


def test_pages_are_matched_by_name_when_the_app_does_not_answer(wiki):
    found, how = llmwiki.pages(wiki, 'board')
    assert how == 'name'
    assert [p['path'] for p in found] == ['wiki/entities/board.md', 'wiki/concepts/usb.md']
    assert found[0]['title'] == 'Board'
    assert found[0]['text'] == 'The board boots from SPI.'  # no frontmatter, no title line


def test_a_long_page_is_cut_at_a_line(wiki):
    lines = '\n'.join(f'line {n} about board' for n in range(400))
    (wiki / 'wiki/entities/board.md').write_text(page('Board', lines), encoding='utf-8')
    text = llmwiki.pages(wiki, 'board')[0][0]['text']
    assert len(text) <= llmwiki.PAGE_CHARS + 2 and text.endswith('\n…')
    assert text.splitlines()[-2].startswith('line ')


def test_the_home_folder_asks_about_nothing(wiki, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    assert llmwiki.project_wiki_context(str(tmp_path)) is None


def test_nothing_without_a_configured_wiki(wiki, monkeypatch, tmp_path):
    monkeypatch.delenv('CW_LLM_WIKI')
    assert llmwiki.project_wiki_context(str(tmp_path / 'board')) is None
    monkeypatch.setenv('CW_LLM_WIKI', str(tmp_path / 'not-a-wiki'))
    assert llmwiki.project_wiki_context(str(tmp_path / 'board')) is None


@pytest.fixture
def api(wiki, monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def _send(self, data):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        def do_GET(self):
            calls.append(('GET', self.path, None))
            self._send({'ok': True, 'projects': [{'id': 'p1', 'path': str(wiki)}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(('POST', self.path, body))
            self._send({'ok': True, 'results': [
                {'path': 'wiki/index.md', 'title': 'Index', 'content': 'skip me'},
                {'path': str(wiki / 'wiki/concepts/other.md'), 'title': 'Other',
                 'content': page('Other', 'found by meaning')},
                {'path': '/elsewhere/wiki/x.md', 'title': 'X', 'content': 'outside'},
            ]})
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except PermissionError:
        pytest.skip('loopback sockets unavailable in sandbox')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('CW_LLM_WIKI_API', f'http://127.0.0.1:{server.server_port}')
    yield calls
    server.shutdown()
    server.server_close()
    thread.join()


def test_the_apps_search_is_asked_first(api, wiki):
    found, how = llmwiki.pages(wiki, 'board')
    assert how == 'search'
    assert [p['path'] for p in found] == ['wiki/concepts/other.md']
    assert found[0]['text'] == 'found by meaning'
    method, path, body = api[-1]
    assert (method, path) == ('POST', '/api/v1/projects/p1/search')
    assert body['query'] == 'board' and body['includeContent'] is True


def test_an_api_error_falls_back_to_the_folder(wiki, monkeypatch):
    def unauthorized(*a, **k):
        raise exporter.ExportError('LLM Wiki API returned HTTP 401')
    monkeypatch.setattr(exporter.WikiClient, 'search', unauthorized)
    assert llmwiki.pages(wiki, 'board')[1] == 'name'


def test_cw_context_prints_the_pages_after_the_notes(wiki, capsys):
    assert cli.note.cmd_context([]) == 0
    out, err = capsys.readouterr()
    assert out.startswith(f'# From the LLM Wiki at {wiki}')
    assert '## Board\n`wiki/entities/board.md`' in out
    assert 'matched by name' in out
    assert 'LLM Wiki search not used (LLM Wiki API unavailable' in err


def test_cw_context_waits_longer_for_the_search_than_a_launch(wiki, monkeypatch):
    seen = []
    def search(self, project, query, top_k, timeout):
        seen.append(timeout)
        return []
    monkeypatch.setattr(exporter.WikiClient, 'search', search)
    cli.note.cmd_context([])
    llmwiki.project_wiki_context(str(wiki.parent / 'board'))
    assert seen == [llmwiki.ASKED_TIMEOUT, llmwiki.API_TIMEOUT]


def test_a_launch_carries_the_pages(wiki, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli.sessions, 'exec_or_die', ran.append)
    monkeypatch.setattr(sys, 'argv', ['cw', 'claude'])
    cli.main()
    cmd = ran[0]
    assert cmd[1] == '--append-system-prompt'
    assert 'The board boots from SPI.' in cmd[2]
    err = capsys.readouterr().err
    assert '2 LLM Wiki pages attached' in err
    assert 'search not used' not in err


def test_cw_context_off_keeps_the_wiki_out_too(wiki, monkeypatch):
    ran = []
    monkeypatch.setattr(cli.sessions, 'exec_or_die', ran.append)
    monkeypatch.setenv('CW_CONTEXT', 'off')
    monkeypatch.setattr(sys, 'argv', ['cw', 'claude'])
    cli.main()
    assert not any('LLM Wiki' in a for a in ran[0])
