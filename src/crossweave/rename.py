"""cw rename - session titles that survive their tools' own titlers.

A listing is scanned, not read: rows titled "hi", "中文" and "只回复
两个字：正常" are indistinguishable, and the tools themselves name
sessions well only when the first message cooperates. This module
renames sessions through each store's own rename surface -- the same
records the tools write when a user renames in their UI -- so the new
title is native to the tool, not a cw-side overlay:

  zcode   UPDATE session SET title=..., title_source='custom' (the
          column's CHECK names the vocabulary; 'custom' is the
          user-rename value and survives the auto-titler)
  codex   append a newer session_index.jsonl line (the index keeps the
          most recently updated name per id)
  claude  append a custom-title record to the transcript (the reader
          keeps the last one it finds)
  step    append a session_info record to the session file (both of
          step's readers keep the last one, and its selector shows the
          name in place of the first prompt)

kimi and codebuddy have no rename surface cw knows, and are refused
rather than guessed at.

--auto proposes titles with the same judge `cw search` uses, one
batched call per twenty sessions, for sessions whose titles are
trivial -- never for good ones. Everything lands in a before/after
table that waits for a yes unless -y, and sessions updated in the last
five minutes are skipped: they are live conversations, mid-flight with
their own titler.
"""
import datetime
import json
import os
import re
import sys
import time
import uuid

# Sessions this young are live conversations -- the user is typing in
# them right now, their own titler may be mid-flight, and a rename
# would race both. Auto mode skips them; manual renames are deliberate
# and proceed regardless.
LIVE_SECONDS = 5 * 60

# One judge call proposes titles for at most this many sessions.
BATCH = 20

# A run proposes for at most this many sessions overall -- a bound on
# judge calls, with the note saying to rerun for the rest.
AUTO_CAP = 60


def _rename_zcode(sid, title):
    import sqlite3
    import time

    from . import zcode

    if not os.path.isfile(zcode.ZCODE_DB):
        return False
    con = sqlite3.connect(zcode.ZCODE_DB, timeout=10)
    try:
        with con:
            cur = con.execute(
                "UPDATE session SET title=?, title_source='custom', "
                "time_title_updated=? WHERE id=?",
                (title, int(time.time() * 1000), sid))
            return cur.rowcount > 0
    finally:
        con.close()


def _rename_codex(sid, title):
    from . import common

    path = os.path.join(common.CODEX_HOME, "session_index.jsonl")
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    line = json.dumps({"id": sid, "thread_name": title, "updated_at": stamp})
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return True


def _rename_claude(sid, title, path):
    if not path or not os.path.isfile(path):
        return False
    record = {"type": "custom-title", "customTitle": title, "sessionId": sid}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return True


def _rename_step(title, path):
    """Append a session_info record -- step's own rename surface.

    `step --name`, /name and the selector's rename key all call
    appendSessionInfo(name), which appends
    {"type":"session_info","id","parentId","timestamp","name"} to the
    session file. Both readers keep the LAST such record, and the
    selector's row is `session.name ?? session.firstMessage` -- so this
    record is what a rename is, and the name takes the first prompt's
    place in step's own picker (see step._step_display_name).

    The id is an 8-hex one retried while it collides with an id already
    in the file (generateId's rule), and parentId is the last entry's id,
    which keeps the file's id/parentId chain intact for step's tree."""
    if not path or not os.path.isfile(path):
        return False
    from . import common

    ids, last_id = set(), None
    for d in common.read_jsonl(path):
        if not isinstance(d, dict):
            continue
        entry_id = d.get("id")
        if isinstance(entry_id, str):
            ids.add(entry_id)
            last_id = entry_id
    for _ in range(100):
        new_id = uuid.uuid4().hex[:8]
        if new_id not in ids:
            break
    else:  # 8 hex collided a hundred times: take a whole uuid instead
        new_id = uuid.uuid4().hex
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    record = {"type": "session_info", "id": new_id, "parentId": last_id,
              "timestamp": stamp, "name": _step_name(title)}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def _step_name(title):
    """step's own sanitization of a name: newlines flattened to spaces,
    then trimmed. appendSessionInfo does exactly this before writing, so
    a title carrying a stray newline is stored the way step would have
    stored it."""
    return re.sub(r"[\r\n]+", " ", title).strip()


def _apply(r, title):
    tool, sid = r["tool"], r["id"]
    if tool == "zcode":
        return _rename_zcode(sid, title)
    if tool == "codex":
        return _rename_codex(sid, title)
    if tool == "claude":
        return _rename_claude(sid, title, r.get("path"))
    if tool == "step":
        return _rename_step(title, r.get("path"))
    return False


