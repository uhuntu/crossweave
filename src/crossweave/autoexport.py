"""Keep the vault up to date without being asked: no daemon, no cron.

An ordinary `cw` -- a listing, a fresh session -- checks one small state
file, and once a day (CW_AUTO=12h to change it, =off to stop) starts the
day's upkeep in the background and goes on at once:

1. a note for every session or topic that has gone quiet for a day and has
   no note yet, or one behind it (one judge call each, four at a time);
   older history follows, at most 20 note attempts per vault per 24h, and
   the new notes of sessions started in ~ moved into the projects they are
   about (`cw note --sort-home`, one judge call);
2. the project pages those notes put behind (`cw wiki --projects`);
3. with CW_LLM_WIKI set, the notes exported into that LLM Wiki and a rescan
   asked for (CW_LLM_WIKI_EXPORT=off skips this step alone).

The vault is the point; the LLM Wiki is an optional reader of it. The run
reaches back as far as the last successful one could have missed, so a
fortnight away loses nothing. It runs in the environment of the command
that started it, so CW_VAULT and the LLM Wiki token the shell loaded reach
it. A run that failed is named once, on the next listing, with the log to
read, and is tried again within the hour."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import exporter, llmwiki, sessions

ENV = 'CW_AUTO'
WIKI_ENV = 'CW_LLM_WIKI_EXPORT'
CHILD_ENV = 'CW_AUTO_CHILD'
DEFAULT_INTERVAL = '1d'
# A session or topic waits this long after its last activity: noted
# before, one about to be continued is noted alone and then again.
QUIET = '1d'
FIRST_SPAN = 7 * 86400
MAX_SPAN = 90 * 86400
# A run that started this long ago and never finished is taken for dead.
STALE_RUN = 3600
RETRY_FAILED = 3600
LOG_BYTES = 1_000_000
OFF = ('0', 'off', 'no', 'false')
KEY = 'daily'
BACKFILL_LIMIT = 20
BACKFILL_WINDOW = 86400


def _state_dir():
    return Path(sessions.HANDOFF_DIR).parent


def state_path():
    return _state_dir() / 'daily.json'


def log_path():
    return _state_dir() / 'daily.log'


def _read_state():
    try:
        with open(state_path(), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data):
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def _off(name):
    return os.environ.get(name, '').strip().lower() in OFF


def _interval():
    """Seconds between runs, or None when off (or unreadable -- said once
    per command, then treated as off rather than guessed)."""
    from . import note

    raw = os.environ.get(ENV, '').strip().lower() or DEFAULT_INTERVAL
    if raw in OFF:
        return None
    seconds = note._parse_since(raw)
    if seconds is None:
        print(f'cw: {ENV}={raw} is not a span like 12h or 1d; the daily upkeep is off',
              file=sys.stderr)
    return seconds


def _targets():
    """(vault or None, LLM Wiki folder or None) the upkeep works on."""
    from . import note

    wiki = None if _off(WIKI_ENV) else llmwiki.wiki_dir()
    return note.vault_path(), wiki


def maybe_start():
    """Start the day's upkeep in the background when it is due. Never
    raises, never waits."""
    try:
        _maybe_start()
    except Exception:  # a listing or a launch must not fail for the vault
        pass


def _maybe_start():
    if os.environ.get(CHILD_ENV):
        return
    vault, wiki = _targets()
    if vault is None and wiki is None:
        return
    interval = _interval()
    if interval is None:
        return
    state = _read_state()
    mine = state.get(KEY) if isinstance(state.get(KEY), dict) else {}
    now = time.time()
    if mine.get('result') == 'failed' and not mine.get('notified'):
        print(f"cw: the last daily upkeep failed ({mine.get('error') or 'see the log'}); "
              f'log: {log_path()}', file=sys.stderr)
        mine['notified'] = True
        state[KEY] = mine
        _write_state(state)
    started = mine.get('started') or 0
    finished = mine.get('finished') or 0
    if started > finished and now - started < STALE_RUN:
        return  # one is running
    # A failed run -- the app was closed, a judge out of credit -- is tried
    # again within the hour, not a whole interval later.
    wait = min(interval, RETRY_FAILED) if mine.get('result') == 'failed' else interval
    if now - started < wait:
        return
    mine.update(started=now, notified=False)
    state[KEY] = mine
    _write_state(state)
    _spawn()
    steps = ['notes', 'project pages'] if vault else []
    if wiki:
        steps.append('LLM Wiki')
    print(f"cw: daily upkeep in the background -- {', '.join(steps)} "
          f'(log: {log_path()}; {ENV}=off stops it)', file=sys.stderr)


def _spawn():
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log.stat().st_size > LOG_BYTES:
            with open(log, 'rb') as fh:
                fh.seek(-LOG_BYTES // 2, os.SEEK_END)
                tail = fh.read()
            with open(log, 'wb') as fh:
                fh.write(tail)
    except OSError:
        pass
    package_parent = str(Path(__file__).resolve().parent.parent)
    code = ('import sys; sys.path.insert(0, %r); '
            'from crossweave import autoexport; sys.exit(autoexport.run())' % package_parent)
    env = dict(os.environ, **{CHILD_ENV: '1'})
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                                   | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        kwargs['start_new_session'] = True
    with open(log, 'ab') as out:
        subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, env=env, close_fds=True, **kwargs)


def span_since(mine, now):
    """How far back a run looks: far enough to reach every topic the last
    successful run could not take yet -- one that was not quiet then went
    quiet after (last success - QUIET) -- with an hour to spare."""
    from . import note

    last = mine.get('succeeded') or 0
    if not last:
        return FIRST_SPAN
    return int(min(MAX_SPAN, max(now - last + note._parse_since(QUIET) + 3600, 2 * 86400)))


def _write_quiet_notes(vault, since):
    """Step 1: notes for the quiet sessions and topics that have none, or one
    behind them. Returns the first failure's text, or None."""
    from . import note, search

    picked, _skipped, _empty = exporter.pick_sessions(since, None, note._parse_since(QUIET))
    state = exporter.notes_state(vault)
    need = [u for u in picked if exporter.fresh_note(u, state) is None]
    print(f'cw note: {len(need)} of {len(picked)} quiet sessions and topics need a note', flush=True)
    if not need:
        return None
    _notes, failed = exporter.write_notes(need, vault, search.DEFAULT_JUDGE, prog='cw note')
    if failed:
        return f'{len(failed)} note(s) not written: {failed[0][1]}'
    return None


