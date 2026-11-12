"""Keep an LLM Wiki fed without being asked: no daemon, no cron.

With CW_LLM_WIKI set, an ordinary `cw` -- a listing, a fresh session --
checks one small state file, and once a day (CW_LLM_WIKI_EXPORT=12h to
change it, =off to stop) starts `cw export --all` in the background and
goes on at once. The export takes what has gone quiet for a day, from as
far back as the last successful run could have missed, and asks the app
to rescan. It runs in the environment of the command that started it, so
the token the shell loaded reaches it. A run that failed is named once,
on the next listing, with the log to read, and is tried again within the
hour."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import exporter, llmwiki, sessions

ENV = 'CW_LLM_WIKI_EXPORT'
CHILD_ENV = 'CW_LLM_WIKI_EXPORT_CHILD'
DEFAULT_INTERVAL = '1d'
# A topic waits this long after its last activity: exported before, a
# session about to be continued goes in alone and is torn down again.
QUIET = '1d'
FIRST_SPAN = 7 * 86400
MAX_SPAN = 90 * 86400
# A run that started this long ago and never finished is taken for dead.
STALE_RUN = 3600
RETRY_FAILED = 3600
LOG_BYTES = 1_000_000
OFF = ('0', 'off', 'no', 'false')


def _state_dir():
    return Path(sessions.HANDOFF_DIR).parent


def state_path():
    return _state_dir() / 'llm-wiki-export.json'


def log_path():
    return _state_dir() / 'llm-wiki-export.log'


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


def _interval():
    """Seconds between runs, or None when off (or unreadable -- said once
    per command, then treated as off rather than guessed)."""
    from . import note

    raw = os.environ.get(ENV, '').strip().lower() or DEFAULT_INTERVAL
    if raw in OFF:
        return None
    seconds = note._parse_since(raw)
    if seconds is None:
        print(f'cw: {ENV}={raw} is not a span like 12h or 1d; LLM Wiki auto-export is off',
              file=sys.stderr)
    return seconds


def _key(wiki):
    return str(Path(wiki).expanduser().resolve())


def maybe_start():
    """Start a background export when one is due. Never raises, never waits."""
    try:
        _maybe_start()
    except Exception:  # a listing or a launch must not fail for the wiki
        pass


def _maybe_start():
    if os.environ.get(CHILD_ENV):
        return
    wiki = llmwiki.wiki_dir()
    if wiki is None:
        return
    interval = _interval()
    if interval is None:
        return
    state = _read_state()
    key = _key(wiki)
    mine = state.get(key) if isinstance(state.get(key), dict) else {}
    now = time.time()
    if mine.get('result') == 'failed' and not mine.get('notified'):
        print(f"cw: the last LLM Wiki auto-export failed ({mine.get('error') or 'see the log'}); "
              f'log: {log_path()}', file=sys.stderr)
        mine['notified'] = True
        state[key] = mine
        _write_state(state)
    started = mine.get('started') or 0
    finished = mine.get('finished') or 0
    if started > finished and now - started < STALE_RUN:
        return  # one is running
    # A failed run -- the app was closed, the token missing -- is tried
    # again within the hour, not a whole interval later.
    wait = min(interval, RETRY_FAILED) if mine.get('result') == 'failed' else interval
    if now - started < wait:
        return
    mine.update(started=now, notified=False)
    state[key] = mine
    _write_state(state)
    _spawn(wiki)
    print(f'cw: exporting quiet sessions to LLM Wiki in the background '
          f'(log: {log_path()}; {ENV}=off stops it)', file=sys.stderr)


def _spawn(wiki):
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
            'from crossweave import autoexport; sys.exit(autoexport.run(%r))'
            % (package_parent, str(wiki)))
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


class _LastError:
    """stderr that remembers the last `cw export: ...` error it was told,
    so the state can say what failed rather than that something did."""

    def __init__(self, stream):
        self.stream, self.last = stream, None

    def write(self, text):
        for line in text.splitlines():
            if line.startswith('cw export: ') and ' exported, ' not in line \
                    and ' sources from ' not in line:
                self.last = line[len('cw export: '):].strip()
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()


def run(wiki):
    """The background export itself. Returns the exit code."""
    state = _read_state()
    key = _key(wiki)
    mine = state.get(key) if isinstance(state.get(key), dict) else {}
    began = mine.get('started') or time.time()
    since = span_since(mine, time.time())
    print(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} cw LLM Wiki auto-export "
          f"(last {since // 3600}h, quiet for {QUIET}) ==", flush=True)
    opts = argparse.Namespace(since=f'{-(-since // 3600)}h', older_than=QUIET, cwd=None,
                              dry_run=False, yes=True, llm_wiki=str(wiki), rescan=True,
                              api_url=os.environ.get(llmwiki.API_ENV, '').strip() or exporter.API_URL,
                              project_id=None)
    error = None
    said = _LastError(sys.stderr)
    sys.stderr = said
    try:
        client = exporter.WikiClient(opts.api_url, os.environ.get(exporter.TOKEN_ENV, ''))
        code = exporter._export_all(opts, client)
    except (exporter.ExportError, OSError) as exc:
        print(f'cw export: {exc}', file=sys.stderr)
        code, error = 1, str(exc)
    except Exception as exc:
        print(f'cw export: {type(exc).__name__}: {exc}', file=sys.stderr)
        code, error = 1, f'{type(exc).__name__}: {exc}'
    finally:
        sys.stderr = said.stream
    sys.stdout.flush()
    sys.stderr.flush()
    error = error or said.last
    state = _read_state()
    mine = state.get(key) if isinstance(state.get(key), dict) else {}
    mine.update(finished=time.time(), result='ok' if code == 0 else 'failed',
                error=None if code == 0 else (error or 'export or rescan failed'))
    if code == 0:
        mine['succeeded'] = began
    state[key] = mine
    _write_state(state)
    return code
