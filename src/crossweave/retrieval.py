"""Bounded query-focused excerpts from local conversation content."""
import heapq
import json
import os
import re

from . import common, opencode, scan, sessions, zcode

# Generic question words add little evidence and otherwise crowd out the
# identifiers or Chinese word fragments the user actually wants to find.
STOP_WORDS = set('the a an of to in on for and or with about find search please '
                 'session sessions what how this that'.split()) | {
    '这个', '那个', '会话', '之前', '相关', '查找', '搜索', '一下', '帮我', '什么', '如何', '怎么', '请问',
}


def patterns_for(topic):
    """Exact phrase first, then bounded identifier/Chinese query terms."""
    topic = ' '.join(topic.split())
    if not topic:
        return []
    terms = []
    for token in re.findall(r'[A-Za-z0-9_./:+-]+|[\u3400-\u9fff]+', topic):
        if token.isascii():
            terms.extend([token, *re.findall(r'[A-Za-z0-9_]+', token)])
        else:
            terms.append(token)
            terms.extend(token[i:i + 2] for i in range(len(token) - 1))
    terms = list(dict.fromkeys(t.lower() for t in terms
                              if t.lower() not in STOP_WORDS and len(t) >= 2))[:16]
    return [scan._literal_pattern(topic), *[scan._literal_pattern(t) for t in terms
                                           if t != topic.lower()]]


def record_texts(record):
    """Yield (stream, location, text), excluding non-conversation records.

    File scans use the literal pass's format readers and record limit. The
    window selector retains only its best hits and one adjacent text block.
    """
    tool = record.get('tool')
    if tool in ('zcode', 'opencode'):
        module = zcode if tool == 'zcode' else opencode
        for i, text in enumerate(module._all_texts(record['id'], with_tool_output=True), 1):
            yield tool, f'{tool} part {i}', text
        return
    if tool not in sessions.TOOLS:
        return
    paths = sessions.session_literal_scan_files(record)
    for path in paths:
        try:
            with open(path, 'rb') as fh:
                for i, (raw, _) in enumerate(scan._scan_lines(fh), 1):
                    if i > scan.LITERAL_SCAN_LIMIT:
                        break
                    if path.endswith('.jsonl'):
                        try:
                            entry = json.loads(raw)
                        except (ValueError, RecursionError):
                            continue
                        texts = scan._literal_texts('step' if tool == 'pi' else tool, entry)
                    else:
                        texts = [raw.decode('utf-8', 'replace')]
                    for text in texts:
                        if isinstance(text, str):
                            yield path, f'{os.path.basename(path)}:{i}', text
        except OSError:
            continue


def excerpt(texts, topic, max_chars=800):
    """Best two query windows with neighboring text, never a full transcript.

    Exact-phrase hits outrank partial-term hits; more distinct query terms
    outrank fewer. Stable source order breaks ties. Locations let the judge
    distinguish extracted evidence from a general session summary.
    """
    patterns = patterns_for(topic)
    if not patterns or max_chars < 120:
        return ''
    hits = []
    previous = ('', '')
    pending = None

    def keep(hit, following=''):
        score, order, source, location, text, position, before = hit
        # Capture a bounded region around the match, including late matches
        # deep inside a long tool result. Adjacent blocks remain separate.
        begin = max(0, position - 100)
        center = ('…' if begin else '') + text[begin:begin + 260]
        if begin + 260 < len(text):
            center += '…'
        relative = position - begin + (1 if begin else 0)
        item = (score, -order, location[:40], before[-65:], center, relative, following[:65])
        if len(hits) < 2:
            heapq.heappush(hits, item)
        elif item[:2] > hits[0][:2]:
            heapq.heapreplace(hits, item)

    for order, (source, location, raw) in enumerate(texts):
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = common.unwrap_openclaw_ctx(raw)
        if common._is_injected_or_pasted(text) or common.is_image_only(text):
            continue
        text = ' '.join(text.split())
        if not text:
            continue
        if pending:
            keep(pending, text if pending[2] == source else '')
            pending = None
        matches = [p.search(text) for p in patterns]
        if any(matches):
            score = (bool(matches[0]), sum(m is not None for m in matches))
            position = next(m.start() for m in matches if m is not None)
            pending = (score, order, source, location, text, position,
                       previous[1] if previous[0] == source else '')
        previous = (source, text[-65:])
    if pending:
        keep(pending)
    if not hits:
        return ''
    selected = sorted(hits, reverse=True)
    prefix = '[query context] '
    budget = (max_chars - len(prefix) - 3 * (len(selected) - 1)) // len(selected)
    parts = []
    for _, _, location, before, center, position, following in selected:
        label = f'[{location[:min(40, budget // 4)]}] '
        neighbors = budget >= 300
        lead = ('Before: ' + before[-35:] + '\n') if before and neighbors else ''
        tail = ('\nAfter: ' + following[:35]) if following and neighbors else ''
        width = max(1, budget - len(label) - len(lead) - len(tail) - len('Match: '))
        begin = max(0, position - width // 3)
        match = center[begin:begin + width]
        parts.append(label + lead + 'Match: ' + match + tail)
    return prefix + ' | '.join(parts)


def context_for(record, topic, max_chars=800):
    # Older metadata-only rows may have no local transcript location. Check
    # that case explicitly instead of hiding failures in the format readers.
    tool = record.get('tool')
    key = ('id' if tool in ('codex', 'zcode', 'opencode') else
           'dir' if tool == 'kimi' else 'path')
    location = record.get(key)
    if not isinstance(location, (str, os.PathLike)) or not location:
        return ''
    return excerpt(record_texts(record), topic, max_chars)


def note_context(body, topic, max_chars=600):
    lines = (('note', f'note line {i}', text)
             for i, text in enumerate(body.splitlines(), 1)
             if not text.startswith(('原文 / Transcript', '- `cw show ')))
    return excerpt(lines, topic, max_chars)
