"""One current conversation snapshot per session in an LLM Wiki source folder."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import sessions
from .common import HANDOFF_PROMPT_PREFIX, TOOL_STARTED_TITLES

API_URL = 'http://127.0.0.1:19828'
TOKEN_ENV = 'LLM_WIKI_API_TOKEN'
SNAPSHOT_NAME = 'session.md'
TOPIC_NAME = 'topic.md'
SESSION_KIND = 'crossweave-session-snapshot'
TOPIC_KIND = 'crossweave-topic-snapshot'
# Predecessor exporters wrote one <content-hash>.md per revision next to each
# other. Left in place, LLM Wiki would read one conversation as several
# independent sources, which is the opposite of what it should conclude.
OLD_REVISION_RE = re.compile(r'[0-9a-f]{64}\.md')


class ExportError(Exception):
    pass


def resolve_selection(selection):
    if len(selection) == 1 and sessions.ROW_NUMBER_RE.fullmatch(selection[0]):
        rows = sessions.read_list_cache()
        number = int(selection[0])
        if not 1 <= number <= len(rows):
            raise ExportError('row unavailable -- run `cw sessions` first')
        row = rows[number - 1]
        if row.get('machine'):
            raise ExportError('session lives on another machine; export it there')
        tool, sid = row['tool'], row['id']
    elif len(selection) == 2 and selection[0] in sessions.ID_RESOLVERS:
        tool, prefix = selection
        matches = sessions.ID_RESOLVERS[tool](prefix)
        if len(matches) != 1:
            raise ExportError(f'{len(matches)} sessions match; use a unique session ID')
        sid = matches[0]
    else:
        raise ExportError('select a row number or <tool> <session-id-or-prefix>')
    if tool not in sessions.TOOLS:
        raise ExportError(f'{tool} is no longer supported')
    return tool, sid


def source_root(project):
    project = _real(Path(project).expanduser())
    root = project / 'raw' / 'sources'
    if not root.is_dir() or not (project / 'wiki').is_dir():
        raise ExportError('not an LLM Wiki project: expected raw/sources/ and wiki/; '
                          'create a project in LLM Wiki first')
    try:
        _real(root).relative_to(project)
    except ValueError:
        raise ExportError('raw/sources must remain inside the selected project')
    return project, _real(root)


def _real(path):
    """path.resolve() without the \\\\?\\ prefix Windows hands back when a
    file on the way is held open -- a concurrent exporter swapping its
    snapshot in -- which would make a folder inside raw/sources compare as
    outside it."""
    resolved = str(Path(path).resolve())
    if resolved.startswith('\\\\?\\') and not resolved.startswith('\\\\?\\UNC\\'):
        resolved = resolved[4:]
    return Path(resolved)


def _slug(sid):
    slug = re.sub(r'[^A-Za-z0-9_-]', '-', sid).strip('-')[:80] or 'session'
    if slug != sid:
        slug += '-' + hashlib.sha256(sid.encode()).hexdigest()[:12]
    return slug


def history_root():
    """Where replaced snapshots go: cw's own state, never the wiki project."""
    return Path(sessions.HANDOFF_DIR).parent / 'export-history'


def _without_time(text):
    header, separator, body = text.partition('\n---\n')
    header = '\n'.join(line for line in header.splitlines()
                       if not line.startswith('exported_at: '))
    return header + separator + body


def _is_snapshot(text):
    head = text.partition('\n---\n')[0]
    lines = head.splitlines()
    return head.startswith('---\n') and any(f'kind: "{kind}"' in lines
                                            for kind in (SESSION_KIND, TOPIC_KIND))


def _revision_of(text):
    head = text.partition('\n---\n')[0]
    for line in head.splitlines():
        if line.startswith('source_revision: '):
            try:
                value = json.loads(line.split(': ', 1)[1])
            except ValueError:
                return ''
            return value if isinstance(value, str) else ''
    return ''


def _keep_revision(path, tool, slug):
    """Copy a file cw is about to replace into its own history. Best effort:
    a history copy failing must not block the export that matters."""
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None
    name = _revision_of(text)
    if not re.fullmatch(r'[0-9a-f]{64}', name):
        name = hashlib.sha256(text.encode('utf-8')).hexdigest()
    folder = history_root() / tool / slug
    dest = folder / f'{name}.md'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # A hand-edited file can carry the same revision marker as the
            # version already archived; keep the edit rather than drop it.
            if dest.read_bytes() == path.read_bytes():
                return dest
            dest = folder / f'{name}-{hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]}.md'
            if dest.exists():
                return dest
        shutil.copyfile(path, dest)
        return dest
    except OSError:
        return None


def _clear_old_revisions(folder, tool, slug):
    for old in folder.glob('*.md'):
        if old.name == SNAPSHOT_NAME or not OLD_REVISION_RE.fullmatch(old.name):
            continue
        if old.is_symlink() or not old.is_file():
            continue
        _keep_revision(old, tool, slug)
        try:
            old.unlink()
        except OSError:
            pass


