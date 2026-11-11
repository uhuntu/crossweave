"""Headless continuation commands and a terminal conversation loop."""
import os
import shutil
import subprocess
import sys

from . import sessions

CHAT_TOOLS = ('claude', 'codex', 'kimi', 'step', 'pi', 'codebuddy', 'zcode')


def available(tool):
    if tool == 'zcode':
        return sessions.zcode_cli_on_path()
    return tool in CHAT_TOOLS and shutil.which(tool) is not None


def turn_command(tool, sid, text):
    """Continue the exact session, keeping each tool's permission defaults."""
    if tool == 'zcode':
        return ['zcode', '--resume', sid, '-p', text]
    if tool == 'kimi':
        return ['kimi', '-S', sid, '-p', text]
    if tool == 'codex':
        return ['codex', 'exec', 'resume', '--skip-git-repo-check', sid, '--', text]
    flag = {'claude': '--resume', 'step': '--resume', 'pi': '--session',
            'codebuddy': '-r'}.get(tool)
    if flag:
        return [tool, flag, sid, '-p', '--', text]
    raise ValueError('该工具暂不支持连续聊天')


def cmd_chat(args):
    if len(args) != 2 or args[0] not in CHAT_TOOLS:
        print(f'Usage: cw chat <{"|".join(CHAT_TOOLS)}> <session-id-or-prefix>', file=sys.stderr)
        return 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('cw chat: needs an interactive terminal; use the tool’s non-interactive mode',
              file=sys.stderr)
        return 1
    tool = args[0]
    hits = sessions.ID_RESOLVERS[tool](args[1])
    if len(hits) != 1:
        print(f'cw chat: {len(hits)} sessions match; use a unique saved session ID', file=sys.stderr)
        return 1
    if not available(tool):
        print(f'cw chat: the {tool} terminal CLI is not on PATH', file=sys.stderr)
        return 1
    sid = hits[0]
    if tool == 'zcode':
        cwd = sessions.zcode_session_cwd(sid)
    else:
        details = sessions.session_handoff_details(tool, sid)
        if details is None:
            print('cw chat: session transcript unavailable', file=sys.stderr)
            return 1
        cwd = details[0]
    if cwd and not os.path.isdir(cwd):
        print(f'cw chat: session directory unavailable: {cwd}', file=sys.stderr)
        return 1
    print(f'{tool} session: {sid}\nDirectory: {cwd or os.getcwd()}\n'
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
            rc = subprocess.call(sessions.spawnable(turn_command(tool, sid, text)),
                                 cwd=cwd)
        except KeyboardInterrupt:
            print('\nTurn interrupted. Enter another message or /quit.')
            continue
        except OSError as exc:
            print(f'cw chat: could not start {tool}: {exc}', file=sys.stderr)
            return 1
        if rc:
            print(f'cw chat: {tool} exited with status {rc}; enter another message to retry',
                  file=sys.stderr)
