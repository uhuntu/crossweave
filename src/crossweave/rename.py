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
  kimi    rewrite state.json's title with titleKind 'custom' (every
          titler path returns early on 'custom', so the name is
          permanent)

codebuddy has no rename surface cw knows, and is refused rather than
guessed at.

The row number may lead or follow the verb: `cw rename 12 "..."` and
`cw 12 rename "..."` are the same command, the latter reading the way
`cw 12 codex` already does. `--auto` has no one row to put first, so
the sweep keeps the verb-first spelling only -- and a `rename` sitting
in that slot is not handed to the tool as a continuation prompt the
way any other word after a row number would be, which is the one
reading this costs; `cw resume 3 rename` spells that out for whoever
meant it.

--auto proposes titles with the same judge `cw search` uses, one
batched call per twenty sessions, for sessions whose titles say
nothing -- never for good ones. "Says nothing" is broader than short:
a row titled with a pasted shell transcript, an `ls -l` listing or a
bare URL is worse than one titled "hi" and passes every length test,
so the shape test in common.title_says_nothing decides it instead
(see _is_trivial). Everything lands in a before/after table that waits
for a yes unless -y, and sessions updated in the last five minutes are
skipped: they are live conversations, mid-flight with their own
titler.

--auto and --trivial answer different questions -- who writes the
title, and which sessions -- so `--auto 3 7` names the rows and lets
the judge title them, while `--auto --trivial` finds the rows too.
--trivial alone means the latter: it picks neither a title nor a row,
so there is nothing else it could be asking for.
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


def _rename_kimi(sdir, title):
    """Rewrite state.json's title with titleKind 'custom' -- kimi's own
    rename surface.

    setTitle(title) writes {title, titleKind: "custom"} into the session's
    state.json (renameSession guards against an empty title), and every
    titler path -- setGeneratedTitleIfUncustomized, generateTitleOnce,
    generateAndApply -- returns early when titleKind is "custom", so the
    name is permanent. The kind is kimi's own vocabulary: `replaceable` is
    the first prompt echoed back and still the titler's to overwrite,
    `generated` the titler's, `custom` a user's. `isCustomTitle` is the v1
    spelling of the same fact and is recomputed the way encodeSessionMeta
    recomputes it, so a reader on either schema sees the rename.

    updatedAt is deliberately left alone: setTitle passes
    touchUpdatedAt: false, and cw reads that field for a session's recency
    -- bumping it would make a renamed old session look live and skip
    itself next time.

    The whole document is rewritten through a temp file and os.replace,
    the way kimi's atomic document store writes it, so a reader sees either
    the old state or the new one and never a half-written file. What that
    cannot do is merge: kimi holds the document in memory and writes all of
    it back on its next update, so renaming a session kimi currently has
    open is last-writer-wins -- kimi's next turn can put its own title back,
    and our write can drop a lastPrompt it had already recorded. --auto
    skips those (a live session's state.json is fresh, and _is_live reads
    it); a manual rename is deliberate and takes the risk."""
    if not sdir or not os.path.isdir(sdir):
        return False
    from . import common

    path = os.path.join(sdir, "state.json")
    state = common.read_json(path)
    if not isinstance(state, dict):
        return False
    state["title"] = title
    state["titleKind"] = "custom"
    state["isCustomTitle"] = True
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
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
    if tool == "kimi":
        return _rename_kimi(r.get("dir"), title)
    return False


def _is_trivial(title):
    """A title the sweep should propose for: one that says nothing, whether
    because it is a bare acknowledgement, two characters long, or a paste the
    scan fell back on. See common.title_says_nothing for the shapes -- the
    long ones matter most, since a pasted shell transcript or an `ls -l`
    listing is a worse row than "hi" and used to pass every check."""
    from . import sessions

    t = (title or "").strip()
    if len(t) <= 2:  # "中文", "测试" -- two characters say nothing
        return True
    return sessions.title_says_nothing(t)


# zcode, codex, claude, step and kimi each have a rename surface cw can
# write (see the module docstring); codebuddy does not, and is refused
# before anything else happens to its rows.
_RENAMABLE = ("zcode", "codex", "claude", "step", "kimi")


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
    print("Usage: cw rename <N> <title>   (or: cw <N> rename <title>)\n"
          "       cw rename --auto [<N>... | --trivial] [-y] [--judge T]\n"
          "  <N> is a row from the last `cw`/`cw sessions` listing. Renames\n"
          "  write through to the store itself (zcode, codex, claude, step,\n"
          "  kimi).\n"
          "  --auto proposes titles with the judge; --trivial sweeps every\n"
          "  session whose title says nothing (and implies --auto).",
          file=sys.stderr)
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

    if trivial and not auto:
        # --trivial is a selector inside --auto, not a mode of its own -- but
        # it names neither a title nor a row, so there is nothing else it could
        # be asking for. Saying it is saying both, and the sweep is the command
        # people actually reach for.
        auto = True

    if not auto and not positional:
        return _usage()

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

    if not positional or not sessions.ROW_NUMBER_RE.match(positional[0]):
        return _usage()
    n = int(positional[0])
    cache = sessions.read_list_cache()
    if not cache:
        print("cw rename: no session list cached yet -- run `cw sessions` first",
              file=sys.stderr)
        return 1
    r, why = _resolve_row(n, cache)
    if r is None:
        print(f"cw rename: {why}", file=sys.stderr)
        return 1
    if len(positional) < 2:
        # The row was named, so name it back. Resolving it first is what makes
        # that possible -- the old ordering counted the arguments before it
        # looked at the listing, so `cw 4 rename` answered one missing word
        # with the whole usage text and a mistyped row number got the same
        # wall as a valid one. Nothing is written here either way.
        #
        # The title comes from _resolve_title_and_cwd, not off the light
        # record: several stores carry no title there and the listing resolves
        # it separately, so reading the field directly printed "(no title)"
        # for a row that plainly had one.
        old = sessions._resolve_title_and_cwd(r)[0]
        print(f"cw rename: row {n} ({r['tool']} {r['id'][:12]}, "
              f"\"{_short(old)}\") needs a title", file=sys.stderr)
        print("Usage: cw rename <N> <title>   (or: cw <N> rename <title>)",
              file=sys.stderr)
        return 2
    title = " ".join(positional[1:]).strip()
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


def _short(title, limit=48):
    """A title as it reads inside an error line -- long enough to recognize
    the row, short enough that the message stays two lines."""
    t = (title or "").strip() or "(no title)"
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


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