def _session(tool, sid, record=None):
    """(record, cwd, title, transcript) for one session, or ExportError."""
    details = sessions.session_handoff_details(tool, sid)
    if details is None:
        raise ExportError('session transcript unavailable')
    cwd, transcript = details
    if record is None:
        record = sessions._find_record(tool, sid)
    if record is None:
        raise ExportError('session no longer exists')
    title = sessions._resolve_title_and_cwd(record)[0]
    title = ' '.join((title or sid).split())
    return record, cwd, title, transcript


def _revision(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _folder(root, *parts):
    folder = root.joinpath('crossweave', *parts)
    try:
        _real(folder).relative_to(root)
    except ValueError:
        raise ExportError('export folder resolves outside raw/sources')
    return folder


def _document(fields, body):
    head = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}'
                     for key, value in fields.items())
    exported_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    # One newline convention, written and read untranslated: a transcript
    # carrying terminal output has \r\n in it, and a file read back with
    # newline translation never equals the document it was written from --
    # every run rewrote it, and the app re-ingested it each time.
    body = body.replace('\r\n', '\n').replace('\r', '\n')
    return f'---\n{head}\nexported_at: "{exported_at}"\n---\n{body}'


def _write(folder, name, document, tool, slug):
    """Swap `document` in as folder/name; False when it is already there."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.is_symlink():
        raise ExportError('snapshot path is a symlink; refusing to replace it')
    if target.exists():
        if not target.is_file():
            raise ExportError('snapshot path exists and is not a regular file')
        with open(target, encoding='utf-8', newline='') as fh:
            existing = fh.read()
        # A snapshot an older cw wrote on Windows has \r\n throughout: still
        # cw's, and unequal to the document, so it is replaced once.
        if not _is_snapshot(existing.replace('\r\n', '\n')):
            raise ExportError(f'{name} exists and is not a crossweave export; '
                              'refusing to overwrite it')
        if _without_time(existing) == _without_time(document):
            return target, False
        _keep_revision(target, tool, slug)
    # Write a complete file beside the snapshot, then swap it in. A reader
    # (or a concurrent exporter) sees either the previous whole file or the
    # new whole file, never a half-written one.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', suffix='.tmp',
                                         prefix='.cw-', dir=folder, delete=False) as fh:
            temporary = Path(fh.name)
            fh.write(document)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target, True


def snapshot(project, tool, sid, record=None):
    project, root = source_root(project)
    _record, cwd, title, transcript = _session(tool, sid, record)
    revision = _revision(dict(tool=tool, session=sid, cwd=cwd, title=title, transcript=transcript))
    slug = _slug(sid)
    folder = _folder(root, tool, slug)
    folder.mkdir(parents=True, exist_ok=True)
    _clear_old_revisions(folder, tool, slug)
    fields = dict(kind=SESSION_KIND, title=title,
                  source_tool=tool, source_session=sid, source_directory=cwd or '',
                  source_revision=revision, content_scope='user_assistant_text',
                  view_command=f'cw show {tool} {sid}', resume_command=f'cw resume {tool} {sid}')
    body = (f'# {title}\n\n'
            '> Conversation snapshot: user and assistant text only; reasoning and '
            'tool calls/results are excluded. cw replaces this file when the '
            'conversation changes, so it is the current state of one session, not '
            "independent evidence. Earlier revisions stay in cw's local history.\n\n"
            f'View current session: `cw show {tool} {sid}`\n\n'
            + transcript.split('\n', 1)[-1].lstrip())
    return _write(folder, SNAPSHOT_NAME, _document(fields, body), tool, slug)


# A handoff target opens with cw's seed -- a pointer to the export file,
# not a request. In a topic the session it points at is the one just above.
_SEED_SECTION_RE = re.compile(r'(## User\n\n)' + re.escape(HANDOFF_PROMPT_PREFIX)
                              + r'(\w+) session \(([^)\s]+)\)[^\n]*(?:\n(?!## )[^\n]*)*')


def _topic_part(transcript):
    """A member's transcript as part of a topic page: its own header
    dropped, roles one level down under the member's heading, the handoff
    seed reduced to what it says."""
    text = transcript.split('\n', 1)[-1].lstrip()
    text = _SEED_SECTION_RE.sub(
        lambda m: f'{m[1]}_(cw handoff: continues the {m[2]} session `{m[3]}` above)_\n', text)
    return re.sub(r'^## (User|Assistant)$', r'### \1', text, flags=re.M)


def topic_key(members):
    """The folder a topic is filed under: its oldest member. A handoff adds
    a newer session, so the root stays put as the topic grows; when it does
    move (an older session joins), _retire() clears the old folder."""
    first = members[0]
    return f"{first['tool']}-{_slug(first['id'])}"


def topic_snapshot(project, members, title):
    """One source for sessions that continue one another, oldest first.
    `members` are light records. Returns (path, written, retired paths)."""
    project, root = source_root(project)
    parts = [_session(r['tool'], r['id'], r) for r in members]
    ids = [f"{r['tool']}:{r['id']}" for r, *_ in parts]
    revision = _revision(dict(title=title, sessions=[
        dict(tool=r['tool'], session=r['id'], cwd=cwd, title=t, transcript=tx)
        for r, cwd, t, tx in parts]))
    key = topic_key(members)
    folder = _folder(root, 'topic', key)
    newest = members[-1]
    dirs = sorted({cwd for _, cwd, _, _ in parts if cwd})
    fields = dict(kind=TOPIC_KIND, title=title, source_sessions=ids,
                  source_directories=dirs, source_revision=revision,
                  content_scope='user_assistant_text',
                  resume_command=f"cw resume {newest['tool']} {newest['id']}")
    lines = [f'# {title}\n',
             f'> Topic snapshot: {len(parts)} sessions that continue one another '
             '(cw handoffs, Claude /clear, codex forks), oldest first. They are one '
             'line of work merged into one source, not independent evidence: a later '
             'session restates and revises the earlier ones. User and assistant text '
             'only; reasoning and tool calls/results are excluded. cw replaces this '
             'file when any of them changes.\n',
             f"Continue the newest: `cw resume {newest['tool']} {newest['id']}`\n",
             '## Sessions\n']
    for n, (r, cwd, t, _) in enumerate(parts, 1):
        lines.append(f"{n}. {r['tool']} `{r['id']}` — {t}" + (f' — `{cwd}`' if cwd else ''))
    for n, (r, _, t, tx) in enumerate(parts, 1):
        lines.append(f"\n## Session {n}/{len(parts)} · {r['tool']} · {t}\n\n"
                     f"View: `cw show {r['tool']} {r['id']}`\n\n{_topic_part(tx).rstrip()}")
    target, written = _write(folder, TOPIC_NAME, _document(fields, '\n'.join(lines) + '\n'),
                             'topic', key)
    retired = _retire(root, members, keep=target)
    return target, written, retired


def _members_of(text):
    head = text.partition('\n---\n')[0]
    for line in head.splitlines():
        if line.startswith('source_sessions: '):
            try:
                value = json.loads(line.split(': ', 1)[1])
            except ValueError:
                return set()
            return set(value) if isinstance(value, list) else set()
    return set()


def _retire(root, members, keep):
    """Take every other cw source for these sessions out of raw/sources --
    a member exported on its own before it was handed off, a topic filed
    under a root that is no longer the oldest -- into cw's history, so the
    wiki holds the work once. Only files cw wrote are touched."""
    ids = {f"{r['tool']}:{r['id']}" for r in members}
    found = [(_folder(root, r['tool'], _slug(r['id'])) / SNAPSHOT_NAME, r['tool'], _slug(r['id']))
             for r in members]
    topics = root / 'crossweave' / 'topic'
    if topics.is_dir():
        for path in topics.glob(f'*/{TOPIC_NAME}'):
            if path != keep:
                found.append((path, 'topic', path.parent.name))
    retired = []
    for path, tool, slug in found:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        if not _is_snapshot(text):
            continue
        if tool == 'topic' and not (_members_of(text) & ids):
            continue
        _keep_revision(path, tool, slug)
        try:
            path.unlink()
        except OSError:
            continue
        retired.append(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return retired


def _replace(source, target, attempts=20):
    """os.replace, retried briefly on Windows.

    Windows refuses to replace a file while any other handle has it open --
    a concurrent exporter reading the old snapshot, or swapping in its own
    copy, is enough -- and reports it as PermissionError (WinError 5). The
    handle closes within milliseconds, so a short retry rides it out. On
    POSIX a rename never collides that way and PermissionError is a real
    permissions problem, so it is raised at once."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != 'nt' or attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WikiClient:
    def __init__(self, url=API_URL, token=''):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https')
                or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') not in ('', '/api/v1')):
            raise ExportError('LLM Wiki API must be a loopback URL without credentials or query')
        try:
            parsed.port  # reject malformed ports before making requests
        except ValueError:
            raise ExportError('invalid LLM Wiki API port')
        self.base = f'{parsed.scheme}://{parsed.netloc}/api/v1'
        self.token = token.strip()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None, timeout=15):
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ExportError('LLM Wiki API response is too large')
            data = json.loads(raw)
        except HTTPError as exc:
            hint = ''
            if exc.code in (401, 403):
                if self.token:
                    hint = ('; the supplied token was rejected. Use the token for the '
                            f'service at {self.base}; a headless service may use '
                            'a different token from the desktop settings')
                else:
                    hint = f'; set {TOKEN_ENV} for the service at {self.base}'
            elif exc.code == 404:
                hint = (f'; this service does not expose {method} {path}. '
                        'Some headless versions lack the project/rescan API; '
                        'use a compatible desktop API or import the saved Markdown manually')
            raise ExportError(f'LLM Wiki API returned HTTP {exc.code}{hint}')
        except (URLError, TimeoutError, OSError):
            raise ExportError('LLM Wiki API unavailable; start the app and enable its local API')
        except (ValueError, UnicodeError):
            raise ExportError('LLM Wiki API returned invalid JSON')
        if not isinstance(data, dict) or data.get('ok') is not True:
            raise ExportError('LLM Wiki API did not report success')
        return data

    def project_id(self, project, project_id=None, timeout=15):
        """The API's id for the project folder, which must match exactly one."""
        root = Path(project).expanduser().resolve()
        entries = self.request('GET', '/projects', timeout=timeout).get('projects', [])
        if not isinstance(entries, list):
            raise ExportError('LLM Wiki API returned an invalid project list')
        matching = [p for p in entries if isinstance(p, dict)
                    and isinstance(p.get('path'), str)
                    and Path(p['path']).expanduser().resolve() == root
                    and isinstance(p.get('id'), str) and p['id']]
        if project_id:
            matching = [p for p in matching if p['id'] == project_id]
        if len(matching) != 1:
            raise ExportError('selected folder does not uniquely match an API project; '
                              'open it in LLM Wiki and check --project-id')
        return matching[0]['id']

    def rescan(self, project, project_id=None):
        pid = self.project_id(project, project_id)
        return self.request('POST', '/projects/' + quote(pid, safe='') + '/sources/rescan')

    def search(self, project, query, top_k, timeout):
        pid = self.project_id(project, timeout=timeout)
        data = self.request('POST', '/projects/' + quote(pid, safe='') + '/search',
                            body=dict(query=query, topK=top_k, includeContent=True),
                            timeout=timeout)
        results = data.get('results')
        return results if isinstance(results, list) else []


