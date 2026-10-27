"""Remove a local session from cw's scanned store, retaining a file backup."""
import json
import os
import shutil
import sys
import tempfile

from . import codex, common, sessions


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
    if tool in ('claude', 'step', 'codebuddy', 'workbuddy'):
        return [record['path']] if record.get('path') else []
    return []


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
    record = sessions._find_record(entry['tool'], entry['id'])
    if record is None:
        print('cw delete: session no longer exists -- run `cw` to refresh', file=sys.stderr)
        return 1
    paths = _paths(record)
    if not paths:
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
        for i, path in enumerate(paths):
            target = os.path.join(backup, str(i))
            manifest['files'].append({'original': os.path.abspath(path), 'backup': target})
        with open(os.path.join(backup, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        for item in manifest['files']:
            shutil.move(item['original'], item['backup'])
            moved.append(item)
    except OSError as exc:
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
