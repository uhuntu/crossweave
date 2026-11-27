"""Reader for Kiro's session store: ~/.kiro/sessions/<workspace-hash>/sess_<uuid>/.

Each session directory holds session.json (id, title, workspacePaths,
createdAt, lastModifiedAt) and messages.jsonl, one {id, timestamp, payload}
record per line. A user or assistant turn keeps its text in payload.content.
The Kiro CLI and the Kiro VS Code extension both write this store, so one
reader covers sessions from either. Nothing here writes to ~/.kiro.
"""
import calendar
import os
import time

from . import common

SESSIONS_DIR = os.path.join(common.HOME, ".kiro", "sessions")

# Titles Kiro writes before it has named a session. A session that still
# carries one of these falls back to its first user message.
KIRO_PLACEHOLDER_TITLES = {"new session", "new kiro session"}
KIRO_TITLE_CHARS = 70


def _session_dirs():
    """Every sess_* directory under every workspace-hash directory."""
    if not os.path.isdir(SESSIONS_DIR):
        return []
    dirs = []
    for ws in sorted(os.listdir(SESSIONS_DIR)):
        wsdir = os.path.join(SESSIONS_DIR, ws)
        if not os.path.isdir(wsdir):
            continue
        for name in sorted(os.listdir(wsdir)):
            sdir = os.path.join(wsdir, name)
            if name.startswith("sess_") and os.path.isdir(sdir):
                dirs.append(sdir)
    return dirs


def _meta(sdir):
    meta = common.read_json(os.path.join(sdir, "session.json"))
    return meta if isinstance(meta, dict) else None


def _turns(sdir):
    """(role, text) for each user or assistant turn with text, in file order.
    Unparseable lines and non-text payloads are skipped by read_jsonl and
    here."""
    for d in common.read_jsonl(os.path.join(sdir, "messages.jsonl")):
        if not isinstance(d, dict):
            continue
        payload = common.dict_field(d, "payload")
        role = payload.get("type")
        if role not in ("user", "assistant"):
            continue
        text = payload.get("content")
        if isinstance(text, str) and text.strip():
            yield role, text


def _first_user_text(sdir):
    for role, text in _turns(sdir):
        if role == "user":
            return text
    return None


def _parse_ts(value):
    """Seconds since epoch from Kiro's ISO-8601 timestamps, e.g.
    "2026-11-27T04:42:26.167Z". Returns 0 when missing or unparseable."""
    if not isinstance(value, str) or not value.strip():
        return 0
    try:
        return calendar.timegm(time.strptime(value.strip()[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def _cwd(meta):
    paths = meta.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str):
        return paths[0]
    return None


def kiro_stored_title(sdir):
    """Kiro's own title for a session, or None when it has none worth showing
    (missing, blank, or one of KIRO_PLACEHOLDER_TITLES)."""
    meta = _meta(sdir)
    title = meta.get("title") if meta else None
    if not isinstance(title, str) or not title.strip():
        return None
    if title.strip().lower() in KIRO_PLACEHOLDER_TITLES:
        return None
    return title.strip()


def kiro_title(sdir):
    """The stored title if Kiro set one, else the first user message, flattened
    to one line and cut to KIRO_TITLE_CHARS. None when there is no user
    message at all."""
    stored = kiro_stored_title(sdir)
    if stored:
        return stored
    first = _first_user_text(sdir)
    if not first:
        return None
    return " ".join(first.split())[:KIRO_TITLE_CHARS]


def kiro_light_records(show_all):
    """One record per session that has at least one user message. Empty
    sessions (no messages.jsonl, or only an assistant turn) are left out,
    the way claude_is_empty drops them. `show_all` is accepted for the
    listing's shared signature; Kiro has no archived sessions."""
    records = []
    for sdir in _session_dirs():
        meta = _meta(sdir)
        if not meta:
            continue
        if _first_user_text(sdir) is None:
            continue
        sid = meta.get("id") or os.path.basename(sdir)
        records.append({
            "tool": "kiro",
            "id": sid,
            "ts": _parse_ts(meta.get("lastModifiedAt") or meta.get("createdAt")),
            "started": _parse_ts(meta.get("createdAt")),
            "cwd": _cwd(meta),
            "path": sdir,
        })
    return records


def _session_dir_for(sid):
    for sdir in _session_dirs():
        meta = _meta(sdir)
        if meta and (meta.get("id") or os.path.basename(sdir)) == sid:
            return sdir
    return None


def kiro_session_cwd(sid):
    sdir = _session_dir_for(sid)
    meta = _meta(sdir) if sdir else None
    return _cwd(meta) if meta else None


def kiro_resolve(prefix):
    """Sorted full session ids that start with `prefix`. Like kimi, a bare
    uuid also matches the "sess_" form."""
    ids = []
    for sdir in _session_dirs():
        meta = _meta(sdir)
        if meta and meta.get("id"):
            ids.append(meta["id"])
    matches = [i for i in ids if i.startswith(prefix)]
    if not matches and not prefix.startswith("sess_"):
        matches = [i for i in ids if i.startswith("sess_" + prefix)]
    return sorted(set(matches))


def kiro_snippet(sdir, max_messages=12, max_chars=800):
    """Search and preview text: up to max_messages user and assistant turns,
    sampled across the whole conversation, joined within max_chars."""
    texts = [text for _role, text in _turns(sdir)]
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)


def kiro_handoff_messages(sdir):
    """Every user and assistant turn with text, as (role, text), for the
    handoff export. Nothing is truncated here."""
    return list(_turns(sdir))