def _under(path, root):
    if not path:
        return False
    try:
        Path(path).expanduser().resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _tool_started(r, title):
    # A judge run, an approval prompt, a codex review subagent: the tool's
    # own bookkeeping, holding no request of the user's.
    return ((r['tool'] == 'codex' and sessions.codex_is_subagent(r['id']))
            or title in TOOL_STARTED_TITLES)


def _turns(r):
    try:
        return sessions.session_turns(r)
    except Exception:  # a store that cannot be counted is still offered
        return None


def _resolved(r):
    try:
        title, cwd = sessions._resolve_title_and_cwd(r)
    except Exception:
        title, cwd = None, None
    return ' '.join((title or '').split()), cwd


def _unit(group, skipped=None):
    """{members (oldest first), title, cwds, turns} for one topic's records,
    without the members a tool started for itself or that hold nothing --
    or None when nothing is left."""
    members, cwds, turns = [], [], []
    for r in sorted(group, key=lambda r: _ts(r)):
        title, cwd = _resolved(r)
        if _tool_started(r, title):
            if skipped is not None:
                skipped['tool'] += 1
            continue
        n = _turns(r)
        if n == 0 and len(group) > 1:
            continue  # an empty session between two real ones adds nothing
        members.append(dict(r, _title=title or r['id']))
        cwds.append(cwd)
        turns.append(n)
    if not members:
        return None
    return dict(members=members, title=_topic_title(members), cwds=cwds, turns=turns)