def _is_trivial(title):
    from . import sessions

    t = (title or "").strip()
    if not t or t == "(no title)":
        return True
    if sessions.is_trivial_title(t):
        return True
    return len(t) <= 2  # "中文", "测试" -- two characters say nothing


# zcode, codex, claude and step each have a rename surface cw can write
# (see the module docstring); kimi and codebuddy do not, and are refused
# before anything else happens to their rows.
_RENAMABLE = ("zcode", "codex", "claude", "step")


def _is_live(r):
    """A session with activity inside LIVE_SECONDS. The listing's own ts
    when the store carries one (zcode's is its time_updated; ts=0 means
    the store does not know -- not live), else the transcript's mtime."""
    from . import sessions

    ts = r.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        return (time.time() - ts) < LIVE_SECONDS
    path = sessions._transcript_path(r)
    if not path or not os.path.isfile(path):
        return False
    try:
        return (time.time() - os.path.getmtime(path)) < LIVE_SECONDS
    except OSError:
        return False


def _not_renamable(r):
    print(f"cw rename: {r['tool']} sessions have no rename surface cw "
          "knows -- refusing to guess", file=sys.stderr)


def _resolve_row(n, cache):
    """Cache row number -> its light record, or (None, reason)."""
    from . import sessions

    if not (1 <= n <= len(cache)):
        return None, f"{n} is out of range (last listing had {len(cache)} rows)"
    entry = cache[n - 1]
    if entry.get("machine"):
        return None, (f"row {n} lives on {entry['machine']} -- rename it "
                      "there, or mirror that machine's session store")
    r = sessions._find_record(entry["tool"], entry["id"])
    if r is None:
        return None, (f"row {n} ({entry['tool']} {entry['id'][:12]}) is no "
                      "longer in the store -- run `cw sessions` to refresh")
    return r, None


def _propose_titles(rows, judge):
    """{position: title} from one batched judge call, or raise
    search.JudgeError."""
    from . import search, sessions

    lines = []
    for n, r in enumerate(rows, 1):
        title_now = sessions._resolve_title_and_cwd(r)[0]
        lines.append(f"{n}. [{r['tool']}] {r.get('cwd') or '?'} — "
                     f"{title_now} :: {search.snippet_for(r)}")
    prompt = (
        "Each line below describes a past AI coding-assistant session whose "
        "title is useless (a greeting, a fragment, a test phrase). Propose a "
        "replacement title for each.\n\n" + "\n".join(lines) +
        "\n\nRules: at most 30 characters; the language of the session's own "
        "content (Chinese sessions get Chinese titles); no quotes, no trailing "
        "period; a title names the actual topic -- the file, error, project or "
        "question -- never 'discussion' or 'help' or '问题排查'.\n"
        "Reply with exactly one line per session, `N. title`, nothing else."
    )
    proc = search._call_judge(judge, prompt)
    if proc is None or proc.returncode != 0:
        raise search.JudgeError(proc.returncode if proc else 127)
    return search.parse_numbered_reasons(proc.stdout, len(rows))


def _usage():
    print("Usage: cw rename <N> <title>\n"
          "       cw rename --auto [<N>... | --trivial] [-y] [--judge T]\n"
          "  <N> is a row from the last `cw`/`cw sessions` listing. Renames\n"
          "  write through to the store itself (zcode, codex, claude, step).\n"
          "  --auto proposes titles with the judge; --trivial sweeps every\n"
          "  session whose title says nothing.", file=sys.stderr)
    return 2


def cmd_rename(args):
    yes = auto = trivial = False
    judge = None
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-y", "--yes"):
            yes = True; i += 1
        elif a == "--auto":
            auto = True; i += 1
        elif a == "--trivial":
            trivial = True; i += 1
        elif a == "--judge":
            if i + 1 >= len(args):
                print("cw rename: --judge requires a tool", file=sys.stderr)
                return 2
            judge = args[i + 1]; i += 2
        elif a.startswith("-"):
            print(f"cw rename: unknown option '{a}'", file=sys.stderr)
            return 2
        else:
            positional.append(a); i += 1

    if not auto and not positional:
        return _usage()
    if not auto and trivial:
        return _usage()  # --trivial is an --auto selector, not a mode

    from . import search, sessions

    if judge is None:
        judge = search.DEFAULT_JUDGE
    elif judge not in search.JUDGE_CMD:
        print(f"cw rename: --judge must be one of {', '.join(search.JUDGE_CMD)}",
              file=sys.stderr)
        return 2

    if not auto:
        return _rename_manual(positional)

    return _rename_auto(positional, trivial, judge, yes)


