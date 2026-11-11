"""Retired integrations must not reappear through another entry point."""
import json

import pytest

from crossweave import chat, cli, common, doctor, machines, search, sessions, show, ui, web

RETIRED = ('codebuddy', 'workbuddy')


@pytest.mark.parametrize('tool', RETIRED)
def test_retired_tool_has_no_public_registration(tool):
    assert tool not in common.TOOLS
    assert tool not in cli.TOOLS
    assert tool not in sessions.ID_RESOLVERS
    assert tool not in search.JUDGE_CMD
    assert tool not in chat.CHAT_TOOLS
    assert tool not in ui.HANDOFF_TARGETS
    assert tool not in dict(doctor.CHECKS)
    assert tool not in cli.USAGE
    with pytest.raises(cli.UsageError):
        cli.build_command(tool, ['-p', 'hello'])
    with pytest.raises(ValueError):
        web.conversation(tool, 'saved')
    assert sessions.session_handoff_details(tool, 'saved') is None


def test_no_surface_scans_retired_stores(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('retired store was scanned')
    monkeypatch.setattr(sessions, 'codebuddy_light_records', forbidden)
    monkeypatch.setattr(sessions, 'workbuddy_light_records', forbidden)
    assert sessions._all_light_records() == []
    assert search.gather_candidates(None) == []
    assert ui.gather() == []
    assert machines.own_index()['sessions'] == []
    sessions.cmd_list([])
    sessions.cmd_stats([])


@pytest.mark.parametrize('tool', RETIRED)
def test_stale_cache_keeps_row_numbers_and_refuses_retired_row(monkeypatch, tmp_path, capsys, tool):
    path = tmp_path / 'cache.json'
    rows = [{'tool': tool, 'id': 'old'}, {'tool': 'claude', 'id': 'saved'}]
    path.write_text(json.dumps(rows))
    monkeypatch.setattr(sessions, 'LIST_CACHE_FILE', str(path))
    assert sessions.read_list_cache() == rows
    assert show.cmd_show(['1']) == 1
    with pytest.raises(SystemExit):
        sessions.resume_by_number(1, [])
    assert 'no longer supported' in capsys.readouterr().err
    monkeypatch.setattr(sessions, 'session_handoff_details', lambda tool, sid: (None, sid))
    assert show.cmd_show(['2']) == 0
    assert capsys.readouterr().out == 'saved'


@pytest.mark.parametrize('tool', RETIRED)
def test_retired_handoff_target_cannot_become_a_prompt(monkeypatch, tool):
    monkeypatch.setattr(sessions, 'read_list_cache', lambda: [{'tool': 'claude', 'id': 'saved'}])
    monkeypatch.setattr(sessions, '_listing_age_warning', lambda: None)
    monkeypatch.setattr(sessions, 'exec_or_die', lambda *args: pytest.fail('agent started'))
    with pytest.raises(SystemExit):
        sessions.resume_by_number(1, [tool])
    with pytest.raises(SystemExit):
        sessions.perform_handoff('claude', 'saved', tool, [])


@pytest.mark.parametrize('tool', RETIRED)
def test_remote_index_ignores_retired_sessions(tool):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    data = {'schema': machines.SCHEMA, 'machine': 'remote', 'generated_at': now.isoformat(),
            'sessions': [{'tool': tool, 'id': 'old'}, {'tool': 'claude', 'id': 'kept'}]}
    result = machines._coerce_index(data, 'remote.json', now, 'local')
    assert [r['id'] for r in result['sessions']] == ['kept']