def _topic_title(members):
    """The title most of a topic's sessions carry, marks dropped -- a
    handoff inherits its source's, so that is the name the work kept; the
    newest member's breaks a tie. The newest alone can be a /clear that
    started on something next to it."""
    titles = [sessions._without_inherited_mark(r['_title']) for r in members]
    known = [t for t in titles if t and t != '(no title)'] or titles
    return max(reversed(known), key=known.count)


def _ts(r):
    from . import note
    return note._ts(r)


def _groups(records):
    group = sessions.link_groups(records)
    out = {}
    for r in records:
        out.setdefault(group[(r['tool'], r['id'])], []).append(r)
    return list(out.values())


def pick_sessions(since_seconds=None, under=None, quiet_seconds=None):
    """([unit], skip counts): what --all exports, newest first. A unit is a
    topic -- the sessions that continue one another, joined the way the
    listing folds them -- and goes in as one source; a session nothing
    continues is a topic of one. The same cut as `cw note --all`: a topic
    with a live session is left until it goes quiet (exported now, it
    changes within minutes and the app re-ingests it for nothing), and one
    of one or two turns in all is a quick question, noise in a knowledge
    base. A topic is in the window, or under the directory, when any of its
    sessions is -- and then all of them go. Unchanged sources are not
    skipped here; the writers find those without a write."""
    from . import note, rename, search

    cutoff = time.time() - since_seconds if since_seconds else 0
    root = Path(under).expanduser().resolve() if under else None
    quiet_cutoff = time.time() - quiet_seconds if quiet_seconds else 0
    picked, empty, skipped = [], [], {'live': 0, 'recent': 0, 'short': 0, 'tool': 0, 'empty': 0}
    for group in _groups(search.gather_candidates(None)):
        if cutoff and not any(not _ts(r) or _ts(r) >= cutoff for r in group):
            continue
        started = {'tool': 0}
        unit = _unit(group, started)
        if unit is None:
            if root is None:  # with --cwd, whether they were in scope is unknown
                skipped['tool'] += started['tool']
            continue
        if root is not None and not any(_under(cwd, root) for cwd in unit['cwds']):
            continue
        skipped['tool'] += started['tool']
        if any(rename._is_live(r) for r in unit['members']):
            skipped['live'] += len(unit['members'])
            continue
        # --older-than: a topic still settling -- a session about to be
        # handed off or /clear'ed into a topic -- waits, so its pages are
        # not built from it alone, then torn down when it joins one.
        if quiet_cutoff and any(_last_active(r) > quiet_cutoff for r in unit['members']):
            skipped['recent'] += len(unit['members'])
            continue
        known = [n for n in unit['turns'] if n is not None]
        if len(known) == len(unit['turns']) and sum(known) < note.MIN_TURNS:
            skipped['short'] += len(unit['members'])
            continue
        # The turn count can see what the transcript cannot: a kimi
        # session of tool calls alone has turns but not a word of text, and
        # LLM Wiki wrote a page about it anyway -- out of nothing.
        hollow = [r for r in unit['members'] if not _has_text(r)]
        if hollow:
            skipped['empty'] += len(hollow)
            empty += hollow
            members = [r for r in unit['members'] if r not in hollow]
            if not members:
                continue
            unit = dict(unit, members=members, title=_topic_title(members))
        picked.append(unit)
    picked.sort(key=lambda u: max(_ts(r) for r in u['members']), reverse=True)
    return picked, skipped, empty