def _rename_manual(positional):
    from . import sessions

    if len(positional) < 2 or not sessions.ROW_NUMBER_RE.match(positional[0]):
        return _usage()
    n = int(positional[0])
    title = " ".join(positional[1:]).strip()
    cache = sessions.read_list_cache()
    if not cache:
        print("cw rename: no session list cached yet -- run `cw sessions` first",
              file=sys.stderr)
        return 1
    r, why = _resolve_row(n, cache)
    if r is None:
        print(f"cw rename: {why}", file=sys.stderr)
        return 1
    if r["tool"] not in _RENAMABLE:
        _not_renamable(r)
        return 1
    old = sessions._resolve_title_and_cwd(r)[0]
    if not _apply(r, title):
        print(f"cw rename: {r['tool']} {r['id'][:12]}: the store did not "
              "take the rename", file=sys.stderr)
        return 1
    print(f"renamed: {old} -> {title}")
    return 0


def _rename_auto(positional, trivial, judge, yes):
    from . import search, sessions

    cache = sessions.read_list_cache()
    rows = []
    skipped = []
    if trivial:
        # The sweep resolves every session's title -- a full-store walk,
        # and the price of finding the unnamed. The judge batches keep it
        # to a handful of calls.
        for r in sessions._all_light_records():
            if r["tool"] not in _RENAMABLE:
                continue
            row = sessions.resolve_row(r)
            if row[5] in sessions.TOOL_STARTED_TITLES:
                continue
            if _is_live(r):
                skipped.append((r, "still live"))
                continue
            if _is_trivial(row[5]):
                rows.append(r)
                if len(rows) >= AUTO_CAP:
                    print(f"cw rename: capped at {AUTO_CAP} per run -- "
                          "rerun for the rest", file=sys.stderr)
                    break
    else:
        if not cache:
            print("cw rename: no session list cached yet -- run `cw sessions` first",
                  file=sys.stderr)
            return 1
        for raw in positional:
            if not sessions.ROW_NUMBER_RE.match(raw):
                print(f"cw rename: '{raw}' is not a row number "
                      "(--trivial is the sweep form)", file=sys.stderr)
                return 2
            r, why = _resolve_row(int(raw), cache)
            if r is None:
                print(f"cw rename: {why}", file=sys.stderr)
                return 1
            if r["tool"] not in _RENAMABLE:
                _not_renamable(r)
                return 1
            if _is_live(r):
                skipped.append((r, "still live"))
                continue
            rows.append(r)

    for r, why in skipped:
        print(f"cw rename: skipping {r['tool']} {r['id'][:12]}: {why}", file=sys.stderr)
    if not rows:
        if trivial:
            print("cw rename: nothing to propose -- every candidate already "
                  "has a real title", file=sys.stderr)
        else:
            print("cw rename: no eligible sessions", file=sys.stderr)
        return 0

    try:
        proposals = {}
        # the judge numbers from 1 (parse_numbered_reasons keeps its
        # numbering); keep that keying so chunk offsets line up
        for start in range(0, len(rows), BATCH):
            chunk = rows[start:start + BATCH]
            for pos, title in _propose_titles(chunk, judge).items():
                proposals[start + pos] = title
    except search.JudgeError as e:
        print(f"cw rename: judge ({judge}) unavailable -- nothing renamed "
              f"(exit {e})", file=sys.stderr)
        return 1

    # Candidates without a proposal keep their title, and stay in the
    # table marked as such: the judge answering for 19 of 20 is a partial
    # result the user should see, not a silent shrink of the batch.
    pending = [(r, proposals.get(k + 1, "").strip() or None)
               for k, r in enumerate(rows)]

    print("proposed renames:\n")
    for r, new in pending:
        old = sessions._resolve_title_and_cwd(r)[0]
        arrow = f"-> {new}" if new else "-> (no proposal -- keeping)"
        print(f"  [{r['tool']}] {r['id'][:12]}  {old}\n{'':16}{arrow}")
    answered = [(r, new) for r, new in pending if new]
    if not answered:
        print("cw rename: the judge proposed nothing usable -- nothing renamed",
              file=sys.stderr)
        return 1
    if not yes:
        if not sys.stdin.isatty():
            print("\ncw rename: not a terminal -- pass -y to apply",
                  file=sys.stderr)
            return 1
        answer = input("\napply these renames? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("nothing renamed.")
            return 0

    applied = 0
    for r, new in answered:
        old = sessions._resolve_title_and_cwd(r)[0]
        if _apply(r, new):
            applied += 1
            print(f"renamed: {old} -> {new}")
        else:
            print(f"cw rename: {r['tool']} {r['id'][:12]}: the store did not "
                  "take the rename", file=sys.stderr)
    print(f"{applied} of {len(pending)} renamed.")
    return 0 if applied else 1
