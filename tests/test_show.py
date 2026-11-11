import pytest

from crossweave import show


def test_show_prefix_exports_without_pager(monkeypatch, capsys):
    monkeypatch.setitem(show.sessions.ID_RESOLVERS, 'zcode', lambda prefix: ['sess_full'])
    monkeypatch.setattr(show.sessions, 'session_handoff_details',
                        lambda tool, sid: (None, f'# Crossweave handoff from {tool}\n{sid}\n## User\nHello\n'))
    monkeypatch.setattr(show.pydoc, 'pager', lambda text: pytest.fail('unexpected pager'))
    assert show.cmd_show(['zcode', 'sess_']) == 0
    assert capsys.readouterr().out == '# zcode conversation\nsess_full\n## User\nHello\n'


def test_show_remote_row_refuses_local_lookup(monkeypatch, capsys):
    monkeypatch.setattr(show.sessions, 'read_list_cache',
                        lambda: [{'machine': 'remote', 'tool': 'zcode', 'id': 'x'}])
    monkeypatch.setattr(show.sessions, '_listing_age_warning', lambda: None)
    assert show.cmd_show(['1']) == 1
    assert 'remote' in capsys.readouterr().err


def test_show_terminal_pages_local_row(monkeypatch):
    monkeypatch.setattr(show.sessions, 'read_list_cache',
                        lambda: [{'tool': 'zcode', 'id': 'x'}])
    monkeypatch.setattr(show.sessions, '_listing_age_warning', lambda: None)
    monkeypatch.setattr(show.sessions, 'session_handoff_details', lambda *args: (None, 'body'))
    monkeypatch.setattr(show.sys.stdout, 'isatty', lambda: True)
    pages = []
    monkeypatch.setattr(show.pydoc, 'pager', pages.append)
    assert show.cmd_show(['1']) == 0
    assert pages == ['body']


def test_show_ambiguous_prefix(monkeypatch, capsys):
    monkeypatch.setitem(show.sessions.ID_RESOLVERS, 'zcode', lambda prefix: ['a', 'b'])
    assert show.cmd_show(['zcode', 'sess_']) == 1
    assert '2 sessions match' in capsys.readouterr().err
