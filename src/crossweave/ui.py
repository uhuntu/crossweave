"""cw ui - an interactive session picker in the terminal.

The same sessions `cw sessions` lists, in one scrollable list: type to
filter, see where a session left off, press Enter to resume it. Built on the
standard library's curses, so it adds no dependency; where curses is missing
(stock Python on Windows) it says so and points at the plain listing.

The picker only chooses. Everything it does after a choice goes through the
commands that already exist -- resume, handoff, note, delete, rename -- and
runs after curses has handed the terminal back, so a tool that starts (or a
y/N question) gets a normal terminal rather than one curses is still holding.
Those commands take a row of the last listing, so the picker writes what it
shows as that listing first: afterwards `cw resume 3` means the third row the
picker had on screen. Every action comes back to the list when it is done --
note, delete and rename right away; resume and handoff when the tool they
started is quit, so quitting a session lands on the list, not the shell.
"""
import os
import subprocess
import sys

from . import common, delete, note, rename, sessions, wiki
from .render import _clip, _display_width, TURNS_WIDTH

KEYS_HELP = ("↑↓ move  →← unfold  / filter  Tab tool  . dir  Enter resume  "
             "h handoff  n note  r rename  d delete  q quit")

# The tools a session can be handed to: every one with a CLI to start.
# workbuddy has none (perform_handoff refuses it), so it is never offered.
HANDOFF_TARGETS = tuple(t for t in sessions.TOOLS if t != "workbuddy")

# Keys that leave the list to run a command on the highlighted row.
ACTION_KEYS = {"n": "note", "d": "delete", "r": "rename"}

# Colors per tool, as curses pair numbers; the ANSI palette in color.py is
# escape codes, which curses cannot take.
TOOL_COLORS = {
    "claude": "YELLOW", "codex": "GREEN", "kimi": "CYAN", "step": "MAGENTA",
    "zcode": "BLUE", "codebuddy": "RED", "workbuddy": "WHITE",
    "pi": "YELLOW", "opencode": "WHITE",
}


def gather():
    """[(row, light record)] for every session the default listing would
    show, newest first: the same two drops cmd_list makes without --all (a
    Claude session /clear emptied, and sessions a tool started for itself)."""
    light = sessions._all_light_records()
    light.sort(key=lambda r: r["ts"], reverse=True)
    out = []
    for r in light:
        if r["tool"] == "claude" and r.get("path") and sessions.claude_is_empty(r["path"]):
            continue
        row = sessions.resolve_row(r)
        if sessions.is_tool_started_row(row):
            continue
        out.append((row, r))
    return out


def matches(row, words, tool, here):
    """Every filter word appears in the row's title, cwd, tool or id (case
    folded), the row is the chosen tool's, and -- with `here` set -- the
    row's directory is that one."""
    if tool and row[0] != tool:
        return False
    if here and not common._same_path(row[4], here):
        return False
    hay = " ".join((row[0], row[1], row[4] or "", row[5] or "")).casefold()
    return all(w in hay for w in words)


