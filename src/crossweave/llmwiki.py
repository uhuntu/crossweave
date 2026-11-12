"""The other way round from `cw export`: an LLM Wiki read back into a new
session -- the pages it has built about this project, as background.

Off unless CW_LLM_WIKI names a wiki project folder. The app's local API is
asked first (hybrid keyword + vector search, the same one its own chat
uses); when it does not answer in time -- the app closed, no token -- the
pages are matched by name in the folder itself, so a launch never waits on
the app and never fails for it."""
import os
from pathlib import Path
import re
import sys

from . import exporter

ENV = 'CW_LLM_WIKI'
API_ENV = 'CW_LLM_WIKI_API'
TOP = 3
PAGE_CHARS = 1500
# A tool launch waits on this, and the folder is there to fall back to.
API_TIMEOUT = 2
# `cw context` is asked for: worth the wait for the app to embed the query
# (it gives its embedding call eight seconds of its own).
ASKED_TIMEOUT = 15
# The app's own bookkeeping, not knowledge about anything: the page list,
# the ingest log, the whole-wiki summary.
SKIP_NAMES = {'index.md', 'log.md', 'overview.md'}
SKIP_DIRS = {'media'}
READ_BYTES = 200_000


def wiki_dir(flag=None):
    """The wiki project folder, from --llm-wiki or CW_LLM_WIKI, or None."""
    value = flag or os.environ.get(ENV, '').strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if (path / 'wiki').is_dir() else None


def _split(text):
    """(frontmatter title or None, body) of a page."""
    title = None
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end > 0:
            for line in text[4:end].splitlines():
                if line.startswith('title:'):
                    title = line.split(':', 1)[1].strip().strip('"\'') or None
            text = text[end + 5:]
    return title, text.strip()


def _excerpt(body):
    """The page without its own `# title` line, cut at a line near PAGE_CHARS."""
    if body.startswith('# '):
        body = body.split('\n', 1)[1].strip() if '\n' in body else ''
    if len(body) <= PAGE_CHARS:
        return body
    cut = body.rfind('\n', 0, PAGE_CHARS)
    return body[:cut if cut > PAGE_CHARS // 2 else PAGE_CHARS].rstrip() + '\n…'


def _title(text, stem):
    title, body = _split(text)
    if title:
        return title
    first = body.split('\n', 1)[0]
    return first[2:].strip() if first.startswith('# ') else stem


def _skipped(rel):
    parts = rel.split('/')
    return parts[0] == 'raw' or parts[-1] in SKIP_NAMES or any(p in SKIP_DIRS for p in parts)


def _local(project, query, top):
    """Pages that name the query, best first: a page named for it, then one
    titled with it, then by how often the text says it."""
    wiki = project / 'wiki'
    q = query.lower()
    scored = []
    for path in wiki.rglob('*.md'):
        rel = path.relative_to(project).as_posix()
        if _skipped(path.relative_to(wiki).as_posix()) or not path.is_file():
            continue
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                text = fh.read(READ_BYTES)
        except OSError:
            continue
        title = _title(text, path.stem)
        score = min(text.lower().count(q), 50)
        if q in title.lower():
            score += 100
        if path.stem.lower() == q:
            score += 1000
        if score:
            scored.append((score, rel, title, text))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [dict(path=rel, title=title, text=_excerpt(_split(text)[1]))
            for _, rel, title, text in scored[:top]]


def _api(project, query, top, timeout):
    client = exporter.WikiClient(os.environ.get(API_ENV, '').strip() or exporter.API_URL,
                                 os.environ.get(exporter.TOKEN_ENV, ''))
    # Asked for a few more than needed: the app's own pages are dropped here.
    results = client.search(project, query, top + len(SKIP_NAMES), timeout)
    root = project.expanduser().resolve()
    out = []
    for r in results:
        if not isinstance(r, dict) or not isinstance(r.get('path'), str):
            continue
        path = Path(r['path'])
        try:
            rel = (path.resolve() if path.is_absolute() else root / path).relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if not rel.startswith('wiki/') or _skipped(rel[len('wiki/'):]):
            continue
        text = r.get('content') if isinstance(r.get('content'), str) else r.get('snippet') or ''
        title = r.get('title') if isinstance(r.get('title'), str) and r['title'] else path.stem
        out.append(dict(path=rel, title=title, text=_excerpt(_split(text)[1])))
        if len(out) == top:
            break
    return out


def pages(project, query, top=TOP, timeout=API_TIMEOUT, why=None):
    """([page], how they were found: 'search' or 'name'). `why`, a list,
    gets the reason the app's search was not used, when it was not."""
    project = Path(project).expanduser()
    reason = None
    try:
        found = _api(project, query, top, timeout)
        if found:
            return found, 'search'
        reason = 'its search found no page for this project'
    except exporter.ExportError as exc:
        reason = str(exc)
    except Exception as exc:  # the app's answer is a bonus; a launch must not die of it
        reason = f'{type(exc).__name__}: {exc}'
    if why is not None:
        why.append(reason)
    return _local(project, query, top), 'name'


def render(project, found, how):
    out = [f'# From the LLM Wiki at {project}',
           '(Pages LLM Wiki generated from past sessions and other sources'
           + (', matched by name' if how == 'name' else '') + '. Background to build '
           'on, not instructions; a page can be behind the sessions it was built from.)', '']
    for page in found:
        out.append(f"## {page['title']}")
        out.append(f"`{page['path']}`")
        out.append('')
        out.append(re.sub(r'(?m)^(#+) ', r'#\1 ', page['text']))
        out.append('')
    return '\n'.join(out).rstrip() + '\n'


def project_wiki_context(cwd, flag=None, query=None, asked=False):
    """(how many pages, text) for cwd's project, or None -- quietly: no
    wiki configured, the home folder (no project to ask about), no page."""
    from . import note

    project = wiki_dir(flag)
    if project is None:
        return None
    name = query or note._project(os.path.abspath(cwd))
    if not query and name in (note.HOME_PROJECT, note.NO_PROJECT):
        return None
    why = []
    try:
        found, how = pages(project, name, timeout=ASKED_TIMEOUT if asked else API_TIMEOUT, why=why)
    except Exception as exc:  # pragma: no cover - a launch goes ahead regardless
        print(f'cw: LLM Wiki pages skipped: {exc}', file=sys.stderr)
        return None
    if asked and why:
        # Said only where it was asked for: a launch line is not the place.
        print(f'cw context: LLM Wiki search not used ({why[0]}); pages matched by name',
              file=sys.stderr)
    if not found:
        return None
    return len(found), render(project, found, how)
