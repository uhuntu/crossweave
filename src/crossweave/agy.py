"""Reader for Google Antigravity (agy) session store.

Antigravity stores session metadata in a SQLite database at
~/.gemini/antigravity/conversation_summaries.db (`conversation_summaries`
table) and per-session JSONL transcripts at
~/.gemini/antigravity/brain/<conversation_id>/.system_generated/logs/transcript.jsonl.

The database belongs to Antigravity's desktop/CLI runtime, which may be
active while cw reads, so every connection is read-only (`mode=ro`,
per-thread like opencode/zcode), and cw never writes to it.
"""
from datetime import datetime
import json
import os
import re
import sqlite3
import threading
from urllib.parse import unquote, urlparse

from . import common


def _default_dir():
    return os.path.join(common.HOME, ".gemini", "antigravity")


AGY_DIR = _default_dir()
AGY_DB = os.path.join(AGY_DIR, "conversation_summaries.db")
AGY_BRAIN = os.path.join(AGY_DIR, "brain")

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_TRAILING_TAGS_RE = re.compile(
    r"\s*<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>\s*",
    re.S,
)

_local = threading.local()


def _drop_connection():
    con = getattr(_local, "connection", None)
    if con is not None:
        try:
            con.close()
        except sqlite3.Error:
            pass
    _local.connection = None
    _local.key = None


def _connect():
    db = AGY_DB
    try:
        st = os.stat(db)
    except OSError:
        _drop_connection()
        return None
    key = (db, st.st_dev, st.st_ino)
    if getattr(_local, "connection", None) is not None and getattr(_local, "key", None) == key:
        return _local.connection
    _drop_connection()
    try:
        _local.connection = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        _local.key = key
    except sqlite3.Error:
        _local.connection = None
        _local.key = None
    return _local.connection


def _query(query, params=()):
    """All rows, or [] when the store is missing or unreadable."""
    con = _connect()
    if con is None:
        return []
    try:
        return con.execute(query, params).fetchall()
    except sqlite3.Error:
        return []


def _ts(value):
    """Parse an ISO-8601 timestamp or numeric epoch into integer seconds."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e12 else int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            pass
        try:
            num = float(text)
            return int(num / 1000) if num > 1e12 else int(num)
        except ValueError:
            return 0
    return 0


def _decode_uri(uri):
    """Turn a file:// URI or plain path into a normalized OS path."""
    if not isinstance(uri, str):
        return None
    uri = uri.strip()
    if not uri:
        return None
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path or "")
        if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
    else:
        path = uri
    if not path:
        return None
    return os.path.normpath(path) if os.name == "nt" else path


def _workspace_cwd(raw):
    """Extract the primary workspace directory from `workspace_uris`."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except Exception:
            items = []
        if isinstance(items, list):
            for item in items:
                decoded = _decode_uri(item)
                if decoded:
                    return decoded
        return None
    for part in text.splitlines():
        decoded = _decode_uri(part)
        if decoded:
            return decoded
    return None


def agy_transcript_path(sid):
    """Path to a session's transcript.jsonl, or None if not on disk."""
    if not sid:
        return None
    path = os.path.join(AGY_BRAIN, sid, ".system_generated", "logs", "transcript.jsonl")
    return path if os.path.isfile(path) else None


def _clean_user_text(raw):
    if not isinstance(raw, str):
        return ""
    m = _USER_REQUEST_RE.search(raw)
    if m:
        return m.group(1).strip()
    return _TRAILING_TAGS_RE.sub("", raw).strip()


def _iter_transcript(sid):
    """Yield (role, text, step_dict) for user and planner steps in transcript.jsonl."""
    path = agy_transcript_path(sid)
    if not path:
        return
    for d in common.read_jsonl(path):
        kind = d.get("type")
        if kind == "USER_INPUT":
            yield "user", _clean_user_text(d.get("content", "")), d
        elif kind == "PLANNER_RESPONSE":
            text = d.get("content") if isinstance(d.get("content"), str) else ""
            yield "assistant", text, d


def _tool_call_text(call):
    if not isinstance(call, dict):
        return ""
    name = call.get("name") if isinstance(call.get("name"), str) else ""
    args = call.get("arguments") or call.get("args")
    if isinstance(args, dict):
        cmd = args.get("CommandLine") or args.get("command") or args.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
    if args in (None, {}, ""):
        return name
    body = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
    return " ".join(p for p in (name, body) if p)[:400]


