"""cw machines - the multi-machine session index.

cw reads each tool's own on-disk session store, and those stores are
per-machine: a session that happened on the laptop simply does not exist
on the desktop, and no amount of local reading can answer "where was
that conversation". The answer this module ships is an index, not a
service: each machine publishes a small JSON file describing its own
sessions, the files meet in a directory the user syncs themselves
(Syncthing, git, Dropbox -- anything that moves files), and `cw sessions
--machines` merges them into the listing.

No backend, no account, no daemon: the transport is the user's, and the
transcripts never leave the machines they live on -- the index carries
metadata only (tool, id, title, cwd, timestamp). cw never creates the
directory: its existence is the switch. Without it every listing behaves
exactly as before; mkdir it (and point a sync tool at it) to turn the
feature on.

The publish side resolves titles through the same machinery the listing
uses (sessions._resolve_title_and_cwd -- the one step that needs the
local transcripts), so the read side needs nothing but JSON: a remote
index is fully self-describing. That asymmetry is the whole trick --
publish once where the files are, read anywhere they have synced to.
"""
import datetime
import json
import os
import re
import socket
import sys
import uuid

from . import __version__
from .common import HOME, read_json

# Where the per-machine index files meet. The user's transport decides how
# they get there; crossweave only ever reads and writes this directory. It
# is never created by cw -- an existing directory is the switch (see the
# module docstring), so an env var alone must not silently turn publishing
# on for every existing install.
MACHINES_DIR = os.environ.get("CROSSWEAVE_MACHINES_DIR") or os.path.join(
    HOME, ".config", "crossweave", "machines")

# A stable per-machine identity, minted once into the cache dir: two
# machines can share a hostname, and self-detection at read time has to
# survive that (see read_remote_indexes).
MACHINE_ID_FILE = os.path.join(HOME, ".cache", "crossweave", "machine_id")

SCHEMA = 1

# A machine's whole history is not what the merged listing is for --
# discovery of recent work is. Newest-first before the cap, so the cap
# cuts the oldest; a heavy machine's index stays ~150 KB.
INDEX_CAP = 1000

# An index this old stops describing a machine anyone uses; after two
# weeks the rows are more likely ghosts than pointers. One stderr line,
# never a refusal -- the listing-age warning's philosophy (see
# sessions.STALE_LISTING_SECONDS).
STALE_MACHINE_SECONDS = 14 * 86400

# The same charset an alias name must survive (alias.NAME_RE): the machine
# name becomes a filename in the sync directory and a word in every other
# machine's listing.
MACHINE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def machine_name():
    """This machine's name as it appears in the sync directory, sanitized
    into the one charset a command-adjacent word survives. An override that
    sanitizes to nothing falls back, as does a host that cannot name
    itself."""
    raw = os.environ.get("CROSSWEAVE_MACHINE_NAME")
    if not raw:
        try:
            raw = socket.gethostname()
        except Exception:
            raw = ""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", (raw or "").strip())
    return name if MACHINE_NAME_RE.match(name) else "machine"


def machine_id():
    """A stable uuid for this machine, minted on first use.

    Two concurrent listings can mint simultaneously; os.replace makes the
    file whole either way, and the loser's published index is rewritten by
    its next listing -- a transient mismatch self-heals. A cache dir that
    cannot be written leaves the id process-local, which still makes
    self-detection work within the run."""
    try:
        with open(MACHINE_ID_FILE, encoding="utf-8") as fh:
            existing = fh.read().strip()
        if re.fullmatch(r"[0-9a-f]{32}", existing):
            return existing
    except OSError:
        pass
    minted = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(MACHINE_ID_FILE), exist_ok=True)
        tmp = f"{MACHINE_ID_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(minted + "\n")
        os.replace(tmp, MACHINE_ID_FILE)
    except OSError:
        pass  # best-effort -- see the docstring
    return minted


def own_index_path():
    return os.path.join(MACHINES_DIR, machine_name() + ".json")