class Picker:
    def __init__(self, items, cwd):
        self.items = items
        self.cwd = cwd
        self.query = ""
        self.typing = False
        self.handoff_to = None  # the target list while asking "hand off to?"
        self.tool = None
        self.here_only = False
        self.expanded = set()  # topic keys unfolded to show every session
        self.cursor = 0
        self.top = 0
        self.details = {}  # full id -> (turns, last message), filled on view
        self.turns = {}  # full id -> turn count, filled as rows render
        self.noted = note.noted_index()
        self.topic_noted = note.topic_noted_index()
        self.wiked = wiki.wiki_index()
        self.tools = [t for t in sessions.TOOLS if any(r[0] == t for r, _ in items)]
        self.refilter()

    def reload(self, items):
        """New items after a rename or delete; the filters stay, and the
        cursor stays on the same session when it is still there."""
        keep = self.selected()
        keep_id = keep[0][1] if keep else None
        self.items = items
        self.details = {}
        self.turns = {}
        self.noted = note.noted_index()
        self.topic_noted = note.topic_noted_index()
        self.wiked = wiki.wiki_index()
        self.tools = [t for t in sessions.TOOLS if any(r[0] == t for r, _ in items)]
        if self.tool not in self.tools:
            self.tool = None
        self.refilter()
        for n, (row, _rec) in enumerate(self.shown):
            if row[1] == keep_id:
                self.cursor = n
                break

    def refilter(self):
        words = self.query.casefold().split()
        here = self.cwd if self.here_only else None
        matched = [it for it in self.items if matches(it[0], words, self.tool, here)]
        # One row per topic: a topic's sessions fold into the newest, which
        # carries the count; → unfolds them under it, newest first. The
        # picker holds every session, so the count is the whole history's.
        groups = {}
        for it in matched:
            key = sessions.topic_key(it[0][5])
            if key is not None:
                groups.setdefault(key, []).append(it)
        self.shown = []
        self.kind = []  # per shown row: "row", "head" or "member"
        self.members = {}  # head id -> [(row, rec)] of its topic, newest first
        self.key_of = {}  # head id -> its topic key
        for it in matched:
            key = sessions.topic_key(it[0][5])
            group = groups.get(key) if key is not None else None
            if not group or len(group) < 2:
                self.shown.append(it)
                self.kind.append("row")
                continue
            if group[0] is not it:
                continue  # folded under its head
            self.shown.append(it)
            self.kind.append("head")
            self.members[it[0][1]] = group
            self.key_of[it[0][1]] = key
            if key in self.expanded:
                for member in group[1:]:
                    self.shown.append(member)
                    self.kind.append("member")
        self.cursor = min(self.cursor, max(0, len(self.shown) - 1))
        self.topic_count = {sid: len(group) for sid, group in self.members.items()}

    def write_cache(self):
        """Make the rows on screen the last listing, as the row-number
        commands read it. A folded row carries its topic's sessions, so
        `n` on it notes the topic -- the same as `cw note <N>` on the plain
        listing's folded row."""
        sessions.write_list_cache([
            sessions.list_cache_entry(
                r, [m[0] for m in self.members[r[1]]]
                if self.kind[n] == "head" else None)
            for n, (r, _rec) in enumerate(self.shown)])

    def head_index(self, n):
        """The row a member row hangs under (n itself for any other row)."""
        while n > 0 and self.kind[n] == "member":
            n -= 1
        return n

    def toggle(self, unfold):
        """Unfold (or fold) the topic under the cursor. Folding from a member
        row lands the cursor on its topic's row."""
        if not self.shown:
            return
        n = self.head_index(self.cursor)
        if self.kind[n] != "head":
            return
        sid = self.shown[n][0][1]
        key = self.key_of[sid]
        if unfold is None:
            unfold = key not in self.expanded
        if unfold:
            self.expanded.add(key)
        else:
            self.expanded.discard(key)
            self.cursor = n
        self.refilter()

    def topic_note(self, sid):
        """"✎" / "✎+" (behind its sessions) for a topic row with a topic
        note, else None."""
        if sid not in self.members:
            return None
        (_tool, _sid, _when, _s, cwd, title), _rec = self.members[sid][0]
        got = self.topic_noted.get((note._project(cwd), sessions.topic_key(title)))
        if not got:
            return None
        stale = note.topic_note_is_stale([rec for _r, rec in self.members[sid]], got)
        return "✎+" if stale else "✎"

    def topic_turns(self, sid):
        """Every turn the topic's sessions hold, or None when none counts."""
        counts = [self.turns_of(rec) for _row, rec in self.members.get(sid, ())]
        counts = [c for c in counts if c]
        return sum(counts) if counts else None

    def selected(self):
        return self.shown[self.cursor] if self.shown else None

    def turns_of(self, record):
        """The row's turn count, computed on first render and cached -- the
        same walk the plain listing pays for every row, spent here only on
        rows a screen actually showed."""
        sid = record["id"]
        if sid not in self.turns:
            try:
                self.turns[sid] = sessions.session_turns(record)
            except Exception:
                self.turns[sid] = None
        return self.turns[sid]

    def detail(self, record):
        sid = record["id"]
        if sid not in self.details:
            last = None
            try:
                last = sessions.session_last_message(record)
            except Exception:
                pass
            self.details[sid] = (self.turns_of(record), last)
        return self.details[sid]

    # -- keys ---------------------------------------------------------------

    def key(self, ch, page):
        """Apply one key. Returns None, "quit", "resume", "note", "delete",
        "rename", or ("handoff", target tool)."""
        if self.handoff_to is not None:
            targets, self.handoff_to = self.handoff_to, None
            if len(ch) == 1 and ch.isdigit() and 1 <= int(ch) <= len(targets):
                return ("handoff", targets[int(ch) - 1])
            return None  # any other key cancels
        if self.typing:
            if ch in ("\n", "\r", "KEY_ENTER"):
                # type, then Enter: resume the highlighted row, fzf-style
                self.typing = False
                return "resume" if self.shown else None
            if ch == "\x1b":
                self.typing = False
                self.query = ""
            elif ch in ("KEY_BACKSPACE", "\x7f", "\b"):
                self.query = self.query[:-1]
            elif ch in ("KEY_UP", "KEY_DOWN"):
                self.move(-1 if ch == "KEY_UP" else 1)
                return None
            elif len(ch) == 1 and ch.isprintable():
                self.query += ch
            else:
                return None
            self.cursor = 0
            self.refilter()
            return None
        if ch in ("q", "\x1b"):
            if ch == "\x1b" and self.query:
                self.query = ""
                self.refilter()
                return None
            return "quit"
        if ch in ("\n", "\r", "KEY_ENTER"):
            return "resume" if self.shown else None
        if ch in ACTION_KEYS:
            return ACTION_KEYS[ch] if self.shown else None
        if ch == "h" and self.shown:
            source = self.selected()[0][0]
            self.handoff_to = [t for t in HANDOFF_TARGETS if t != source]
            return None
        if ch == "/":
            self.typing = True
        elif ch in ("KEY_RIGHT", " "):
            self.toggle(None if ch == " " else True)
        elif ch == "KEY_LEFT":
            self.toggle(False)
        elif ch in ("KEY_UP", "k"):
            self.move(-1)
        elif ch in ("KEY_DOWN", "j"):
            self.move(1)
        elif ch == "KEY_PPAGE":
            self.move(-page)
        elif ch == "KEY_NPAGE":
            self.move(page)
        elif ch in ("KEY_HOME", "g"):
            self.cursor = 0
        elif ch in ("KEY_END", "G"):
            self.cursor = max(0, len(self.shown) - 1)
        elif ch == "\t":
            cycle = [None, *self.tools]
            self.tool = cycle[(cycle.index(self.tool) + 1) % len(cycle)]
            self.cursor = 0
            self.refilter()
        elif ch == ".":
            self.here_only = not self.here_only
            self.cursor = 0
            self.refilter()
        return None

    def move(self, delta):
        if self.shown:
            self.cursor = max(0, min(len(self.shown) - 1, self.cursor + delta))

    # -- drawing --------------------------------------------------------------

    def draw(self, scr, curses):
        scr.erase()
        h, w = scr.getmaxyx()
        preview_h = 6 if h >= 16 else 0
        list_h = max(1, h - 2 - preview_h)

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + list_h:
            self.top = self.cursor - list_h + 1

        # header: what is being shown, and how it is filtered
        scope = self.tool or "all tools"
        if self.here_only:
            scope += ", this dir"
        head = f" cw ui  {len(self.shown)}/{len(self.items)}  [{scope}]"
        if self.typing or self.query:
            head += f"  /{self.query}" + ("_" if self.typing else "")
        put(scr, 0, 0, _clip(head, w - 1), curses.A_BOLD)

        w_tool = max((_display_width(t) for t in self.tools), default=6)
        w_when = 9
        w_cwd = min(28, max(8, w // 4))
        for i in range(list_h):
            n = self.top + i
            if n >= len(self.shown):
                break
            (tool, sid, when, _short, cwd, title), rec = self.shown[n]
            kind = self.kind[n]
            if kind == "head":
                # The topic, not how its newest session happened to start.
                title = sessions._without_inherited_mark(title or "")
            elif kind == "member":
                last_member = n + 1 >= len(self.shown) or self.kind[n + 1] != "member"
                title = ("└ " if last_member else "├ ") + (title or "")
            turns = self.turns_of(rec)
            shown = "?" if turns is None else str(turns)
            mark = "▸" if common._same_path(cwd, self.cwd) else " "
            line_attr = curses.A_REVERSE if n == self.cursor else 0
            y = 1 + i
            x = 0
            x = put(scr, y, x, f"{mark}", line_attr)
            x = put(scr, y, x, f"{tool:<{w_tool}} ", line_attr | tool_attr(curses, tool))
            x = put(scr, y, x, f"{pad(when, w_when)} ", line_attr | curses.A_DIM)
            x = put(scr, y, x, f"{pad(clip_tail(cwd or '', w_cwd), w_cwd)} ", line_attr)
            # The TURNS column sits just before the title (as in the plain
            # listing): CWD, then TURNS, then the title -- not between WHEN
            # and CWD. The title is clipped to what the turns column leaves.
            rest = w - 1 - x
            turns_w = TURNS_WIDTH + 1  # the column plus its trailing space
            if rest > turns_w:
                x = put(scr, y, x, f"{shown:>{TURNS_WIDTH}} ", line_attr | curses.A_DIM)
                rest = w - 1 - x
            if rest > 2:
                # The ✎ follows the title (as in the plain listing), not
                # before it -- it marks the row, it is not part of the title.
                # The ⌗ rides after it: a wiki page was built from this
                # session. The ⧉N rides after both: N rows on screen share
                # this session's topic.
                group = [r[1] for r, _ in self.members.get(sid, ())] or [sid]
                fold = ""
                if sid in self.topic_count:
                    # ▾ only when unfolded: ▸ already marks "this directory"
                    # in the first column.
                    # A space after ⧉: fonts that draw it two cells wide
                    # (seen live) covered the digit that followed it.
                    fold = f"⧉ {self.topic_count[sid]}" + \
                        ("▾" if self.key_of[sid] in self.expanded else "")
                pen = self.topic_note(sid) or ("✎" if any(g in self.noted for g in group) else "")
                marks = pen + \
                        ("⌗" if any(g in self.wiked for g in group) else "") + fold
                if marks:
                    x = put(scr, y, x, _clip(title or "", rest - 1 - len(marks)), line_attr)
                    x = put(scr, y, x, " " + marks, line_attr)
                else:
                    x = put(scr, y, x, _clip(title or "", rest), line_attr)
            if line_attr and x < w - 1:
                put(scr, y, x, " " * (w - 1 - x), line_attr)

        if preview_h:
            y0 = h - 1 - preview_h
            put(scr, y0, 0, "─" * (w - 1), curses.A_DIM)
            sel = self.selected()
            if sel:
                (tool, sid, when, _short, cwd, title), rec = sel
                turns, last = self.detail(rec)
                facts = f" {tool}  {sid}  {when}"
                if turns:
                    facts += f"  {turns} turns"
                topic_pen = self.topic_note(sid)
                if topic_pen:
                    facts += "  ✎ topic noted" + (" (behind its sessions)" if topic_pen == "✎+" else "")
                elif sid in self.noted:
                    facts += "  ✎ noted"
                if sid in self.wiked:
                    facts += "  ⌗ wiki: " + ", ".join(self.wiked[sid])
                if sid in self.topic_count:
                    group = self.members[sid]
                    total = self.topic_turns(sid)
                    facts += f"  ⧉ {len(group)} sessions on this topic"
                    if total:
                        facts += f", {total} turns"
                    facts += f" since {group[-1][0][2]}"
                    facts += "  (← fold)" if self.key_of[sid] in self.expanded else "  (→ unfold)"
                put(scr, y0 + 1, 0, _clip(facts, w - 1), curses.A_BOLD)
                put(scr, y0 + 2, 0, _clip(f" {cwd}", w - 1), curses.A_DIM)
                lines = wrap(f"↳ {last}" if last else "↳ (no message text)", w - 2, preview_h - 3)
                for k, text in enumerate(lines):
                    put(scr, y0 + 3 + k, 1, text)

        if self.handoff_to is not None:
            ask = " hand off to:  " + "  ".join(
                f"{n} {t}" for n, t in enumerate(self.handoff_to, start=1)) + "   (other key: cancel)"
            put(scr, h - 1, 0, _clip(ask, w - 1), curses.A_BOLD)
        else:
            put(scr, h - 1, 0, _clip(" " + KEYS_HELP, w - 1), curses.A_DIM)
        scr.refresh()


def put(scr, y, x, text, attr=0):
    """addstr that never raises at the screen's edge; returns the next x."""
    try:
        scr.addstr(y, x, text, attr)
    except Exception:
        pass
    return x + _display_width(text)


def pad(text, width):
    return text + " " * max(0, width - _display_width(text))


def clip_tail(text, width):
    """Keep the end of a path: its last folder names the project."""
    if _display_width(text) <= width:
        return text
    out = ""
    for ch in reversed(text):
        if _display_width(out) + _display_width(ch) > width - 1:
            break
        out = ch + out
    return "…" + out


def wrap(text, width, max_lines):
    """Split text into display-width lines, at most max_lines of them."""
    if max_lines < 1 or width < 2:
        return []
    text = " ".join(text.split())
    lines, cur, cut = [], "", False
    for ch in text:
        if _display_width(cur) + _display_width(ch) > width:
            lines.append(cur)
            cur = ""
            if len(lines) == max_lines:
                cut = True
                break
        cur += ch
    if not cut and cur:
        lines.append(cur)
    if cut:
        last = lines[-1]
        while last and _display_width(last) + 1 > width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


_PAIRS = {}


def tool_attr(curses, tool):
    return curses.color_pair(_PAIRS.get(tool, 0))


def init_colors(curses):
    try:
        curses.start_color()
        curses.use_default_colors()
    except Exception:
        return
    for n, (tool, name) in enumerate(TOOL_COLORS.items(), start=1):
        try:
            curses.init_pair(n, getattr(curses, f"COLOR_{name}"), -1)
            _PAIRS[tool] = n
        except Exception:
            pass


def run(scr, curses, picker):
    curses.curs_set(0)
    init_colors(curses)
    while True:
        picker.draw(scr, curses)
        try:
            ch = scr.get_wch()
        except KeyboardInterrupt:
            return None
        if isinstance(ch, int):
            ch = curses.keyname(ch).decode("ascii", "replace")
        page = max(1, scr.getmaxyx()[0] - 9)
        action = picker.key(ch, page)
        if action == "quit":
            return None
        if action:
            return action


def run_cw_command(args):
    """Run one of cw's own exec-ending commands (resume, handoff) in a child.

    Those commands end in exec_or_die: the process *becomes* the tool, by
    design -- a plain `cw resume 3` is expected to hand the terminal over
    for good. cw ui wants the terminal back when the tool quits, so the
    command runs as a child: the child execs into the tool, this process
    stays alive to take the list back. PYTHONPATH keeps the child on this
    checkout's sources, the same path the cw shim inserts."""
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    seen = os.environ.get("PYTHONPATH", "")
    env = dict(os.environ,
               PYTHONPATH=f"{src}{os.pathsep}{seen}" if seen else src)
    try:
        return subprocess.call(args, env=env)
    except KeyboardInterrupt:
        # Ctrl+C reaches both the tool and this waiter; the tool handles
        # its own exit, and a traceback here would only scroll past its UI.
        return 130


def cmd_ui(args):
    if args and args[0] in ("-h", "--help"):
        print("Usage: cw ui\n\nPick a session from every tool's store and resume it.\n"
              + KEYS_HELP)
        return 0
    if args:
        print(f"cw ui: unexpected argument '{args[0]}'", file=sys.stderr)
        return 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("cw ui: needs an interactive terminal -- `cw sessions` is the plain listing",
              file=sys.stderr)
        return 1
    try:
        import curses
    except ImportError:
        print("cw ui: this Python has no curses module (stock Windows Python ships none) -- "
              "`pip install windows-curses` adds it, or use `cw sessions`", file=sys.stderr)
        return 1
    import locale
    locale.setlocale(locale.LC_ALL, "")
    # ESC is a key here (leave the filter); the default 1s wait for an
    # escape sequence makes it feel broken.
    os.environ.setdefault("ESCDELAY", "25")

    print("cw ui: reading session stores…", file=sys.stderr, end="", flush=True)
    items = gather()
    print("\r\033[K", file=sys.stderr, end="", flush=True)
    if not items:
        print("No sessions found.")
        return 0

    picker = Picker(items, os.getcwd())
    while True:
        action = curses.wrapper(run, curses, picker)
        if not action:
            return 0
        (tool, sid, *_), _rec = picker.selected()
        # Every action leaves curses first (run() has already returned), so
        # the tool or command gets a normal terminal -- and the list takes
        # the terminal back when it is done. The picker is the home screen:
        # quitting a tool comes back here, it does not end cw.
        if action == "resume":
            run_cw_command([sys.executable, "-m", "crossweave.cli",
                            "resume", tool, sid])
        elif isinstance(action, tuple):  # ("handoff", target)
            # The commands below take a row of the last listing: make the
            # picker's view that listing, and the highlighted row its number.
            picker.write_cache()
            row = str(picker.cursor + 1)
            run_cw_command([sys.executable, "-m", "crossweave.cli",
                            row, action[1]])
        else:
            picker.write_cache()
            row = str(picker.cursor + 1)
            run_action(action, row, tool, sid)
        if not pause():
            return 0
        if action == "note":
            picker.noted = note.noted_index()
            picker.topic_noted = note.topic_noted_index()
        else:
            print("cw ui: reading session stores…", file=sys.stderr, end="", flush=True)
            picker.reload(gather())


def run_action(action, row, tool, sid):
    """Run note/delete/rename on one row, in the plain terminal."""
    print(f"\n[{tool}] {sid}")
    if action == "note":
        note.cmd_note([row])
    elif action == "delete":
        delete.cmd_delete([row])
    elif action == "rename":
        try:
            title = input("New title (empty: let the judge propose one): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNot renamed.")
            return
        rename.cmd_rename([row, title] if title else [row])


def pause():
    """Wait for Enter before going back to the list; False on q or Ctrl-C."""
    try:
        answer = input("\nEnter: back to the list   q: quit ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() != "q"