def _first_prompt(sid, preview=None):
    """(genuine_title, fallback) from transcript or DB preview."""
    fallback = None
    for role, text, _ in _iter_transcript(sid):
        if role != "user":
            continue
        if not text.strip():
            continue
        stripped = " ".join(text.split())
        if common.is_image_only(stripped):
            continue
        if fallback is None:
            fallback = stripped[:70]
        if not common._is_injected_or_pasted(text) and not common.is_trivial_title(stripped):
            return stripped[:70], fallback
    if fallback is None and isinstance(preview, str) and preview.strip():
        stripped = " ".join(_clean_user_text(preview).split())
        if stripped:
            fallback = stripped[:70]
            if not common.is_trivial_title(stripped):
                return stripped[:70], fallback
    return None, fallback


def _row_title(sid, stored, preview=None):
    title = common._as_title(stored)
    if title:
        return title
    genuine, fallback = _first_prompt(sid, preview=preview)
    return common._title_or_placeholder(genuine, fallback)


def agy_light_records(show_all=False):
    """One record per Antigravity conversation. Subagent conversations
    (parent_conversation_id or nesting_depth > 0) are omitted unless --all."""
    records = []
    rows = _query(
        "SELECT conversation_id, title, preview, last_modified_time, workspace_uris, "
        "parent_conversation_id, nesting_depth "
        "FROM conversation_summaries"
    )
    for sid, title, preview, modified, workspace_uris, parent_id, depth in rows:
        if not isinstance(sid, str) or not sid:
            continue
        if not show_all and (parent_id or (depth or 0) > 0):
            continue
        records.append({
            "tool": "agy",
            "id": sid,
            "ts": _ts(modified),
            "path": agy_transcript_path(sid),
            "cwd": _workspace_cwd(workspace_uris),
            "title": _row_title(sid, title, preview=preview),
        })
    return records


def agy_resolve(prefix):
    ids = [r[0] for r in _query("SELECT conversation_id FROM conversation_summaries")
           if isinstance(r[0], str)]
    return sorted({i for i in ids if i.startswith(prefix)})


def agy_session_cwd(sid):
    for (workspace_uris,) in _query(
            "SELECT workspace_uris FROM conversation_summaries WHERE conversation_id = ?", (sid,)):
        return _workspace_cwd(workspace_uris)
    return None


def agy_session_turns(sid):
    """User turn count from transcript.jsonl, falling back to DB step_count."""
    path = agy_transcript_path(sid)
    if path:
        turns = sum(1 for role, text, _ in _iter_transcript(sid)
                    if role == "user" and text.strip())
        return turns or None
    rows = _query(
        "SELECT step_count FROM conversation_summaries WHERE conversation_id = ?", (sid,))
    if rows and isinstance(rows[0][0], int) and rows[0][0] > 0:
        return rows[0][0]
    return None


def agy_handoff_messages(sid):
    """Every user and assistant message as (role, text), in order."""
    messages = []
    for role, text, _ in _iter_transcript(sid):
        if text.strip():
            messages.append((role, text))
    return messages


def _all_texts(sid, with_tool_output=False):
    texts = []
    for role, text, d in _iter_transcript(sid):
        if role == "user":
            if text.strip():
                texts.append(text)
            continue
        if text.strip():
            texts.append(text)
        thinking = d.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            texts.append(thinking[:400])
        for call in common.list_field(d, "tool_calls"):
            call_text = _tool_call_text(call)
            if call_text:
                texts.append(call_text)
    if not texts and with_tool_output:
        for (title, preview) in _query(
                "SELECT title, preview FROM conversation_summaries WHERE conversation_id = ?",
                (sid,)):
            for val in (title, preview):
                if isinstance(val, str) and val.strip():
                    texts.append(val)
    return texts


def agy_snippet(sid, max_messages=12, max_chars=800):
    texts = [" ".join(t.split()) for t in _all_texts(sid)]
    texts = [t for t in texts if t]
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)


def agy_contains(sid, pattern):
    return any(pattern.search(text) for text in _all_texts(sid, with_tool_output=True))


def agy_last_message(sid):
    last = None
    for _, text, _ in _iter_transcript(sid):
        if not text.strip():
            continue
        if common.is_image_only(text) or common._is_injected_or_pasted(text):
            continue
        last = text
    return last


def agy_first_user_text(sid):
    for role, text, _ in _iter_transcript(sid):
        if role == "user" and text.strip():
            return text
    return ""


def agy_schema():
    """'v1' when conversation_summaries exists, None if unreadable, '' otherwise."""
    con = _connect()
    if con is None:
        return None
    try:
        names = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error:
        return None
    return "v1" if "conversation_summaries" in names else ""
