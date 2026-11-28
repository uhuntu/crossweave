"""Bounded query-focused excerpts from local conversation content."""
import heapq
import json
import os
import re

from . import agy, common, opencode, scan, sessions, zcode

SESSION_READERS = {'agy': agy, 'opencode': opencode, 'zcode': zcode}

# Generic question words add little evidence and otherwise crowd out the
# identifiers or Chinese word fragments the user actually wants to find.
STOP_WORDS = set('the a an of to in on for and or with about find search please '
                 'session sessions what how this that'.split()) | {
    '这个', '那个', '会话', '之前', '相关', '查找', '搜索', '一下', '帮我', '什么', '如何', '怎么', '请问',
}


def _pattern_groups(topic):
    """Exact phrase pattern plus per-token pattern groups (bounded to 16 total)."""
    topic = ' '.join(topic.split())
    if not topic:
        return None, []
    exact_pat = scan._literal_pattern(topic)
    seen = {topic.lower()}
    groups = []
    total = 0
    for token in re.findall(r'[A-Za-z0-9_./:+-]+|[\u3400-\u9fff]+', topic):
        if total >= 16:
            break
        raw_terms = (
            [token, *re.findall(r'[A-Za-z0-9_]+', token)]
            if token.isascii()
            else [token, *(token[i:i + 2] for i in range(len(token) - 1))]
        )
        group = []
        for raw in raw_terms:
            low = raw.lower()
            if low in STOP_WORDS or len(low) < 2 or low in seen:
                continue
            seen.add(low)
            group.append(scan._literal_pattern(raw))
            total += 1
            if total >= 16:
                break
        if group:
            groups.append(group)
    return exact_pat, groups


def patterns_for(topic):
    """Exact phrase first, then bounded identifier/Chinese query terms."""
    exact_pat, groups = _pattern_groups(topic)
    if exact_pat is None:
        return []
    return [exact_pat, *(p for g in groups for p in g)]


def record_texts(record):
    """Yield (stream, location, text), excluding non-conversation records.

    File scans use the literal pass's format readers and record limit. The
    window selector retains only its best hits and one adjacent text block.
    """
    tool = record.get('tool')
    module = SESSION_READERS.get(tool)
    if module is not None:
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

    Exact-phrase hits outrank partial-term hits. Query terms are grouped per
    source token so overlapping Chinese 2-grams do not inflate scores, and
    groups are weighted by inverse document frequency (1/df) so rare technical
    terms (e.g. `ssh`) are not drowned out by high-frequency words.
    """
    exact_pat, groups = _pattern_groups(topic)
    if exact_pat is None or max_chars < 120:
        return ''
    df = [0] * len(groups)
    # Keep top 2 candidates per group (plus exact) in O(groups) memory.
    buckets = {}
    previous = ('', '')
    pending = None

    def _window_for(text, pos, before, following):
        begin = max(0, pos - 100)
        center = ('…' if begin else '') + text[begin:begin + 260]
        if begin + 260 < len(text):
            center += '…'
        relative = pos - begin + (1 if begin else 0)
        return center, relative, before[-65:], following[:65]

    def keep(hit, following=''):
        exact_pos, matched_groups, order, source, location, text, before = hit
        if not matched_groups and exact_pos is None:
            return
        # Pre-slice windows per matched group so we don't retain huge `text`.
        windows = {}
        for gi, pos in matched_groups.items():
            windows[gi] = _window_for(text, pos, before, following)
        exact_win = _window_for(text, exact_pos, before, following) if exact_pos is not None else None
        entry = (exact_win, matched_groups, windows, order, location[:40])
        rank = (exact_pos is not None, len(matched_groups), -order)
        target_keys = list(matched_groups.keys()) if matched_groups else ['exact']
        for key in target_keys:
            bucket = buckets.setdefault(key, [])
            heapq.heappush(bucket, (rank, order, entry))
            if len(bucket) > 2:
                heapq.heappop(bucket)

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
            keep(pending, text if pending[3] == source else '')
            pending = None
        exact_m = exact_pat.search(text)
        matched_groups = {}
        for gi, group in enumerate(groups):
            for p in group:
                m = p.search(text)
                if m is not None:
                    matched_groups[gi] = m.start()
                    df[gi] += 1
                    break
        if exact_m is not None or matched_groups:
            pending = (
                exact_m.start() if exact_m is not None else None,
                matched_groups,
                order,
                source,
                location,
                text,
                previous[1] if previous[0] == source else '',
            )
        previous = (source, text[-65:])
    if pending:
        keep(pending)
    if not buckets:
        return ''

    unique = {}
    for bucket in buckets.values():
        for _, order, entry in bucket:
            unique[order] = entry

    def idf_score(entry):
        exact_win, matched_groups, _, order, _ = entry
        rarity = sum(1.0 / df[gi] for gi in matched_groups if df[gi] > 0)
        return (exact_win is not None, rarity, len(matched_groups), -order)

    selected = sorted(unique.values(), key=idf_score, reverse=True)[:2]
    prefix = '[query context] '
    budget = (max_chars - len(prefix) - 3 * (len(selected) - 1)) // len(selected)
    parts = []
    for exact_win, matched_groups, windows, _, location in selected:
        if exact_win is not None:
            center, position, before, following = exact_win
        else:
            rarest_gi = max(matched_groups, key=lambda gi: 1.0 / df[gi] if df[gi] > 0 else 0.0)
            center, position, before, following = windows[rarest_gi]
        label = f'[{location}] '
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
    key = ('id' if tool in ('codex', 'zcode', 'opencode', 'agy') else
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
