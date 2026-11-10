"""Reader for opencode's session store: one SQLite database,
~/.local/share/opencode/opencode.db ($XDG_DATA_HOME/opencode when set).

This is opencode's v2 store (opencode 2.x). A session is a `session_v2` row:
its cwd is `directory`, its title a stored one opencode's own titler
writes, `parent_id` marks a subagent run opencode started for itself, and
`time_archived` an archived one. The conversation is `session_message`, one
row per message in `seq` order, with the kind in `type` and the body as
JSON in `data`:

  user       {"text": ..., "metadata": {"displayText": ...}, ...}
  assistant  {"content": [{"type": "text" | "reasoning" | "tool", ...}], ...}
             a tool block carries `name`, `state.input`, and the result as
             `state.content` text blocks
  system / synthetic / idle / model-switched
             what opencode itself injected or recorded -- nobody said these

The older 1.x layout (`session` / `message` / `part` tables, the one ZCode
inherited) is not read here; doctor names it when that is what it finds.

The database belongs to opencode's background service, which can be running
while cw reads, so every connection is read-only (`mode=ro`, the zcode
contract), and cw never writes here: rename and delete go through opencode,
or not at all.
"""
import json
import os
import re
import sqlite3

from . import common


def _default_db():
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(common.HOME, ".local", "share")
    return os.path.join(data_home, "opencode", "opencode.db")


OPENCODE_DB = _default_db()

# The name opencode gives a session before its titler has run, and keeps
# when it never does: "New session - 2026-11-06T12:00:00.000Z" (a subagent's
# reads "Child session - ..."). Not a name -- the first prompt says more.
PLACEHOLDER_TITLE_RE = re.compile(r"^(new|child) session - \d{4}-\d{2}-\d{2}T", re.I)

TITLE_SCAN_MESSAGES = 50

# One cached read-only connection per process, revalidated by file identity
# on every take -- the reason and the shape are zcode's (see zcode._connect).
_connection = None
_connection_key = None


def _drop_connection():
    global _connection, _connection_key
    if _connection is not None:
        try:
            _connection.close()
        except sqlite3.Error:
            pass
    _connection = None
    _connection_key = None


def _connect():
    global _connection, _connection_key
    db = OPENCODE_DB
    try:
        st = os.stat(db)
    except OSError:
        _drop_connection()
        return None
    key = (db, st.st_dev, st.st_ino)
    if _connection is not None and _connection_key == key:
        return _connection
    _drop_connection()
    try:
        _connection = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        _connection_key = key
    except sqlite3.Error:
        _connection = None
        _connection_key = None
    return _connection


def _query(query, params=()):
    """All rows, or [] when the store is missing or not the schema this
    reader knows -- an unreadable store lists as an empty one."""
    con = _connect()
    if con is None:
        return []
    try:
        return con.execute(query, params).fetchall()
    except sqlite3.Error:
        return []


def _json(data):
    if not isinstance(data, str):
        return {}
    try:
        value = json.loads(data)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _epoch(ms):
    try:
        return int((ms or 0) / 1000)
    except (TypeError, ValueError):
        return 0


def _directory(value):
    """The recorded cwd, in this OS's spelling: opencode writes Windows
    paths with forward slashes (C:/Users/...), every other tool with
    backslashes, and one listing should not show both."""
    if not isinstance(value, str) or not value:
        return None
    return os.path.normpath(value) if os.name == "nt" else value


def _iter_messages(sid):
    """(type, data dict) per message of a session, in conversation order."""
    for kind, data in _query(
            "SELECT type, data FROM session_message WHERE session_id = ? ORDER BY seq", (sid,)):
        yield kind, _json(data)


def _user_text(data):
    text = data.get("text")
    return text if isinstance(text, str) else ""


def _blocks(data):
    return [b for b in common.list_field(data, "content") if isinstance(b, dict)]


def _assistant_text(data):
    """The assistant's own words: its text blocks, joined."""
    return "\n\n".join(common.block_text(b) for b in _blocks(data)
                       if b.get("type") == "text" and common.block_text(b).strip())


def _tool_call_text(block):
    """A tool call as text: the shell command it ran, else its name and
    input -- a term often lives only in a call's arguments."""
    state = common.dict_field(block, "state")
    name = block.get("name") if isinstance(block.get("name"), str) else ""
    arguments = state.get("input")
    if isinstance(arguments, dict):
        command = arguments.get("command") or arguments.get("cmd")
        if isinstance(command, list):
            command = " ".join(str(piece) for piece in command)
        if isinstance(command, str) and command.strip():
            return command.strip()
    if arguments in (None, {}, ""):
        return name
    body = arguments if isinstance(arguments, str) else json.dumps(
        arguments, ensure_ascii=False, default=str)
    return " ".join(piece for piece in (name, body) if piece)[:400]


def _tool_output(block):
    state = common.dict_field(block, "state")
    texts = [common.block_text(b) for b in common.list_field(state, "content")
             if isinstance(b, dict) and b.get("type") == "text"]
    output = state.get("output")
    if isinstance(output, str):
        texts.append(output)
    return "\n".join(t for t in texts if t.strip())


