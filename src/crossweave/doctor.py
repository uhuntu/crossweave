"""cw doctor - per-tool store health, so a store that stopped yielding
sessions is distinguishable from one that never had any.

Every reader in this package treats an unreadable store as an empty one:
read_jsonl skips what it cannot parse, a missing directory reads as no
sessions, a SQLite error reads as []. That tolerance is what keeps a
listing alive over one half-written file, and it has a price -- when a
tool changes its schema, sessions silently vanish and nothing says why.
doctor asks each store the questions the readers swallow: is it there,
can it be opened, how many sessions came out, and do the numbers add up
(a store holding transcript files that resolve to no sessions is telling
you something different from an empty one).

The counts come from the same light-record readers the listing uses, so
what doctor reports is what `cw sessions` would show -- not an
independent re-derivation that could disagree with it.
"""
import glob
import os
import sqlite3
import sys

from . import codebuddy, color, common, step, workbuddy, zcode
from .claude import claude_light_records
from .codebuddy import codebuddy_light_records
from .codex import codex_light_records, codex_rollout_files
from .kimi import kimi_index, kimi_light_records
from .sessions import TOOLS, harden_console_output
from .step import step_light_records
from .zcode import zcode_light_records

# Statuses, worst last: a store that is not there is ordinary (you do not
# use that tool), a store that is there but yields nothing is worth a
# look, and a store that is there but cannot be read is the one doctor
# exists for.
OK, EMPTY, MISSING, BROKEN = "ok", "empty", "missing", "broken"

_STATUS_COLOR = {OK: "32", EMPTY: "33", MISSING: "2", BROKEN: "31"}

TOOL_WIDTH = 10


def _file_count(pattern):
    return len(glob.glob(pattern))


def _check_claude():
    root = common.CLAUDE_PROJECTS
    if not os.path.isdir(root):
        return MISSING, 0, None
    files = _file_count(os.path.join(root, "*", "*.jsonl"))
    records = len(claude_light_records())
    return _verdict(files, records)


def _check_codex():
    root = os.path.join(common.CODEX_HOME, "sessions")
    if not os.path.isdir(root):
        return MISSING, 0, None
    files = len(codex_rollout_files())
    records = len(codex_light_records())
    return _verdict(files, records)


def _check_kimi():
    index = os.path.join(common.KIMI_HOME, "session_index.jsonl")
    if not os.path.isfile(index):
        return MISSING, 0, None
    records = len(kimi_light_records(show_all=True))
    return _verdict(len(kimi_index()), records)


def _check_step():
    root = step.STEP_SESSIONS
    if not os.path.isdir(root):
        return MISSING, 0, None
    files = (_file_count(os.path.join(root, "*", "*.jsonl"))
             + _file_count(os.path.join(root, "*.jsonl")))
    records = len(step_light_records(show_all=True))
    return _verdict(files, records)


def _check_zcode():
    db = zcode.ZCODE_DB
    if not os.path.isfile(db):
        return MISSING, 0, None
    # The difference between empty and broken hinges on this probe: the
    # reader's _query answers a schema mismatch with [], so ask the schema
    # directly. All three tables -- a store missing only `message` or `part`
    # lists sessions whose turns and last lines silently come back empty.
    con = zcode._connect()
    if con is None:
        return BROKEN, 0, "the store exists but cannot be opened"
    try:
        con.execute("SELECT 1 FROM session LIMIT 1")
        con.execute("SELECT 1 FROM message LIMIT 1")
        con.execute("SELECT 1 FROM part LIMIT 1")
    except sqlite3.Error:
        return BROKEN, 0, ("the db opens but is missing a table this reader "
                           "queries (session/message/part) -- schema changed?")
    records = len(zcode_light_records(show_all=True))
    note = "CLI on PATH" if zcode.zcode_cli_on_path() else "desktop app only"
    return OK, records, note


def _check_codebuddy():
    root = codebuddy.CODEBUDDY_PROJECTS
    if not os.path.isdir(root):
        return MISSING, 0, None
    files = _file_count(os.path.join(root, "*", "*.jsonl"))
    records = len(codebuddy_light_records())
    return _verdict(files, records)


def _check_workbuddy():
    """Both editions' stores read as the one tool they are listed under.

    A machine with only one of them installed is the normal case rather than
    a half-broken one, so the note names the editions that keep a store here
    -- and says plainly that there is no CLI, the way zcode's desktop-only
    case does, since that is why a resume opens the app."""
    roots = workbuddy.workbuddy_roots_present()
    if not roots:
        return MISSING, 0, None
    files = sum(_file_count(os.path.join(root, workbuddy.PROJECTS_DIRNAME, "*", "*.jsonl"))
                for root, _ in roots)
    records = len(workbuddy.workbuddy_light_records())
    state, count, note = _verdict(files, records)
    if state is not OK:
        return state, count, note
    editions = " + ".join(scheme for _, scheme in roots)
    suffix = f"desktop app only ({editions})"
    return OK, count, f"{note}; {suffix}" if note else suffix


def _verdict(files, records):
    """ok/empty/broken from the two counts a file-backed store offers.

    A store with transcript files that resolve to no sessions is not
    empty -- the files are there and the reader saw none of them, which
    is the shape a format change takes before it takes everything."""
    if files and not records:
        return BROKEN, 0, (f"{files} transcript file(s) resolved to no sessions "
                           "-- the records may not be the shape this reader knows")
    if records:
        note = None
        if files and files > records:
            note = f"{files} files on disk"
        return OK, records, note
    return EMPTY, 0, None


CHECKS = (
    ("claude", _check_claude),
    ("codex", _check_codex),
    ("kimi", _check_kimi),
    ("step", _check_step),
    ("zcode", _check_zcode),
    ("codebuddy", _check_codebuddy),
    ("workbuddy", _check_workbuddy),
)


def cmd_doctor(argv):
    tool_filter = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tool":
            if i + 1 >= len(argv):
                print("cw doctor: --tool requires a value", file=sys.stderr)
                sys.exit(1)
            tool_filter = argv[i + 1]
            if tool_filter not in TOOLS:
                print(f"cw doctor: --tool must be one of {', '.join(TOOLS)}", file=sys.stderr)
                sys.exit(1)
            i += 2
        else:
            print(f"cw doctor: unknown option '{a}'", file=sys.stderr)
            sys.exit(1)

    worst = 0
    for tool, check in CHECKS:
        if tool_filter and tool != tool_filter:
            continue
        status, count, note = check()
        if status == BROKEN:
            worst = 1
        name = tool.ljust(TOOL_WIDTH)
        counts = f"{count} session{'s' if count != 1 else ''}"
        line = f"{name} {color.paint(status.ljust(7), _STATUS_COLOR[status])} {counts.rjust(6)}"
        if note:
            line += f"  ({note})"
        print(line.rstrip())
    return worst
