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
from .common import TOOL_STARTED_TITLES

API_URL = 'http://127.0.0.1:19828'
TOKEN_ENV = 'LLM_WIKI_API_TOKEN'
SNAPSHOT_NAME = 'session.md'
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
    project = Path(project).expanduser().resolve()
    root = project / 'raw' / 'sources'
    if not root.is_dir() or not (project / 'wiki').is_dir():
        raise ExportError('not an LLM Wiki project: expected raw/sources/ and wiki/; '
                          'create a project in LLM Wiki first')
    try:
        root.resolve().relative_to(project)
    except ValueError:
        raise ExportError('raw/sources must remain inside the selected project')
    return project, root.resolve()


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
    return head.startswith('---\n') and 'kind: "crossweave-session-snapshot"' in head.splitlines()


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


def snapshot(project, tool, sid, record=None):
    project, root = source_root(project)
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
    payload = dict(tool=tool, session=sid, cwd=cwd, title=title, transcript=transcript)
    revision = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
    slug = _slug(sid)
    folder = root / 'crossweave' / tool / slug
    try:
        folder.resolve().relative_to(root)
    except ValueError:
        raise ExportError('export folder resolves outside raw/sources')
    folder.mkdir(parents=True, exist_ok=True)
    _clear_old_revisions(folder, tool, slug)
    target = folder / SNAPSHOT_NAME
    fields = dict(kind='crossweave-session-snapshot', title=title,
                  source_tool=tool, source_session=sid, source_directory=cwd or '',
                  source_revision=revision, content_scope='user_assistant_text',
                  view_command=f'cw show {tool} {sid}', resume_command=f'cw resume {tool} {sid}')
    head = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}'
                     for key, value in fields.items())
    exported_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    body = (f'# {title}\n\n'
            '> Conversation snapshot: user and assistant text only; reasoning and '
            'tool calls/results are excluded. cw replaces this file when the '
            'conversation changes, so it is the current state of one session, not '
            "independent evidence. Earlier revisions stay in cw's local history.\n\n"
            f'View current session: `cw show {tool} {sid}`\n\n'
            + transcript.split('\n', 1)[-1].lstrip())
    document = f'---\n{head}\nexported_at: "{exported_at}"\n---\n{body}'

    if target.is_symlink():
        raise ExportError('snapshot path is a symlink; refusing to replace it')
    if target.exists():
        if not target.is_file():
            raise ExportError('snapshot path exists and is not a regular file')
        existing = target.read_text(encoding='utf-8')
        if not _is_snapshot(existing):
            raise ExportError(f'{SNAPSHOT_NAME} exists and is not a crossweave export; '
                              'refusing to overwrite it')
        if _without_time(existing) == _without_time(document):
            return target, False
        _keep_revision(target, tool, slug)
    # Write a complete file beside the snapshot, then swap it in. A reader
    # (or a concurrent exporter) sees either the previous whole file or the
    # new whole file, never a half-written one.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.tmp',
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

    def request(self, method, path):
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = Request(self.base + path, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ExportError('LLM Wiki API response is too large')
            data = json.loads(raw)
        except HTTPError as exc:
            hint = ''
            if exc.code in (401, 403):
                if self.token:
                    hint = ('; the supplied token was rejected. Use the token for the '
                            'service listening at --api-url; a headless service may use '
                            'a different token from the desktop settings')
                else:
                    hint = f'; set {TOKEN_ENV} for the service listening at --api-url'
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

    def rescan(self, project, project_id=None):
        root = Path(project).expanduser().resolve()
        entries = self.request('GET', '/projects').get('projects', [])
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
        return self.request('POST', '/projects/' + quote(matching[0]['id'], safe='') + '/sources/rescan')


def _under(path, root):
    if not path:
        return False
    try:
        Path(path).expanduser().resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def pick_sessions(since_seconds=None, under=None):
    """([(record, title)] to export, skip counts): the local sessions --all takes,
    newest first. The same cut as `cw note --all`: a live conversation is
    left until it goes quiet -- exported now, it changes again within
    minutes and the app re-ingests it for nothing -- and a session of one
    or two turns is a quick question, noise in a knowledge base. Unchanged
    sessions are not skipped here; snapshot() finds those without a write."""
    from . import note, rename, search

    cutoff = time.time() - since_seconds if since_seconds else 0
    root = Path(under).expanduser().resolve() if under else None
    picked, skipped = [], {'live': 0, 'short': 0, 'tool': 0}
    for r in search.gather_candidates(None):
        if cutoff and note._ts(r) and note._ts(r) < cutoff:
            continue
        try:
            title, cwd = sessions._resolve_title_and_cwd(r)
        except Exception:
            title, cwd = None, None
        if root is not None and not _under(cwd, root):
            continue
        # A judge run, an approval prompt, a codex review subagent: the
        # tool's own bookkeeping, holding no request of the user's.
        if ((r['tool'] == 'codex' and sessions.codex_is_subagent(r['id']))
                or title in TOOL_STARTED_TITLES):
            skipped['tool'] += 1
            continue
        if rename._is_live(r):
            skipped['live'] += 1
            continue
        try:
            turns = sessions.session_turns(r)
        except Exception:  # a store that cannot be counted is still offered
            turns = None
        if turns is not None and turns < note.MIN_TURNS:
            skipped['short'] += 1
            continue
        picked.append((r, ' '.join((title or '').split()) or r['id']))
    return picked, skipped


def _export_all(opts, client):
    from . import note

    since = None
    if opts.since:
        since = note._parse_since(opts.since)
        if since is None:
            raise ExportError('--since takes a span like 30m, 12h, 7d or 2w')
    source_root(opts.llm_wiki)  # a wrong folder fails before the scan, not after
    picked, skipped = pick_sessions(since, opts.cwd)
    window = f' from the last {opts.since}' if since else ''
    scope = f' under {opts.cwd}' if opts.cwd else ''
    print(f'cw export: {len(picked)} sessions{window}{scope} '
          f'(skipping {skipped["live"]} live, {skipped["short"]} under {note.MIN_TURNS} turns, '
          f'{skipped["tool"]} tool-started)', file=sys.stderr)
    if opts.dry_run:
        for r, title in picked:
            print(f'{r["tool"]:<8} {r["id"]}  {title}')
        return 0
    if not picked:
        return 0
    # The export is local and free; what it sets off is not: every new or
    # changed source is one ingest in the app, on the app's model.
    if not opts.yes and not note._confirm(
            f'Export up to {len(picked)} sessions into {opts.llm_wiki}? '
            'Each new or changed one is an LLM Wiki ingest. [y/N] '):
        print('cw export: nothing exported (pass -y to skip this question)', file=sys.stderr)
        return 1
    counts = {'exported': 0, 'unchanged': 0, 'failed': 0}
    for r, _ in picked:
        try:
            target, created = snapshot(opts.llm_wiki, r['tool'], r['id'], record=r)
        except (ExportError, OSError) as exc:
            counts['failed'] += 1
            print(f'cw export: {r["tool"]} {r["id"]}: {exc}', file=sys.stderr)
            continue
        if created:
            counts['exported'] += 1
            print(f'exported: {target}', flush=True)
        else:
            counts['unchanged'] += 1
    print(f'cw export: {counts["exported"]} exported, {counts["unchanged"]} unchanged, '
          f'{counts["failed"]} failed', file=sys.stderr)
    if client:
        if counts['exported']:
            try:
                client.rescan(opts.llm_wiki, opts.project_id)
            except ExportError as exc:
                print(f'cw export: {exc}', file=sys.stderr)
                print('The snapshots are saved. Retry with --rescan once the API answers; '
                      'unchanged sessions are skipped, so only the rescan is repeated.',
                      file=sys.stderr)
                return 1
            print('LLM Wiki source rescan accepted; check the app for ingest progress. '
                  'Generated wiki pages are not verified by this command.')
        else:
            print('cw export: nothing changed, so no rescan requested', file=sys.stderr)
    return 1 if counts['failed'] else 0


def cmd_export(args):
    parser = argparse.ArgumentParser(prog='cw export', description='Export a versioned conversation to LLM Wiki')
    parser.add_argument('selection', nargs='*', help='row number or <tool> <session-id-or-prefix>')
    parser.add_argument('--all', action='store_true',
                        help='every local session (skipping live and short ones); unchanged ones are left alone')
    parser.add_argument('--since', metavar='SPAN', help='with --all: only sessions active in the last 30m/12h/7d/2w')
    parser.add_argument('--cwd', nargs='?', const=os.getcwd(), metavar='DIR',
                        help='with --all: only sessions started in DIR or below (default: current directory)')
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
        used = [flag for flag, on in (('--since', opts.since), ('--cwd', opts.cwd),
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