def _first_prompt(sid):
    """(genuine_title, first_prompt_at_all) from the session's user
    messages, with the filters every other title reader applies."""
    fallback = None
    for i, (kind, data) in enumerate(_iter_messages(sid)):
        if i >= TITLE_SCAN_MESSAGES:
            break
        if kind != "user":
            continue
        text = common.unwrap_openclaw_ctx(_user_text(data))
        if not text.strip():
            continue
        stripped = " ".join(text.split())
        if common.is_image_only(stripped):
            continue
        if fallback is None:
            fallback = stripped[:70]
        if not common._is_injected_or_pasted(text) and not common.is_trivial_title(stripped):
            return stripped[:70], fallback
    return None, fallback


def _row_title(sid, stored):
    title = common._as_title(stored)
    if title and not PLACEHOLDER_TITLE_RE.match(title):
        return title
    genuine, fallback = _first_prompt(sid)
    return common._title_or_placeholder(genuine, fallback)


def opencode_light_records(show_all=False):
    """One record per opencode session. Subagent runs (a parent_id) and
    archived sessions are left out unless --all asks for them."""
    records = []
    for sid, directory, title, created, updated, parent, archived in _query(
            "SELECT id, directory, title, time_created, time_updated, parent_id, time_archived "
            "FROM session_v2"):
        if not isinstance(sid, str) or not sid:
            continue
        if not show_all and (parent or archived):
            continue
        records.append({
            "tool": "opencode", "id": sid,
            "ts": _epoch(updated if updated is not None else created),
            "path": None,  # the transcript is in the store, not in a file
            "cwd": _directory(directory),
            "title": _row_title(sid, title),
            "started": _epoch(created),
        })
    return records


def opencode_resolve(prefix):
    """Ids starting with prefix. opencode's ids all open with `ses_`, so a
    prefix typed without it is tried with it too."""
    ids = [r[0] for r in _query("SELECT id FROM session_v2") if isinstance(r[0], str)]
    matches = [i for i in ids if i.startswith(prefix)]
    if not matches and not prefix.startswith("ses_"):
        matches = [i for i in ids if i.startswith("ses_" + prefix)]
    return sorted(set(matches))


def opencode_session_cwd(sid):
    for (directory,) in _query("SELECT directory FROM session_v2 WHERE id = ?", (sid,)):
        return _directory(directory)
    return None


def opencode_session_turns(sid):
    """How many times the user spoke, or None for none -- the convention
    sessions.session_turns documents."""
    rows = _query("SELECT COUNT(*) FROM session_message WHERE session_id = ? AND type = 'user'",
                  (sid,))
    turns = rows[0][0] if rows else 0
    return turns or None


def opencode_handoff_messages(sid):
    """Every user and assistant message as (role, text), in order -- what
    was said, without tool calls or reasoning (the line every exporter
    holds)."""
    messages = []
    for kind, data in _iter_messages(sid):
        if kind == "user":
            text = _user_text(data)
        elif kind == "assistant":
            text = _assistant_text(data)
        else:
            continue
        if text.strip():
            messages.append((kind, text))
    return messages


def _all_texts(sid, with_tool_output=False):
    """Every text the conversation offers, in order: what the user and the
    assistant said, the assistant's reasoning, its tool calls -- and what
    they returned when `with_tool_output` (the literal scan)."""
    texts = []
    for kind, data in _iter_messages(sid):
        if kind == "user":
            text = _user_text(data)
            if text.strip():
                texts.append(text)
            continue
        if kind != "assistant":
            continue
        for block in _blocks(data):
            btype = block.get("type")
            if btype in ("text", "reasoning"):
                text = common.block_text(block)
                if text.strip():
                    texts.append(text if btype == "text" else text[:400])
            elif btype == "tool":
                call = _tool_call_text(block)
                if call:
                    texts.append(call)
                if with_tool_output:
                    output = _tool_output(block)
                    if output:
                        texts.append(output)
    return texts


def opencode_snippet(sid, max_messages=12, max_chars=800):
    """A longer excerpt than the title, for `cw search` -- sampled evenly
    across the whole conversation (common.sample_stride)."""
    texts = [" ".join(t.split()) for t in _all_texts(sid)]
    texts = [t for t in texts if t]
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)


def opencode_contains(sid, pattern):
    """True if the pattern matches anything said, called or returned -- the
    store's twin of the literal file scan."""
    return any(pattern.search(text) for text in _all_texts(sid, with_tool_output=True))


def opencode_last_message(sid):
    """The last thing anyone said, walking back past tool-only turns and
    injected text."""
    rows = _query("SELECT type, data FROM session_message WHERE session_id = ? "
                  "AND type IN ('user', 'assistant') ORDER BY seq DESC LIMIT 60", (sid,))
    for kind, raw in rows:
        data = _json(raw)
        text = _user_text(data) if kind == "user" else _assistant_text(data)
        if not text.strip():
            continue
        if common.is_image_only(text) or common._is_injected_or_pasted(text):
            continue
        return text  # the caller folds it into one preview line
    return None


def opencode_first_user_text(sid):
    """The session's opening user message, or "" -- where a `cw handoff`
    seed lands when opencode is the target."""
    rows = _query("SELECT data FROM session_message WHERE session_id = ? AND type = 'user' "
                  "ORDER BY seq LIMIT 1", (sid,))
    return _user_text(_json(rows[0][0])) if rows else ""


def opencode_schema():
    """'v2', 'legacy' (the 1.x session/message/part tables), None when the
    store cannot be opened, or '' when it opens as neither -- for doctor."""
    con = _connect()
    if con is None:
        return None
    try:
        names = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error:
        return None
    if {"session_v2", "session_message"} <= names:
        return "v2"
    if {"session", "message", "part"} <= names:
        return "legacy"
    return ""