def _backfill_notes(vault, since):
    """Catch up outside the recent window, at most 20 attempts per vault
    in 24 hours. Reserve attempts before calling the judge so retries of
    unrelated failed steps cannot spend another whole batch."""
    from . import note, search

    now = time.time()
    state = _read_state()
    quotas = state.setdefault('backfill', {})
    key = str(Path(vault).resolve())
    quota = quotas.get(key, {})
    if now - quota.get('started', 0) >= BACKFILL_WINDOW:
        quota = {'started': now, 'attempted': 0}
    remaining = max(0, BACKFILL_LIMIT - quota.get('attempted', 0))
    if not remaining:
        print('cw note: historical backfill budget used; waiting for the next day', flush=True)
        return None
    picked, _skipped, _empty = exporter.pick_sessions(None, None, note._parse_since(QUIET))
    notes = exporter.notes_state(vault)
    cutoff = now - since
    need = [u for u in picked
            if all(exporter._ts(r) and exporter._ts(r) < cutoff for r in u['members'])
            and exporter.fresh_note(u, notes) is None]
    batch = need[:remaining]
    print(f'cw note: historical backfill {len(batch)} of {len(need)} pending '
          f'(limit {BACKFILL_LIMIT} attempts per 24h)', flush=True)
    if not batch:
        return None
    quota['attempted'] = quota.get('attempted', 0) + len(batch)
    quota.setdefault('started', now)
    quotas[key] = quota
    _write_state(state)
    _notes, failed = exporter.write_notes(batch, vault, search.DEFAULT_JUDGE, prog='cw note history')
    if failed:
        return f'{len(failed)} historical note(s) not written: {failed[0][1]}'
    return None


