"""A terminal input loop over ZCode's persisted headless sessions."""
import os
import subprocess
import sys

from . import sessions


def cmd_chat(args):
    if len(args) != 2 or args[0] != 'zcode':
        print('Usage: cw chat zcode <session-id-or-prefix>', file=sys.stderr)
        return 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('cw chat: needs an interactive terminal; use `cw resume zcode <id> "instruction"`',
              file=sys.stderr)
        return 1
    hits = sessions.ID_RESOLVERS['zcode'](args[1])
    if len(hits) != 1:
        print(f'cw chat: {len(hits)} sessions match; use a unique saved session ID', file=sys.stderr)
        return 1
    if not sessions.zcode_cli_on_path():
        print('cw chat: the zcode terminal CLI is not on PATH', file=sys.stderr)
        return 1
    sid = hits[0]
    cwd = sessions.zcode_session_cwd(sid)
    if cwd and not os.path.isdir(cwd):
        print(f'cw chat: session directory unavailable: {cwd}', file=sys.stderr)
        return 1
    print(f'ZCode session: {sid}\nDirectory: {cwd or os.getcwd()}\n'
          'Type a message; /quit exits. Ctrl-D or Ctrl-C at the prompt also exits.')
    while True:
        try:
            text = input('\nYou> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in ('/quit', '/exit'):
            return 0
        if not text:
            continue
        try:
            rc = subprocess.call(sessions.spawnable(['zcode', '--resume', sid, '-p', text]),
                                 cwd=cwd)
        except KeyboardInterrupt:
            print('\nTurn interrupted. Enter another message or /quit.')
            continue
        except OSError as exc:
            print(f'cw chat: could not start zcode: {exc}', file=sys.stderr)
            return 1
        if rc:
            print(f'cw chat: zcode exited with status {rc}; enter another message to retry',
                  file=sys.stderr)
