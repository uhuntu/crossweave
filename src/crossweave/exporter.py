"""Versioned local conversation snapshots for an LLM Wiki source folder."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import sessions

API_URL = 'http://127.0.0.1:19828'
TOKEN_ENV = 'LLM_WIKI_API_TOKEN'


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


def snapshot(project, tool, sid):
    project, root = source_root(project)
    details = sessions.session_handoff_details(tool, sid)
    if details is None:
        raise ExportError('session transcript unavailable')
    cwd, transcript = details
    record = sessions._find_record(tool, sid)
    if record is None:
        raise ExportError('session no longer exists')
    title = sessions._resolve_title_and_cwd(record)[0]
    title = ' '.join((title or sid).split())
    payload = dict(tool=tool, session=sid, cwd=cwd, title=title, transcript=transcript)
    revision = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
    folder = root / 'crossweave' / tool / _slug(sid)
    try:
        folder.resolve().relative_to(root)
    except ValueError:
        raise ExportError('export folder resolves outside raw/sources')
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{revision}.md'
    fields = dict(kind='crossweave-session-snapshot', title=title,
                  source_tool=tool, source_session=sid, source_directory=cwd or '',
                  source_revision=revision, content_scope='user_assistant_text',
                  view_command=f'cw show {tool} {sid}', resume_command=f'cw resume {tool} {sid}')
    head = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}'
                     for key, value in fields.items())
    exported_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    body = (f'# {title}\n\n'
            '> Immutable conversation snapshot: user and assistant text only; '
            'reasoning and tool calls/results are excluded. '
            '`cw show` reads the current session, which may have newer messages. '
            'Snapshots sharing source_tool and source_session are revisions of one '
            'conversation, not independent corroborating sources.\n\n'
            f'View current session: `cw show {tool} {sid}`\n\n'
            + transcript.split('\n', 1)[-1].lstrip())
    document = f'---\n{head}\nexported_at: "{exported_at}"\n---\n{body}'

    def verify_existing():
        if target.is_symlink() or not target.is_file():
            raise ExportError('snapshot path already exists and is not a regular file')
        existing = target.read_text(encoding='utf-8')
        def strip_time(text):
            header, separator, body = text.partition('\n---\n')
            header = '\n'.join(line for line in header.splitlines()
                               if not line.startswith('exported_at: '))
            return header + separator + body
        if strip_time(existing) != strip_time(document):
            raise ExportError('existing snapshot was modified; refusing to overwrite it')

    if target.exists() or target.is_symlink():
        verify_existing()
        return target, False
    # Publish a complete file without overwriting another exporter or a
    # previous immutable revision. Interrupted temporary writes are not sources.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.tmp',
                                         prefix='.cw-', dir=folder, delete=False) as fh:
            temporary = Path(fh.name)
            fh.write(document)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(temporary, target)
            created = True
        except FileExistsError:
            verify_existing()
            created = False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target, created


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


def cmd_export(args):
    parser = argparse.ArgumentParser(prog='cw export', description='Export a versioned conversation to LLM Wiki')
    parser.add_argument('selection', nargs='+', help='row number or <tool> <session-id-or-prefix>')
    parser.add_argument('--llm-wiki', required=True, metavar='DIR', help='existing LLM Wiki project folder')
    parser.add_argument('--rescan', action='store_true', help='request local LLM Wiki source rescan after export')
    parser.add_argument('--api-url', default=API_URL)
    parser.add_argument('--project-id', help='optional API project ID, verified against the folder')
    opts = parser.parse_args(args)
    if opts.project_id and not opts.rescan:
        parser.error('--project-id requires --rescan')
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
