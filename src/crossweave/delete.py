"""Remove a local session from cw's scanned store, retaining a file backup."""
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile

from . import agy, codex, common, opencode, sessions, zcode


def _paths(record):
    tool = record['tool']
    if tool == 'codex':
        # A resumed thread can own multiple rollouts; removing just the newest
        # would expose the older conversation again on the next scan.
        return [p for p in codex.codex_rollout_files()
                if (m := common.UUID_RE.search(os.path.basename(p)))
                and m.group() == record['id']]
    if tool == 'kimi':
        return [record['dir']] if record.get('dir') else []
    if tool in ('claude', 'step', 'pi'):
        return [record['path']] if record.get('path') else []
    if tool == 'agy':
        sid = record['id']
        candidates = [
            os.path.join(agy.AGY_BRAIN, sid),
            os.path.join(agy.AGY_DIR, 'annotations', f'{sid}.pbtxt'),
            os.path.join(agy.AGY_DIR, 'conversations', f'{sid}.db'),
            os.path.join(agy.AGY_DIR, 'conversations', f'{sid}.db-wal'),
            os.path.join(agy.AGY_DIR, 'conversations', f'{sid}.db-shm'),
        ]
        return [p for p in candidates if os.path.exists(p)]
    return []


def _agy_backup_row(sid):
    if not os.path.isfile(agy.AGY_DB):
        return None
    con = sqlite3.connect(f'file:{agy.AGY_DB.replace(chr(92), "/")}?mode=ro', uri=True)
    try:
        cur = con.execute(
            'SELECT * FROM conversation_summaries WHERE conversation_id = ?', (sid,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        out = {}
        for k, v in zip(cols, row):
            out[k] = base64.b64encode(v).decode('ascii') if isinstance(v, (bytes, bytearray)) else v
        return out
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _agy_delete_db(sid):
    if not os.path.isfile(agy.AGY_DB):
        return
    con = sqlite3.connect(agy.AGY_DB, timeout=10)
    try:
        with con:
            con.execute('DELETE FROM conversation_summaries WHERE conversation_id = ?', (sid,))
            for table in ('conversations_fts', 'conversation_parents'):
                try:
                    con.execute(f'DELETE FROM {table} WHERE conversation_id = ?', (sid,))
                except sqlite3.OperationalError:
                    pass
    finally:
        con.close()
    agy.agy_clear_custom_title(sid)
    agy._drop_connection()


def _opencode_backup_rows(sid):
    if not os.path.isfile(opencode.OPENCODE_DB):
        return None
    con = sqlite3.connect(f'file:{opencode.OPENCODE_DB.replace(chr(92), "/")}?mode=ro', uri=True)
    try:
        cur = con.execute('SELECT * FROM session_v2 WHERE id = ?', (sid,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        session_row = dict(zip(cols, row))
        mcur = con.execute(
            'SELECT * FROM session_message WHERE session_id = ? ORDER BY seq', (sid,))
        mcols = [d[0] for d in mcur.description]
        messages = [dict(zip(mcols, mr)) for mr in mcur.fetchall()]
        return {'session': session_row, 'messages': messages}
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _opencode_delete_db(sid):
    if not os.path.isfile(opencode.OPENCODE_DB):
        return
    con = sqlite3.connect(opencode.OPENCODE_DB, timeout=10)
    try:
        with con:
            con.execute('DELETE FROM session_message WHERE session_id = ?', (sid,))
            con.execute('DELETE FROM session_v2 WHERE id = ?', (sid,))
    finally:
        con.close()
    opencode._drop_connection()


def _zcode_backup_rows(sid):
    if not os.path.isfile(zcode.ZCODE_DB):
        return None
    con = sqlite3.connect(f'file:{zcode.ZCODE_DB.replace(chr(92), "/")}?mode=ro', uri=True)
    try:
        cur = con.execute('SELECT * FROM session WHERE id = ?', (sid,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        session_row = dict(zip(cols, row))
        mcur = con.execute('SELECT * FROM message WHERE session_id = ? ORDER BY sequence', (sid,))
        mcols = [d[0] for d in mcur.description]
        messages = [dict(zip(mcols, mr)) for mr in mcur.fetchall()]
        pcur = con.execute(
            'SELECT * FROM part WHERE message_id IN '
            '(SELECT id FROM message WHERE session_id = ?) ORDER BY sequence', (sid,))
        pcols = [d[0] for d in pcur.description]
        parts = [dict(zip(pcols, pr)) for pr in pcur.fetchall()]
        return {'session': session_row, 'messages': messages, 'parts': parts}
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _zcode_delete_db(sid):
    if not os.path.isfile(zcode.ZCODE_DB):
        return
    con = sqlite3.connect(zcode.ZCODE_DB, timeout=10)
    try:
        with con:
            con.execute(
                'DELETE FROM part WHERE message_id IN '
                '(SELECT id FROM message WHERE session_id = ?)', (sid,))
            con.execute('DELETE FROM message WHERE session_id = ?', (sid,))
            con.execute('DELETE FROM session WHERE id = ?', (sid,))
    finally:
        con.close()
    zcode._drop_connection()


def cmd_delete(args):
    if args in (['-h'], ['--help']):
        print('Usage: cw delete <N> [-y]  (or cw <N> delete [-y])\n'
              'Remove local session files, keeping a backup. -y skips confirmation.')
        return 0
    yes = '-y' in args or '--yes' in args
    positional = [a for a in args if a not in ('-y', '--yes')]
    if len(positional) != 1 or not sessions.ROW_NUMBER_RE.fullmatch(positional[0]):
        print('Usage: cw delete <N> [-y]', file=sys.stderr)
        return 2
    cache = sessions.read_list_cache()
    n = int(positional[0])
    if not 1 <= n <= len(cache):
        print('cw delete: row unavailable -- run `cw` to refresh the list', file=sys.stderr)
        return 1
    entry = cache[n - 1]
    if entry.get('machine'):
        print(f"cw delete: row lives on {entry['machine']} -- delete it there", file=sys.stderr)
        return 1
    if entry.get('extra_home'):
        print(f"cw delete: row lives in read-only extra home {entry['extra_home']} "
              "-- refusing to delete", file=sys.stderr)
        return 1
    record = sessions._find_record(entry['tool'], entry['id'])
    if record is None:
        print('cw delete: session no longer exists -- run `cw` to refresh', file=sys.stderr)
        return 1
    if record.get('extra_home'):
        print(f"cw delete: row lives in read-only extra home {record['extra_home']} "
              "-- refusing to delete", file=sys.stderr)
        return 1
    paths = _paths(record)
    if not paths and entry['tool'] not in ('agy', 'opencode', 'zcode'):
        print(f"cw delete: {entry['tool']} deletion is not supported", file=sys.stderr)
        return 1
    print(f"Delete [{entry['tool']}] {entry['id']}?")
    if not yes:
        if not sys.stdin.isatty():
            print('cw delete: not a terminal -- pass -y to delete', file=sys.stderr)
            return 1
        try:
            answer = input('Remove session files and keep a backup? [y/N] ')
        except (EOFError, KeyboardInterrupt):
            print('\nNothing deleted.')
            return 0
        if answer.strip().lower() not in ('y', 'yes'):
            print('Nothing deleted.')
            return 0
    moved = []
    try:
        root = os.path.join(os.path.dirname(sessions.LIST_CACHE_FILE), 'deleted')
        os.makedirs(root, exist_ok=True)
        backup = tempfile.mkdtemp(prefix=entry['tool'] + '-', dir=root)
        manifest = {'tool': entry['tool'], 'id': entry['id'], 'files': []}
        if entry['tool'] == 'agy':
            row = _agy_backup_row(entry['id'])
            if row is not None:
                manifest['db_row'] = row
        elif entry['tool'] == 'opencode':
            rows = _opencode_backup_rows(entry['id'])
            if rows is not None:
                manifest['db_rows'] = rows
        elif entry['tool'] == 'zcode':
            rows = _zcode_backup_rows(entry['id'])
            if rows is not None:
                manifest['db_rows'] = rows
        for i, path in enumerate(paths):
            target = os.path.join(backup, str(i))
            manifest['files'].append({'original': os.path.abspath(path), 'backup': target})
        with open(os.path.join(backup, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        for item in manifest['files']:
            shutil.move(item['original'], item['backup'])
            moved.append(item)
        if entry['tool'] == 'agy':
            _agy_delete_db(entry['id'])
        elif entry['tool'] == 'opencode':
            _opencode_delete_db(entry['id'])
        elif entry['tool'] == 'zcode':
            _zcode_delete_db(entry['id'])
    except (OSError, sqlite3.Error) as exc:
        # Restore earlier moves if a multi-file deletion fails partway through.
        for item in reversed(moved):
            try:
                shutil.move(item['backup'], item['original'])
            except OSError as restore_error:
                print(f'cw delete: restore failed: {restore_error}', file=sys.stderr)
        print(f'cw delete: {exc}', file=sys.stderr)
        return 1
    codex._codex_path_index = None
    # Keep the cached numbering: shifting it could delete the wrong row when
    # the user runs another delete against the list still on their screen.
    print(f'Deleted session files. Backup: {backup}')
    return 0
