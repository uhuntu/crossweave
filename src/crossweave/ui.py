"""cw ui - an interactive session picker in the terminal.

The same sessions `cw sessions` lists, in one scrollable list: type to
filter, see where a session left off, press Enter to resume it. Built on the
standard library's curses, so it adds no dependency; where curses is missing
(stock Python on Windows) it says so and points at the plain listing.

The picker only chooses. Everything it does after a choice goes through the
commands that already exist -- resume is cmd_resume -- and it runs after
curses has handed the terminal back, so the tool that starts gets a normal
terminal rather than one curses is still holding.
"""
import os
import sys

from . import common, note, sessions
from .render import _clip, _display_width

KEYS_HELP = ("↑↓ move  / filter  Tab tool  . this dir  Enter resume  q quit")

# Colors per tool, as curses pair numbers; the ANSI palette in color.py is
# escape codes, which curses cannot take.
TOOL_COLORS = {
    "claude": "YELLOW", "codex": "GREEN", "kimi": "CYAN", "step": "MAGENTA",
    "zcode": "BLUE", "codebuddy": "RED", "workbuddy": "WHITE",
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
        self.tool = None
        self.here_only = False
        self.cursor = 0
        self.top = 0
        self.details = {}  # full id -> (turns, last message), filled on view
        self.noted = note.noted_index()
        self.tools = [t for t in sessions.TOOLS if any(r[0] == t for r, _ in items)]
        self.refilter()

    def refilter(self):
        words = self.query.casefold().split()
        here = self.cwd if self.here_only else None
        self.shown = [it for it in self.items if matches(it[0], words, self.tool, here)]
        self.cursor = min(self.cursor, max(0, len(self.shown) - 1))

    def selected(self):
        return self.shown[self.cursor] if self.shown else None

    def detail(self, record):
        sid = record["id"]
        if sid not in self.details:
            try:
                turns = sessions.session_turns(record)
            except Exception:
                turns = None
            try:
                last = sessions.session_last_message(record)
            except Exception:
                last = None
            self.details[sid] = (turns, last)
        return self.details[sid]

    # -- keys ---------------------------------------------------------------

    def key(self, ch, page):
        """Apply one key. Returns "resume", "quit" or None."""
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
        if ch == "/":
            self.typing = True
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
            (tool, sid, when, _short, cwd, title), _rec = self.shown[n]
            mark = "▸" if common._same_path(cwd, self.cwd) else " "
            noted = "✎" if sid in self.noted else " "
            line_attr = curses.A_REVERSE if n == self.cursor else 0
            y = 1 + i
            x = 0
            x = put(scr, y, x, f"{mark}", line_attr)
            x = put(scr, y, x, f"{tool:<{w_tool}} ", line_attr | tool_attr(curses, tool))
            x = put(scr, y, x, f"{pad(when, w_when)} ", line_attr | curses.A_DIM)
            x = put(scr, y, x, f"{pad(clip_tail(cwd or '', w_cwd), w_cwd)} ", line_attr)
            rest = w - 1 - x
            if rest > 2:
                x = put(scr, y, x, _clip(f"{noted}{title or ''}", rest), line_attr)
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
                if sid in self.noted:
                    facts += "  ✎ noted"
                put(scr, y0 + 1, 0, _clip(facts, w - 1), curses.A_BOLD)
                put(scr, y0 + 2, 0, _clip(f" {cwd}", w - 1), curses.A_DIM)
                lines = wrap(f"↳ {last}" if last else "↳ (no message text)", w - 2, preview_h - 3)
                for k, text in enumerate(lines):
                    put(scr, y0 + 3 + k, 1, text)

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
        if action == "resume":
            return picker.selected()


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
    chosen = curses.wrapper(run, curses, picker)
    if not chosen:
        return 0
    (tool, sid, *_), _rec = chosen
    sessions.cmd_resume([tool, sid])
    return 0
