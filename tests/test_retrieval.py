import json

import pytest

from crossweave import retrieval, search, sessions


def texts(*items):
    return [('file', f'record {i}', text) for i, text in enumerate(items)]


def test_finds_unsampled_message_with_before_and_after():
    data = texts(*['ordinary discussion'] * 70, 'before the fix',
                 'The B_NFC_LOCATION_27 setting controls the frequency lock.',
                 'after the fix', *['ordinary discussion'] * 70)
    result = retrieval.excerpt(data, 'B_NFC_LOCATION_27')
    assert 'B_NFC_LOCATION_27' in result
    assert 'before the fix' in result and 'after the fix' in result
    assert 'record 71' in result
    assert len(result) <= 800


def test_query_at_end_of_large_tool_output_is_preserved():
    result = retrieval.excerpt(texts('log noise ' * 10000 + 'fatal TARGET_ERROR_76'),
                               'TARGET_ERROR_76')
    assert 'TARGET_ERROR_76' in result
    assert 'fatal' in result
    assert len(result) <= 800


def test_best_phrase_beats_earlier_partial_terms():
    data = texts(*['USB happened'] * 20, 'unrelated', 'USB driver failed to enumerate')
    result = retrieval.excerpt(data, 'USB driver')
    assert result.index('USB driver failed') < result.index('Match: USB happened')


def test_chinese_and_english_query_terms_can_locate_rephrased_content():
    result = retrieval.excerpt(texts('说明', 'NFC 配置里的频率锁定已经修复', '完成'),
                               '如何修复NFC频率锁定')
    assert 'NFC 配置里的频率锁定' in result


def test_short_identifier_does_not_match_ordinary_word():
    assert retrieval.excerpt(texts('workbench improvements'), 'rk') == ''
    assert 'RK SDK' in retrieval.excerpt(texts('RK SDK source request'), 'rk')


def test_no_query_evidence_returns_empty_for_sample_fallback():
    assert retrieval.excerpt(texts('nothing relevant here'), 'missing-identifier') == ''


def test_context_does_not_cross_file_boundaries():
    data = [('first', 'one', 'before'), ('first', 'two', 'target'),
            ('second', 'three', 'not a neighboring message')]
    result = retrieval.excerpt(data, 'target')
    assert 'before' in result
    assert 'not a neighboring message' not in result


@pytest.mark.parametrize('budget', [120, 200, 600, 800])
def test_two_long_hits_stay_bounded_and_keep_match(budget):
    data = texts('before ' * 100, 'noise ' * 100 + 'needle ONE ' + 'tail ' * 100,
                 'after ' * 100, 'noise ' * 100 + 'needle TWO ' + 'tail ' * 100)
    result = retrieval.excerpt(data, 'needle', budget)
    assert len(result) <= budget
    assert result.count('needle') >= 2


@pytest.mark.parametrize('tool', ['claude', 'codex', 'step', 'pi', 'kimi'])
def test_file_reader_uses_each_tools_content_format(tmp_path, tool):
    records = {
        'claude': {'type': 'user', 'message': {'content': 'needle message'}},
        'codex': {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                  'content': [{'type': 'input_text', 'text': 'needle message'}]}},
        'step': {'type': 'message', 'message': {'role': 'toolResult',
                 'content': [{'type': 'text', 'text': 'needle message'}]}},
        'pi': {'type': 'message', 'message': {'role': 'user',
               'content': [{'type': 'text', 'text': 'needle message'}]}},
        'kimi': {'type': 'turn.prompt', 'input': [{'type': 'text', 'text': 'needle message'}]},
    }
    path = tmp_path / 'conversation.jsonl'
    path.write_text(json.dumps({'type': 'metadata', 'cwd': 'needle metadata'}) + '\n'
                    + 'invalid json\n' + json.dumps(records[tool]) + '\n')
    from unittest.mock import patch
    with patch.object(sessions, 'session_literal_scan_files', return_value=[str(path)]):
        result = retrieval.context_for({'tool': tool, 'id': 'saved',
                                        'path': str(path), 'dir': str(tmp_path)}, 'needle')
    assert 'needle message' in result and 'needle metadata' not in result
    assert 'conversation.jsonl:3' in result


