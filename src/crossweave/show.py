"""Read saved conversations without starting an agent."""
import pydoc
import sys

from . import sessions


def cmd_show(args):
    """Display one local session, paging only when stdout is a terminal."""
    if len(args) == 1 and sessions.ROW_NUMBER_RE.fullmatch(args[0]):
        cache = sessions.read_list_cache()
        n = int(args[0])
        if not cache or not 1 <= n <= len(cache):
            print("cw show: row unavailable -- run `cw sessions` first", file=sys.stderr)
            return 1
        sessions._listing_age_warning()
        record = cache[n - 1]
        if record.get("machine"):
            print(f"cw show: session lives on {record['machine']} -- view it there",
                  file=sys.stderr)
            return 1
        tool, sid = record["tool"], record["id"]
    elif len(args) == 2 and args[0] in sessions.ID_RESOLVERS:
        tool, prefix = args
        hits = sessions.ID_RESOLVERS[tool](prefix)
        if len(hits) != 1:
            print(f"cw show: {len(hits)} sessions match {prefix!r}; use a unique session ID",
                  file=sys.stderr)
            return 1
        sid = hits[0]
    else:
        print("Usage: cw show <N> | cw show <tool> <session-id-or-prefix>", file=sys.stderr)
        return 1
    if tool not in sessions.TOOLS:
        print(f"cw show: {tool} is no longer supported -- refresh the session list", file=sys.stderr)
        return 1
    details = sessions.session_handoff_details(tool, sid)
    if details is None:
        print("cw show: session transcript unavailable", file=sys.stderr)
        return 1
    _, transcript = details
    transcript = transcript.replace(f"# Crossweave handoff from {tool}",
                                    f"# {tool} conversation", 1)
    if sys.stdout.isatty():
        pydoc.pager(transcript)
    else:
        sys.stdout.write(transcript)
    return 0