def _sort_home(vault):
    """Step 1b: notes of sessions started in ~ moved into the projects they
    are about (one judge call for all of them). Returns None; a judge that
    is unavailable leaves _home as it is, which is no failure."""
    from . import note, search

    if note.unsorted_home_notes(vault):
        note.sort_home(vault, search.DEFAULT_JUDGE, yes=True)
    return None


def _refresh_project_pages(vault):
    """Step 2: the project pages the notes put behind (one judge call
    each). Returns the first failure's text, or None."""
    from . import search, wiki

    stale = wiki.stale_projects(vault)
    if stale:
        print(f"cw wiki: updating {len(stale)} project page(s): {', '.join(stale)}", flush=True)
    failed = []
    for name in stale:
        path = wiki.build_project(vault, name, search.DEFAULT_JUDGE)
        if path:
            print(f'wiki: {path}', flush=True)
        else:
            failed.append(name)
    return f"project page(s) not updated: {', '.join(failed)}" if failed else None


class _LastError:
    """stderr that remembers the last `cw export: ...` error it was told,
    so the state can say what failed rather than that something did."""

    def __init__(self, stream):
        self.stream, self.last = stream, None

    def write(self, text):
        for line in text.splitlines():
            if line.startswith('cw export: ') and ' exported, ' not in line \
                    and ' sources from ' not in line and ' need a note' not in line:
                self.last = line[len('cw export: '):].strip()
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()


def _export_to_wiki(wiki, since):
    """Step 3: the notes into the LLM Wiki, and a rescan. Returns the
    failure's text, or None."""
    opts = argparse.Namespace(since=f'{-(-since // 3600)}h', older_than=QUIET, cwd=None,
                              dry_run=False, yes=True, llm_wiki=str(wiki), rescan=True,
                              content=exporter.CONTENT_NOTES, judge=None,
                              api_url=os.environ.get(llmwiki.API_ENV, '').strip() or exporter.API_URL,
                              project_id=None)
    said = _LastError(sys.stderr)
    sys.stderr = said
    error = None
    try:
        client = exporter.WikiClient(opts.api_url, os.environ.get(exporter.TOKEN_ENV, ''))
        if exporter._export_all(opts, client) != 0:
            error = said.last or 'export or rescan failed'
    except (exporter.ExportError, OSError) as exc:
        print(f'cw export: {exc}', file=sys.stderr)
        error = str(exc)
    except Exception as exc:
        print(f'cw export: {type(exc).__name__}: {exc}', file=sys.stderr)
        error = f'{type(exc).__name__}: {exc}'
    finally:
        sys.stderr = said.stream
    return error


def _step(name, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # one step failing does not stop the next
        print(f'cw: {name} failed: {type(exc).__name__}: {exc}', flush=True)
        return f'{name}: {type(exc).__name__}: {exc}'


def run():
    """The day's upkeep itself. Returns the exit code."""
    vault, wiki = _targets()
    state = _read_state()
    mine = state.get(KEY) if isinstance(state.get(KEY), dict) else {}
    began = mine.get('started') or time.time()
    since = span_since(mine, time.time())
    print(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} cw daily upkeep "
          f"(last {since // 3600}h, quiet for {QUIET}) ==", flush=True)
    errors = []
    if vault:
        errors.append(_step('notes', _write_quiet_notes, vault, since))
        errors.append(_step('historical notes', _backfill_notes, vault, since))
        errors.append(_step('sorting _home', _sort_home, vault))
        errors.append(_step('project pages', _refresh_project_pages, vault))
    if wiki:
        errors.append(_step('LLM Wiki export', _export_to_wiki, wiki, since))
    sys.stdout.flush()
    sys.stderr.flush()
    errors = [e for e in errors if e]
    state = _read_state()
    mine = state.get(KEY) if isinstance(state.get(KEY), dict) else {}
    mine.update(finished=time.time(), result='failed' if errors else 'ok',
                error=errors[0] if errors else None)
    if not errors:
        mine['succeeded'] = began
    state[KEY] = mine
    _write_state(state)
    return 1 if errors else 0