def _last_active(r):
    """When a session was last written to: its listing ts, else its
    transcript's mtime, else 0 (not known -- not counted as recent)."""
    if _ts(r):
        return _ts(r)
    path = sessions._transcript_path(r)
    try:
        return os.path.getmtime(path) if path else 0
    except OSError:
        return 0


_ROLE_RE = re.compile(r'^## (User|Assistant)$', re.M)


def _has_text(r):
    """Whether a session's transcript holds any user or assistant text."""
    try:
        details = sessions.session_handoff_details(r['tool'], r['id'])
    except Exception:
        return True  # unreadable is the export's to report, not a skip
    return details is None or bool(_ROLE_RE.search(details[1]))


def retire_sessions(project, records):
    """Take cw's own single-session snapshots of `records` out of
    raw/sources, into the history. [paths retired]"""
    _, root = source_root(project)
    retired = []
    for r in records:
        slug = _slug(r['id'])
        path = _folder(root, r['tool'], slug) / SNAPSHOT_NAME
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with open(path, encoding='utf-8', newline='') as fh:
                text = fh.read()
        except (OSError, UnicodeError):
            continue
        if not _is_snapshot(text.replace('\r\n', '\n')):
            continue
        _keep_revision(path, r['tool'], slug)
        try:
            path.unlink()
        except OSError:
            continue
        retired.append(path)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return retired


def topic_of(tool, sid):
    """The unit a selected session belongs to: its whole topic when other
    sessions continue it or it continues them, else the session alone."""
    from . import search

    records = search.gather_candidates(None)
    for group in _groups(records):
        if any(r['tool'] == tool and r['id'] == sid for r in group):
            unit = _unit(group)
            if unit and any(r['tool'] == tool and r['id'] == sid for r in unit['members']):
                return unit
    return None


def export_unit(project, unit, got=None):
    """(path, written, retired) for a unit: a topic of several sessions as
    one topic source, a single session as its session snapshot. With
    `got`, the unit's (meta, body) note, the note is the source's content
    instead of the transcript; the path is the same either way, so a wiki
    moves from one to the other in place."""
    members = unit['members']
    if got is not None:
        return note_snapshot(project, unit, *got)
    if len(members) > 1:
        return topic_snapshot(project, members, unit['title'])
    r = members[0]
    target, written = snapshot(project, r['tool'], r['id'], record=r)
    return target, written, []


# ---------- notes as the content: the distilled record, not the transcript ----------

CONTENT_NOTES = 'notes'
CONTENT_TRANSCRIPT = 'transcript'


def notes_state(vault):
    """What the vault already holds: {session id: (noted, body, path)} and
    [(noted, {session ids}, meta, body)] for its topic notes."""
    from . import note

    topics = []
    for path in sorted(Path(vault).glob('*/*.md')):
        if path.name == note.INDEX_NAME:
            continue
        got = note._read_note(str(path), 'topic_key')
        if not got:
            continue
        meta, body = got
        try:
            noted = datetime.datetime.strptime(str(meta.get('noted', '')).strip(),
                                               '%Y-%m-%dT%H:%M').timestamp()
        except ValueError:
            noted = path.stat().st_mtime
        topics.append((noted, set(meta.get('sessions') or []), meta, body))
    return note.session_notes(vault), topics