@pytest.mark.parametrize('tool', ['zcode', 'opencode'])
def test_database_reader_includes_tool_output(monkeypatch, tool):
    module = getattr(retrieval, tool)
    calls = []
    def all_texts(sid, with_tool_output=False):
        calls.append((sid, with_tool_output))
        return ['before', 'needle in tool output', 'after']
    monkeypatch.setattr(module, '_all_texts', all_texts)
    assert 'needle in tool output' in retrieval.context_for({'tool': tool, 'id': 'saved'}, 'needle')
    assert calls == [('saved', True)]


def test_note_tail_is_selected_instead_of_leading_summary():
    body = '\n'.join(['- unrelated summary'] * 100 + ['- UniqueIssue99 was fixed'])
    assert 'UniqueIssue99 was fixed' in retrieval.note_context(body, 'UniqueIssue99')


def test_search_uses_raw_context_when_fresh_summary_omits_detail(monkeypatch):
    record = {'tool': 'claude', 'id': 'saved', 'ts': 1, 'path': '/missing'}
    monkeypatch.setattr(search, 'gather_candidates', lambda *args: [record])
    monkeypatch.setattr(sessions, 'resolve_row', lambda r: ('claude', 'saved', 'now', 'saved', '/tmp', 'title'))
    monkeypatch.setattr(sessions, 'literal_matches', lambda *args: [])
    monkeypatch.setattr(search.note, 'session_notes', lambda: {'saved': (100, '- general summary', '/note')})
    monkeypatch.setattr(retrieval, 'context_for', lambda *args: '[query context] raw needle evidence')
    monkeypatch.setattr(search, 'snippet_for', lambda r: pytest.fail('generic sample used'))
    prompts = []
    def judge(prompt, *args):
        prompts.append(prompt)
        return {1: 'source evidence'}, 'step'
    monkeypatch.setattr(search, 'run_judge_with_fallback', judge)
    assert search.find('needle').judged[0][1] == 'saved'
    assert 'raw needle evidence' in prompts[0]
    assert search.NOTES_PROMPT_NOTE not in prompts[0]


@pytest.mark.parametrize('block', [
    {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'check UniqueIssue99'}},
    {'type': 'tool_result', 'content': 'log result: UniqueIssue99'},
    {'type': 'tool_result', 'content': [{'type': 'text', 'text': 'UniqueIssue99 found'}]},
])
def test_claude_tool_only_evidence_reaches_both_passes(tmp_path, block):
    from crossweave import scan
    path = tmp_path / 'claude.jsonl'
    path.write_text(json.dumps({'type': 'assistant', 'message': {'content': [block]}}) + '\n')
    record = {'tool': 'claude', 'path': str(path), 'id': 'saved'}
    assert 'UniqueIssue99' in retrieval.context_for(record, 'UniqueIssue99')
    assert scan._file_contains(str(path), scan._literal_pattern('UniqueIssue99'), 'claude')


def test_query_without_lexical_match_keeps_semantic_sample(monkeypatch):
    record = {'tool': 'claude', 'id': 'saved', 'ts': 1, 'path': '/missing'}
    monkeypatch.setattr(search, 'gather_candidates', lambda *args: [record])
    monkeypatch.setattr(sessions, 'resolve_row', lambda r: ('claude', 'saved', 'now', 'saved', '/tmp', 'title'))
    monkeypatch.setattr(sessions, 'literal_matches', lambda *args: [])
    monkeypatch.setattr(search.note, 'session_notes', lambda: {})
    monkeypatch.setattr(search, 'snippet_for', lambda r: 'paraphrased discussion about touch hardware')
    prompts = []
    def judge(prompt, *args):
        prompts.append(prompt)
        return {}, 'step'
    monkeypatch.setattr(search, 'run_judge_with_fallback', judge)
    search.find('rain mode')
    assert 'paraphrased discussion about touch hardware' in prompts[0]
