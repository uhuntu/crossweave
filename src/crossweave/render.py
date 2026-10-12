"""The listing table: column arithmetic, clipping and the row renderer.

Everything here is presentation. The numbers behind every column are
display-width arithmetic, not len(): a CJK character occupies two terminal
columns and a combining mark zero, so a title measured in code points runs
past its column and pushes every field after it out of line. The table's
furniture (the current-directory marker, the ellipsis, the "where it left
off" connector) is chosen as the first glyph the output encoding can
actually represent, because a legacy code page has no "▸" and a printed
"?" there reads as a bug rather than a marker.

render_rows is reached as sessions.render_rows -- the sessions hub
re-exports it, and its namespace is where cli.py, search.py and the tests
aim their patches. For the same reason the per-row facts (turn counts, the
"where it left off" line) and the listing cache are read from the hub at
call time: sessions owns the store readers and the cache, and a top-level
import here would be circular, since sessions re-exports this module.
"""
import os
import shutil
import sys
import unicodedata

from . import color, common


# A reason is prose, and the table is already wide, so it gets the space
# that's left rather than a share of its own.
WHY_MAX_WIDTH = 50
WHY_MIN_WIDTH = 12
TITLE_MIN_WIDTH = 12
TITLE_WITH_WHY_MAX_WIDTH = 44
# The turns column is fixed-width: its values are read while the rows print
# (see render_rows' sources), so the header cannot wait to measure them.
TURNS_WIDTH = 5
CWD_MIN_WIDTH = 8
# Room for the "▸ " that marks a row belonging to the current directory.
MARK_WIDTH = 2
# ...but a legacy code page (GBK, Shift JIS) has no such glyph -- neither
# does it have "»" -- and printing one there substitutes "?" and looks like
# a bug. The first marker the output encoding can actually encode wins.
MARKER_CHOICES = ("▸", "»", ">")
# What a clipped cell ends (or starts) with, in order of preference.
ELLIPSIS_CHOICES = ("…", "..")
# What the "where it left off" line hangs from its row with.
LAST_PREFIX_CHOICES = ("└", "`")
# A last message is a preview, not a transcript: one line, same budget as a
# title.
LAST_MESSAGE_MAX = 70


def _encodable(choices):
    """The first of `choices` the output stream's encoding can represent.

    The marker, the clip glyph and the "where it left off" connector are
    what the table draws with, and a legacy code page has no "▸" (nor "»")
    -- printing one there substitutes "?" and reads as a bug rather than a
    marker. The ASCII stand-ins say the same thing, and the width math
    measures whichever one is chosen."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    for glyph in choices:
        try:
            glyph.encode(encoding)
            return glyph
        except (UnicodeEncodeError, LookupError):
            continue
    return choices[-1]


def _row_marker():
    return _encodable(MARKER_CHOICES)


def _char_width(ch):
    """Columns a character occupies: wide (CJK) is two, combining is zero."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text):
    return sum(_char_width(c) for c in text)


def _pad(text, width):
    return text + " " * max(0, width - _display_width(text))


def _clip(text, width):
    """Clip to `width` columns, ending in an ellipsis.

    len() counts code points, so a title in Chinese -- or with an emoji --
    measured narrower than it printed: the row ran past its column and
    pushed every field after it out of line, and the ellipsis landed on the
    second half of a wide character. Measuring columns fixes both."""
    if _display_width(text) <= width:
        return text
    ellipsis = _encodable(ELLIPSIS_CHOICES)
    budget = width - _display_width(ellipsis)
    out = []
    used = 0
    for ch in text:
        w = _char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def _clip_tail(text, width):
    """Clip a path from the left instead: the tail names the project, while
    the head is the home directory you are already standing in."""
    if _display_width(text) <= width:
        return text
    ellipsis = _encodable(ELLIPSIS_CHOICES)
    budget = width - _display_width(ellipsis)
    keep = []
    used = 0
    for ch in reversed(text):
        w = _char_width(ch)
        if used + w > budget:
            break
        keep.append(ch)
        used += w
    return ellipsis + "".join(reversed(keep))