def fresh_note(unit, state):
    """The unit's note as (meta, body) when the vault has one that covers it
    and none of its sessions moved on since; None when one must be written."""
    from . import note

    members = unit['members']
    singles, topics = state
    if len(members) == 1:
        r = members[0]
        got = singles.get(r['id'])
        if not got or note.is_stale(r, got[0]):
            return None
        parsed = note._read_note(got[2])
        return parsed if parsed else None
    ids = {r['id'] for r in members}
    best = max(topics, key=lambda t: len(t[1] & ids), default=None)
    if not best or not ids <= best[1] or note.topic_note_is_stale(members, best[:2]):
        return None
    return best[2], best[3]


def write_note(unit, vault, judge, judge_explicit=False):
    """Write the unit's note now (one judge call); ((meta, body), its path),
    or ExportError."""
    from . import note

    members = unit['members']
    if len(members) == 1:
        path = note._note_one(members[0], vault, judge, judge_explicit)
        got = note._read_note(path) if path else None
    else:
        newest_first = sorted(members, key=_ts, reverse=True)
        path = note.note_topic(newest_first, vault, judge, judge_explicit)
        got = note._read_note(path, 'topic_key') if path else None
    if not got:
        raise ExportError('no note could be written (see above)')
    return got, path


def write_notes(units, vault, judge, judge_explicit=False, prog='cw export'):
    """Write the notes of `units`, four at a time; ({id(unit): (meta, body)},
    [(unit, error)]). Each note written is said on stdout, each failure on
    stderr, and the project indexes the notes land on are rebuilt."""
    from concurrent.futures import ThreadPoolExecutor
    from . import note

    def one(unit):
        try:
            return write_note(unit, vault, judge, judge_explicit)
        except (ExportError, OSError) as exc:
            return exc

    notes, failed, written = {}, [], []
    with ThreadPoolExecutor(max_workers=note.WORKERS) as pool:
        for unit, got in zip(units, pool.map(one, units)):
            if isinstance(got, Exception):
                failed.append((unit, got))
                r = unit['members'][-1]
                print(f'{prog}: {r["tool"]} {r["id"]}: {got}', file=sys.stderr)
            else:
                notes[id(unit)], path = got
                written.append(path)
                print(f'noted: {path}', flush=True)
    note._index_folders_of(written)  # the project indexes the notes are listed on
    return notes, failed


def _note_text(body):
    """A note's body without the transcript pointers it carries for
    Obsidian (`原文 / Transcript: cw show ...`): in the wiki they would be
    read as knowledge, and the frontmatter already says where it came from."""
    out, skipping = [], False
    for line in body.splitlines():
        if line.startswith('原文 / Transcript'):
            skipping = True
            continue
        if skipping and (not line.strip() or line.lstrip().startswith('- `cw show ')):
            continue
        skipping = False
        out.append(line)
    return '\n'.join(out).strip()


def note_snapshot(project, unit, meta, body):
    """A source whose content is the unit's cw note."""
    project, root = source_root(project)
    members = unit['members']
    text = _note_text(body)
    title = unit['title']
    tools = sorted({r['tool'] for r in members})
    date = str(meta.get('updated') or meta.get('noted') or '')[:10]
    newest = max(members, key=_ts)
    revision = _revision(dict(title=title, note=text,
                              sessions=[f"{r['tool']}:{r['id']}" for r in members]))
    if len(members) > 1:
        key = topic_key(members)
        folder, name, history = _folder(root, 'topic', key), TOPIC_NAME, ('topic', key)
        dirs = sorted({str(meta.get('cwd') or '')} - {''})
        fields = dict(kind=TOPIC_KIND, title=title,
                      source_sessions=[f"{r['tool']}:{r['id']}" for r in members],
                      source_directories=dirs, tools=tools, date=date,
                      source_revision=revision, content_scope='cw_note',
                      resume_command=f"cw resume {newest['tool']} {newest['id']}")
    else:
        r = members[0]
        slug = _slug(r['id'])
        folder, name, history = _folder(root, r['tool'], slug), SNAPSHOT_NAME, (r['tool'], slug)
        folder.mkdir(parents=True, exist_ok=True)
        _clear_old_revisions(folder, r['tool'], slug)
        fields = dict(kind=SESSION_KIND, title=title, source_tool=r['tool'],
                      source_session=r['id'], source_directory=str(meta.get('cwd') or ''),
                      tools=tools, date=date, source_revision=revision,
                      content_scope='cw_note',
                      view_command=f"cw show {r['tool']} {r['id']}",
                      resume_command=f"cw resume {r['tool']} {r['id']}")
    target, written = _write(folder, name, _document(fields, f'# {title}\n\n{text}\n'), *history)
    retired = _retire(root, members, keep=target) if len(members) > 1 else []
    return target, written, retired


def _describe(unit):
    members = unit['members']
    if len(members) == 1:
        r = members[0]
        return f'{r["tool"]:<8} {r["id"]}  {unit["title"]}'
    tools = ', '.join(f'{r["tool"]} {re.sub(r"^(session|sess|ses)_", "", r["id"])[:8]}'
                      for r in members)
    return f'{"topic":<8} {len(members)} sessions  {unit["title"]}  ({tools})'


