import pytest

from crossweave import chat


def setup_chat(monkeypatch, tmp_path, inputs):
    monkeypatch.setattr(chat.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(chat.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setitem(chat.sessions.ID_RESOLVERS, 'zcode', lambda prefix: ['sess_full'])
    monkeypatch.setattr(chat.sessions, 'zcode_cli_on_path', lambda: True)
    monkeypatch.setattr(chat.sessions, 'zcode_session_cwd', lambda sid: str(tmp_path))
    turns = iter(inputs)
    monkeypatch.setattr('builtins.input', lambda prompt: next(turns))


def test_turns_keep_session_and_directory(monkeypatch, tmp_path):
    setup_chat(monkeypatch, tmp_path, ['', 'hello', 'follow up', '/quit'])
    calls = []
    monkeypatch.setattr(chat.subprocess, 'call', lambda argv, **kw: calls.append((argv, kw)) or 0)
    assert chat.cmd_chat(['zcode', 'sess_']) == 0
    assert calls == [(['zcode', '--resume', 'sess_full', '-p', text], {'cwd': str(tmp_path)})
                     for text in ['hello', 'follow up']]


def test_failure_allows_next_turn(monkeypatch, tmp_path):
    setup_chat(monkeypatch, tmp_path, ['one', 'two', '/exit'])
    results = iter([1, 0])
    monkeypatch.setattr(chat.subprocess, 'call', lambda *args, **kw: next(results))
    assert chat.cmd_chat(['zcode', 'sess_']) == 0


def test_eof_exits(monkeypatch, tmp_path):
    setup_chat(monkeypatch, tmp_path, [])
    def eof(prompt):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert chat.cmd_chat(['zcode', 'sess_']) == 0


def test_missing_directory_stops_before_sending(monkeypatch, tmp_path):
    setup_chat(monkeypatch, tmp_path, [])
    monkeypatch.setattr(chat.sessions, 'zcode_session_cwd', lambda sid: str(tmp_path / 'missing'))
    assert chat.cmd_chat(['zcode', 'sess_']) == 1


@pytest.mark.parametrize('tool,expected', [
    ('claude', ['claude', '--resume', 'saved', '-p', '--', '-hello']),
    ('codex', ['codex', 'exec', 'resume', '--skip-git-repo-check', 'saved', '--', '-hello']),
    ('kimi', ['kimi', '-S', 'saved', '-p', '-hello']),
    ('step', ['step', '--resume', 'saved', '-p', '--', '-hello']),
    ('pi', ['pi', '--session', 'saved', '-p', '--', '-hello']),
    ('codebuddy', ['codebuddy', '-r', 'saved', '-p', '--', '-hello']),
])
def test_headless_continuation_arguments(tool, expected):
    assert chat.turn_command(tool, 'saved', '-hello') == expected


@pytest.mark.parametrize('tool', ['workbuddy', 'opencode'])
def test_unsupported_tool_not_offered_for_chat(tool):
    assert not chat.available(tool)
    with pytest.raises(ValueError):
        chat.turn_command(tool, 'saved', 'hi')


def test_terminal_chat_with_another_tool(monkeypatch, tmp_path):
    setup_chat(monkeypatch, tmp_path, ['hello', '/quit'])
    monkeypatch.setitem(chat.sessions.ID_RESOLVERS, 'claude', lambda prefix: ['saved'])
    monkeypatch.setattr(chat, 'available', lambda tool: True)
    monkeypatch.setattr(chat.sessions, 'session_handoff_details', lambda tool, sid: (str(tmp_path), 'history'))
    calls = []
    monkeypatch.setattr(chat.subprocess, 'call', lambda argv, **kw: calls.append((argv, kw)) or 0)
    assert chat.cmd_chat(['claude', 'sav']) == 0
    assert calls == [(chat.turn_command('claude', 'saved', 'hello'), {'cwd': str(tmp_path)})]