def _same_saying(a, b):
    """True when two texts are the same saying, one of them clipped.

    A one-turn session's last message *is* its title, and printing both
    says everything twice. The title is cut at 70 characters, so the last
    message can be the longer of the two."""
    return a == b or b.startswith(a) or a.startswith(b)


def render_rows(rows, write_cache=True, start=1, notes=None, full_notes=False,
                sources=None, notes_label="WHY"):
    """rows: list of resolve_row()-shaped tuples, already in display order.
    Prints the numbered table and, unless write_cache=False, writes the
    resume cache (cmd_search renders two sections with continuing numbers
    and writes the cache once for their union).

    notes: optional {(tool, full_id): one-line why} -- `cw search` passes the
    judge's justification there. Shown as a column (labeled notes_label --
    `cw sessions --machines` reuses the mechanism for the machine a row
    lives on), clipped to whatever width the terminal has left after the
    other fields: one line per hit, so a 10-hit search stays 10 lines.
    `full_notes` prints the unclipped reason on its own line instead, which
    is what `--why` is for.

    sources: optional {full_id: light record}, for the listing only -- search
    rows have none. It buys the TURNS column, counted per row as that row
    prints, so the cost stays proportional to the rows actually shown rather
    than to the size of the store.

    The per-row facts and the cache write go through the sessions hub at
    call time: its namespace is the patch surface the tests aim at, and a
    top-level import would be circular (sessions re-exports this module)."""
    from . import sessions

    if not rows:
        print("No sessions found.")
        return

    # resolve_row already coerces a cwd that isn't a string; the formatter is
    # the boundary that would crash on one, so it does not rely on every
    # caller having done so. It has to happen before the width math below,
    # which measures every row's cwd: a dict there reached _display_width as
    # an iterable of its keys.
    rows = [(*r[:4], r[4] if isinstance(r[4], str) else "?", *r[5:]) for r in rows]

    if write_cache:
        sessions.write_list_cache([{"tool": tool, "id": full_id} for tool, full_id, *_ in rows])

    notes = notes or {}
    inline_why = bool(notes) and not full_notes
    here = os.getcwd()
    # "▸" on the rows that belong to the directory you are standing in: the
    # question you actually have when you type `cw` in a project.
    marks = [common._same_path(r[4], here) if isinstance(r[4], str) else False for r in rows]
    marker = _row_marker()

    w_num = len(str(start + len(rows) - 1))
    w_tool = max(4, max(_display_width(r[0]) for r in rows))
    w_when = max(4, max(_display_width(r[2]) for r in rows))
    w_id = max(2, max(_display_width(r[3]) for r in rows))
    # The reason needs room, so the working directory gives some up.
    w_cwd = min(24 if inline_why else 40, max(3, max(_display_width(r[4]) for r in rows)))

    show_turns = bool(sources) and not inline_why
    show_last = show_turns
    if show_turns:
        # Fit the plumbing before the title: the cwd is the most compressible
        # (a path's tail still names the project), and if even a floor-width
        # title does not fit, the turns column goes entirely -- the title is
        # the one column that has to stay readable. The title itself is never
        # clipped here; a long one wraps, as it always has.
        overhead = MARK_WIDTH + w_num + w_tool + w_when + w_id + 10  # 5 gaps
        room = shutil.get_terminal_size((100, 24)).columns - overhead - (TURNS_WIDTH + 2)
        w_cwd = min(w_cwd, max(CWD_MIN_WIDTH, room - TITLE_MIN_WIDTH))
        if shutil.get_terminal_size((100, 24)).columns - overhead - (TURNS_WIDTH + 2) - w_cwd < TITLE_MIN_WIDTH:
            show_turns = False
            w_cwd = min(w_cwd, max(CWD_MIN_WIDTH,
                                   shutil.get_terminal_size((100, 24)).columns - overhead - TITLE_MIN_WIDTH))

    w_title = 0
    w_why = 0
    if inline_why:
        fixed = MARK_WIDTH + w_num + w_tool + w_when + w_id + w_cwd + 10  # 2 spaces between fields
        spare = shutil.get_terminal_size((100, 24)).columns - fixed
        if spare >= TITLE_MIN_WIDTH + WHY_MIN_WIDTH + 2:
            # Roughly even, leaning to the reason: a title that's clipped to a
            # few characters is still recognizable from its first words, while
            # a reason clipped that short says nothing.
            w_title = max(TITLE_MIN_WIDTH, min(TITLE_WITH_WHY_MAX_WIDTH, int(spare * 0.45)))
            w_why = max(WHY_MIN_WIDTH, min(WHY_MAX_WIDTH, spare - w_title - 2))
        else:
            # Too narrow for both floors: split what's left rather than push
            # the row past the terminal edge and wrap it.
            w_why = max(1, spare // 2)
            w_title = max(1, spare - w_why - 2)

    header = (f"{'':>{MARK_WIDTH}}{'#':>{w_num}}  {'TOOL':<{w_tool}}  {'WHEN':<{w_when}}  "
              f"{'ID':<{w_id}}  {'CWD':<{w_cwd}}  ")
    if show_turns:
        header += f"{'TURNS':>{TURNS_WIDTH}}  "
    header += "TITLE"
    if inline_why:
        # Left-align the label where the title column ends -- the same
        # w spaces `{'':<{w}}` would produce, without a nested field.
        label_pad = " " * max(0, w_title - 2 - len(notes_label))
        header += f"{label_pad}  {notes_label}"
    print(color.paint(header, color.BOLD))
    indent = " " * (w_num + 2)
    # The "where it left off" line hangs off the row number rather than under
    # the title: the title column starts most of the way across the table, and
    # a preview with 27 characters to live in is not a preview. This is where
    # `--why` puts its own continuation line, for the same reason.
    last_prefix = _encodable(LAST_PREFIX_CHOICES)
    last_room = max(10, shutil.get_terminal_size((100, 24)).columns
                    - len(indent) - _display_width(last_prefix) - 1)
    for n, (tool, full_id, when, sid, cwd_show, title) in enumerate(rows, start=start):
        mine = marks[n - start]
        cwd_disp = _clip_tail(cwd_show, w_cwd)
        line = (color.paint(marker, color.BOLD) + " " if mine else " " * MARK_WIDTH)
        line += f"{n:>{w_num}}  "
        line += color.paint(_pad(tool, w_tool), color.tool_color(tool)) + "  "
        line += color.paint(_pad(when, w_when), color.DIM) + "  "
        line += color.paint(_pad(sid, w_id), color.DIM) + "  "
        line += (color.paint(_pad(cwd_disp, w_cwd), color.BOLD) if mine
                 else _pad(cwd_disp, w_cwd)) + "  "
        if show_turns:
            turns = sessions.session_turns(sources[full_id]) if full_id in sources else None
            shown = "?" if turns is None else str(turns)
            line += color.paint(f"{shown:>{TURNS_WIDTH}}", color.DIM) + "  "
        if inline_why:
            line += f"{_clip(title, w_title):<{w_title}}  {_clip(notes.get((tool, full_id), ''), w_why)}"
        else:
            line += title
        print(line.rstrip())
        if show_last:
            last = sessions.session_last_message(sources[full_id]) if full_id in sources else None
            if last and not _same_saying(last, title):
                print(color.paint(f"{indent}{last_prefix} {_clip(last, last_room)}",
                                  color.DIM))
        why = notes.get((tool, full_id))
        if full_notes and why:
            print(f"{indent}why: {why}")