def write_own_index(light_records):
    """Publish this machine's index; a no-op while the directory does not
    exist. `light_records` is the full, unfiltered light set -- the same
    records a listing would resolve -- not a --tool/--limit slice: the
    index describes the machine, the display filters describe the
    question.

    Atomic like the listing cache and the alias manifest: a torn file in
    a sync directory must not read back as "this machine has no
    sessions". Best-effort like them too -- a listing must never fail
    because a sync dir hiccuped."""
    if not os.path.isdir(MACHINES_DIR):
        return None
    # Call-time import for the same reason render.py imports sessions at
    # call time: that namespace is the patch surface the tests aim at, and
    # a top-level import goes circular once cmd_list publishes.
    from . import sessions

    by_key = {}
    for r in light_records:
        tool, sid = r.get("tool"), r.get("id")
        if not (isinstance(tool, str) and tool and isinstance(sid, str) and sid):
            continue
        old = by_key.get((tool, sid))
        if old is None or (r.get("ts") or 0) >= (old.get("ts") or 0):
            by_key[(tool, sid)] = r

    entries = []
    for (tool, sid), r in by_key.items():
        try:
            title, cwd_show = sessions._resolve_title_and_cwd(r)
        except Exception:
            # One malformed record loses its title, not the machine's
            # whole publish -- the listing tolerates rows the same way.
            title, cwd_show = "(no title)", "?"
        if not isinstance(cwd_show, str):
            cwd_show = "?"
        if title in sessions.TOOL_STARTED_TITLES:
            continue  # the same rows the default listing hides
        ts = r.get("ts")
        entries.append({
            "tool": tool,
            "id": sid,
            "title": title,
            "cwd": cwd_show,
            "ts": ts if isinstance(ts, (int, float)) else 0,
        })
    entries.sort(key=lambda e: e["ts"], reverse=True)

    index = {
        "schema": SCHEMA,
        "machine": machine_name(),
        "machine_id": machine_id(),
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cw_version": __version__,
        "sessions": entries[:INDEX_CAP],
    }
    try:
        os.makedirs(MACHINES_DIR, exist_ok=True)
        tmp = os.path.join(MACHINES_DIR, f".{machine_name()}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh)
        os.replace(tmp, own_index_path())
    except OSError:
        return None
    return own_index_path()


def read_remote_indexes(now=None):
    """Every other machine's fresh-enough index in the sync directory.

    Each refusal is one stderr line and never fatal -- a foreign file, a
    half-synced one, or a machine that stopped being used two weeks ago
    must not take the merged listing down. The local machine's own file is
    skipped silently: the local store is always fresher than this
    machine's own index of itself."""
    if not os.path.isdir(MACHINES_DIR):
        return []
    now = now or datetime.datetime.now().astimezone()
    mine = machine_id()
    try:
        names = sorted(os.listdir(MACHINES_DIR))
    except OSError:
        return []
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(MACHINES_DIR, name)
        data = read_json(path)
        if not isinstance(data, dict):
            continue  # torn mid-sync, or not an index at all -- silent
        if data.get("schema") != SCHEMA:
            print(f"cw sessions: skipping {path}: unknown schema "
                  f"{data.get('schema')!r}", file=sys.stderr)
            continue
        if data.get("machine_id") == mine:
            continue
        try:
            generated = datetime.datetime.fromisoformat(data["generated_at"])
        except (KeyError, TypeError, ValueError):
            print(f"cw sessions: skipping {path}: unreadable generated_at",
                  file=sys.stderr)
            continue
        if generated.tzinfo is None:
            generated = generated.astimezone()
        age = (now - generated).total_seconds()
        if age > STALE_MACHINE_SECONDS:
            print(f"cw sessions: skipping {path}: index is "
                  f"{int(age // 86400)} days old", file=sys.stderr)
            continue
        raw = data.get("sessions")
        sessions_out = []
        for s in raw if isinstance(raw, list) else []:
            if not isinstance(s, dict):
                continue
            tool, sid = s.get("tool"), s.get("id")
            if not (isinstance(tool, str) and tool and isinstance(sid, str) and sid):
                continue
            title = s.get("title")
            cwd = s.get("cwd")
            ts = s.get("ts")
            sessions_out.append({
                "tool": tool,
                "id": sid,
                "title": title if isinstance(title, str) else "(no title)",
                "cwd": cwd if isinstance(cwd, str) else "?",
                "ts": ts if isinstance(ts, (int, float)) else 0,
            })
        machine = data.get("machine")
        found.append({
            "machine": machine if isinstance(machine, str) and machine else name[:-len(".json")],
            "machine_id": data.get("machine_id"),
            "path": path,
            "generated_at": generated,
            "sessions": sessions_out,
        })
    return found


def remote_sessions(indexes, local_keys):
    """Remote-only sessions, newest first, each tagged with its machine.

    `local_keys` is the local light set's {(tool, id)}: a session the
    local store also holds (mirrored stores do this) is already a
    resumable local row, and the remote entry carries nothing the local
    one lacks -- coalescing it away is lossless, and it is what makes a
    store-mirroring machine fully functional rather than read-only. The
    same rule dedupes two remote machines sharing a store: newest ts
    wins, machine name breaks ties, so the merged listing is deterministic
    whichever machine's index arrived first."""
    by_key = {}
    for index in indexes:
        machine = index["machine"]
        for s in index["sessions"]:
            key = (s["tool"], s["id"])
            if key in local_keys:
                continue
            candidate = dict(s, machine=machine)
            old = by_key.get(key)
            if old is None or (-candidate["ts"], candidate["machine"]) < (-old["ts"], old["machine"]):
                by_key[key] = candidate
    return sorted(by_key.values(), key=lambda s: (-s["ts"], s["machine"], s["id"]))