def _export_all(opts, client):
    from . import note

    since = None
    if opts.since:
        since = note._parse_since(opts.since)
        if since is None:
            raise ExportError('--since takes a span like 30m, 12h, 7d or 2w')
    quiet = None
    if opts.older_than:
        quiet = note._parse_since(opts.older_than)
        if quiet is None:
            raise ExportError('--older-than takes a span like 30m, 12h, 7d or 2w')
        if since and quiet >= since:
            raise ExportError(f'--older-than {opts.older_than} leaves nothing of --since {opts.since}')
    source_root(opts.llm_wiki)  # a wrong folder fails before the scan, not after
    picked, skipped, empty = pick_sessions(since, opts.cwd, quiet)
    window = f' from the last {opts.since}' if since else ''
    if quiet:
        window += f' quiet for {opts.older_than}'
    scope = f' under {opts.cwd}' if opts.cwd else ''
    sessions_in = sum(len(u['members']) for u in picked)
    topics = sum(1 for u in picked if len(u['members']) > 1)
    merged = f', {topics} of them topics merging several sessions' if topics else ''
    print(f'cw export: {len(picked)} sources from {sessions_in} sessions{window}{scope}{merged} '
          f'(skipping {skipped["live"]} live, '
          + (f'{skipped["recent"]} active within {opts.older_than}, ' if quiet else '')
          + f'{skipped["short"]} under {note.MIN_TURNS} turns, '
          f'{skipped["tool"]} tool-started, {skipped["empty"]} with no text)', file=sys.stderr)
    notes, need, vault = {}, [], None
    if opts.content == CONTENT_NOTES and picked:
        vault = note._vault_dir(None, 'cw export')
        if vault is None:
            raise ExportError('no vault for the notes; set CW_VAULT, or pass --content transcript')
        state = notes_state(vault)
        for unit in picked:
            got = fresh_note(unit, state)
            if got is None:
                need.append(unit)
            else:
                notes[id(unit)] = got
        if need:
            print(f'cw export: {len(need)} of them need a note written first '
                  f'(one judge call each, {opts.judge or "default judge"})', file=sys.stderr)
    if opts.dry_run:
        for unit in picked:
            print(_describe(unit) + ('  (+note)' if unit in need else ''))
        return 0
    if not picked and not empty:
        return 0
    # The export is local and free; what it sets off is not: every new or
    # changed source is one ingest in the app, on the app's model.
    calls = f' after {len(need)} judge calls for notes' if need else ''
    if not opts.yes and not note._confirm(
            f'Export up to {len(picked)} sources into {opts.llm_wiki}{calls}? '
            'Each new or changed one is an LLM Wiki ingest. [y/N] '):
        print('cw export: nothing exported (pass -y to skip this question)', file=sys.stderr)
        return 1
    counts = {'exported': 0, 'unchanged': 0, 'failed': 0, 'retired': 0}
    if need:
        from . import search

        got, failed = write_notes(need, vault, opts.judge or search.DEFAULT_JUDGE, bool(opts.judge))
        notes.update(got)
        counts['failed'] += len(failed)
    for unit in picked:
        if opts.content == CONTENT_NOTES and id(unit) not in notes:
            continue  # its note failed, said above
        try:
            target, written, retired = export_unit(opts.llm_wiki, unit, notes.get(id(unit)))
        except (ExportError, OSError) as exc:
            counts['failed'] += 1
            r = unit['members'][-1]
            print(f'cw export: {r["tool"]} {r["id"]}: {exc}', file=sys.stderr)
            continue
        for path in retired:
            print(f'retired: {path}', flush=True)
        counts['retired'] += len(retired)
        if written:
            counts['exported'] += 1
            print(f'exported: {target}', flush=True)
        else:
            counts['unchanged'] += 1
    # A session with no text that went in before this check: its source
    # comes out, so the app stops holding a page made of nothing.
    try:
        hollow = retire_sessions(opts.llm_wiki, empty)
    except (ExportError, OSError) as exc:
        hollow = []
        print(f'cw export: sources of sessions with no text left in place: {exc}', file=sys.stderr)
    for path in hollow:
        print(f'retired: {path}', flush=True)
    counts['retired'] += len(hollow)
    retired = f', {counts["retired"]} superseded sources retired' if counts['retired'] else ''
    print(f'cw export: {counts["exported"]} exported, {counts["unchanged"]} unchanged, '
          f'{counts["failed"]} failed{retired}', file=sys.stderr)
    if client:
        # Asked for, a rescan is always sent, changed or not: it is how a
        # rescan that failed after an earlier export is retried, and the app
        # caches ingests by content, so unchanged sources cost nothing.
        try:
            client.rescan(opts.llm_wiki, opts.project_id)
        except ExportError as exc:
            print(f'cw export: {exc}', file=sys.stderr)
            print('The snapshots are saved. Run the same command again once the API '
                  'answers: unchanged sources are left alone and the rescan is sent.',
                  file=sys.stderr)
            return 1
        print('LLM Wiki source rescan accepted; check the app for ingest progress. '
              'Generated wiki pages are not verified by this command.')
    return 1 if counts['failed'] else 0


def cmd_export(args):
    parser = argparse.ArgumentParser(prog='cw export', description='Export a versioned conversation to LLM Wiki')
    parser.add_argument('selection', nargs='*', help='row number or <tool> <session-id-or-prefix>')
    parser.add_argument('--all', action='store_true',
                        help='every local session (skipping live and short ones); unchanged ones are left alone')
    parser.add_argument('--since', metavar='SPAN', help='with --all: only sessions active in the last 30m/12h/7d/2w')
    parser.add_argument('--older-than', metavar='SPAN',
                        help='with --all: only topics quiet for at least SPAN, so a session about to be '
                             'continued does not go in alone first')
    parser.add_argument('--cwd', nargs='?', const=os.getcwd(), metavar='DIR',
                        help='with --all: only sessions started in DIR or below (default: current directory)')
    parser.add_argument('--content', choices=(CONTENT_NOTES, CONTENT_TRANSCRIPT), default=CONTENT_NOTES,
                        help="what a source holds: the session's or topic's cw note (default; one is "
                             'written first when missing or behind), or the raw transcript')
    parser.add_argument('--judge', help='with --content notes: the tool that writes missing notes')
    parser.add_argument('--dry-run', action='store_true', help='with --all: list what would be exported')
    parser.add_argument('-y', '--yes', action='store_true', help='with --all: do not ask before exporting')
    parser.add_argument('--llm-wiki', required=True, metavar='DIR', help='existing LLM Wiki project folder')
    parser.add_argument('--rescan', action='store_true', help='request local LLM Wiki source rescan after export')
    parser.add_argument('--api-url', default=API_URL)
    parser.add_argument('--project-id', help='optional API project ID, verified against the folder')
    opts = parser.parse_args(args)
    if opts.project_id and not opts.rescan:
        parser.error('--project-id requires --rescan')
    if opts.all and opts.selection:
        parser.error('--all picks the sessions itself; drop the row/session, or drop --all')
    if not opts.all:
        if not opts.selection:
            parser.error('select a row number or <tool> <session-id-or-prefix>, or pass --all')
        used = [flag for flag, on in (('--since', opts.since), ('--older-than', opts.older_than),
                                      ('--cwd', opts.cwd),
                                      ('--dry-run', opts.dry_run), ('-y', opts.yes)) if on]
        if used:
            parser.error(f'{", ".join(used)} only applies with --all')
    if opts.all:
        try:
            client = (WikiClient(opts.api_url, os.environ.get(TOKEN_ENV, ''))
                      if opts.rescan and not opts.dry_run else None)
            return _export_all(opts, client)
        except (ExportError, OSError) as exc:
            print(f'cw export: {exc}', file=sys.stderr)
            return 1
    target = None
    try:
        tool, sid = resolve_selection(opts.selection)
        # Validate the endpoint before exporting if an API action was requested.
        client = WikiClient(opts.api_url, os.environ.get(TOKEN_ENV, '')) if opts.rescan else None
        unit = topic_of(tool, sid)
        if unit and len(unit['members']) > 1:
            print(f'cw export: {tool} {sid} continues or is continued by '
                  f'{len(unit["members"]) - 1} other sessions; exporting the topic '
                  f'"{unit["title"]}" as one source', file=sys.stderr)
        got = None
        if opts.content == CONTENT_NOTES:
            from . import note, search

            source_root(opts.llm_wiki)
            if unit is None:
                record = sessions._find_record(tool, sid)
                if record is None:
                    raise ExportError('session no longer exists')
                title = ' '.join((sessions._resolve_title_and_cwd(record)[0] or sid).split())
                unit = dict(members=[record], title=title)
            vault = note._vault_dir(None, 'cw export')
            if vault is None:
                raise ExportError('no vault for the notes; set CW_VAULT, or pass --content transcript')
            got = fresh_note(unit, notes_state(vault))
            if got is None:
                print('cw export: writing the note first (one judge call)', file=sys.stderr)
                got, path = write_note(unit, vault, opts.judge or search.DEFAULT_JUDGE, bool(opts.judge))
                note._index_folders_of([path])
                print(f'noted: {path}', file=sys.stderr)
        if got is not None or (unit and len(unit['members']) > 1):
            target, created, retired = export_unit(opts.llm_wiki, unit, got)
            for path in retired:
                print(f'retired: {path}')
        else:
            target, created = snapshot(opts.llm_wiki, tool, sid)
        print(f'{"exported" if created else "unchanged"}: {target}')
        if client:
            client.rescan(opts.llm_wiki, opts.project_id)
            print('LLM Wiki source rescan accepted; check the app for ingest progress. '
                  'Generated wiki pages are not verified by this command.')
        return 0
    except (ExportError, OSError) as exc:
        print(f'cw export: {exc}', file=sys.stderr)
        if target is not None:
            print('The local snapshot is saved. Import it in LLM Wiki, or retry '
                  '--rescan with a compatible API and its matching token.',
                  file=sys.stderr)
        return 1
