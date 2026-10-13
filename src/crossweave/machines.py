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
import subprocess
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


def own_index():
    """This machine's index as a dict, gathered fresh -- the dump half of
    the pull mode (`cw machines`, what ssh calls run remotely) and the
    heart of the publish."""
    from . import sessions

    light = []
    for gather in (
        sessions.claude_light_records,
        sessions.codex_light_records,
        lambda: sessions.kimi_light_records(False),
        lambda: sessions.step_light_records(False),
        lambda: sessions.zcode_light_records(False),
        sessions.codebuddy_light_records,
        sessions.workbuddy_light_records,
    ):
        light += gather()
    return _build_index(light)


def _build_index(light_records):
    """The full, unfiltered light set -> the index dict. The index
    describes the machine, not the question."""
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

    return {
        "schema": SCHEMA,
        "machine": machine_name(),
        "machine_id": machine_id(),
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cw_version": __version__,
        "sessions": entries[:INDEX_CAP],
    }


def write_own_index(light_records):
    """Publish this machine's index; a no-op while the directory does not
    exist. `light_records` is the listing's already-gathered set. Atomic
    like the listing cache and the alias manifest: a torn file in a sync
    directory must not read back as "this machine has no sessions".
    Best-effort like them too -- a listing must never fail because a sync
    dir hiccuped."""
    if not os.path.isdir(MACHINES_DIR):
        return None
    index = _build_index(light_records)
    try:
        os.makedirs(MACHINES_DIR, exist_ok=True)
        tmp = os.path.join(MACHINES_DIR, f".{machine_name()}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh)
        os.replace(tmp, own_index_path())
    except OSError:
        return None
    return own_index_path()


def _coerce_index(data, source, now, mine):
    """A parsed index dict -> the merged-listing shape, or None.

    Every refusal is one stderr line naming `source` (a file path or an
    ssh target) and is never fatal -- the tolerant-reader contract holds
    for anything a sync tool dropped mid-flight or an older cw wrote.
    The local machine's own index is skipped silently: the local store is
    always fresher than this machine's index of itself."""
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SCHEMA:
        print(f"cw sessions: skipping {source}: unknown schema "
              f"{data.get('schema')!r}", file=sys.stderr)
        return None
    if data.get("machine_id") == mine:
        return None
    try:
        generated = datetime.datetime.fromisoformat(data["generated_at"])
    except (KeyError, TypeError, ValueError):
        print(f"cw sessions: skipping {source}: unreadable generated_at",
              file=sys.stderr)
        return None
    if generated.tzinfo is None:
        generated = generated.astimezone()
    age = (now - generated).total_seconds()
    if age > STALE_MACHINE_SECONDS:
        print(f"cw sessions: skipping {source}: index is "
              f"{int(age // 86400)} days old", file=sys.stderr)
        return None
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
    if not (isinstance(machine, str) and machine):
        # files fall back to their filename; ssh targets to themselves
        machine = (os.path.basename(source)[:-len(".json")]
                   if source.endswith(".json") else source)
    return {
        "machine": machine,
        "machine_id": data.get("machine_id"),
        "path": source,
        "generated_at": generated,
        "sessions": sessions_out,
    }


def read_remote_indexes(now=None):
    """Every other machine's fresh-enough index in the sync directory."""
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
        index = _coerce_index(read_json(path), path, now, mine)
        if index:
            found.append(index)
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


# The two shells a fleet actually has cannot run the same command line: the
# Windows .cmd path needs cmd's %USERPROFILE% expansion, and a POSIX bash
# reads a word starting with % as a job spec -- `%foo` means `fg %foo`, so
# the Windows form produced "fg: no job control" on every jump host (seen
# live 2026-10-12). One cheap probe tells them apart: %OS% expands under
# cmd.exe to Windows_NT and passes through a POSIX shell verbatim -- padded
# so the % never opens the word.
_WIN_PROBE = "echo pre%OS%post"

POSIX_ATTEMPTS = (
    "cw machines",
    "sh -lc 'cw machines'",
    "$HOME/.local/bin/cw machines",
    "sh -lc '$HOME/.local/bin/cw machines'",
)
WINDOWS_ATTEMPTS = ('"%USERPROFILE%\\.local\\bin\\cw.cmd" machines',)


def _parse_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def _probe_target(ssh_argv, timeout):
    """(is_cmd, detail) for one ssh target: True = the shell is cmd.exe,
    False = a POSIX shell, None = the host did not answer (detail says
    why)."""
    try:
        proc = subprocess.run(ssh_argv + [_WIN_PROBE],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None, "timed out"
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        return None, (lines[-1] if lines else f"exit {proc.returncode}")
    return "Windows_NT" in proc.stdout, ""


def _answer_detail(proc):
    """The one stderr line a failed attempt earns, translated where it
    names a fixable cause: an old cw says `unknown tool 'machines'`, and
    what the user should do about that is update that machine."""
    lines = (proc.stderr or "").strip().splitlines()
    last = lines[-1] if lines else f"exit {proc.returncode}"
    if "unknown tool" in last:
        return "has a cw older than this feature -- run `cw update` on it"
    return last


def pull_remote_indexes(targets, timeout=15):
    """Fresh indexes pulled over ssh, one per live target.

    Each target is any ssh destination -- an alias from ~/.ssh/config
    works as-is, jump hosts included. Failures are per-host, one stderr
    line, never fatal: an unreachable host, one without cw, or one still
    running a cw older than this feature must not take the merged listing
    down. BatchMode keeps a password-only host from hanging the listing
    at a prompt -- key auth, or a mux master already holding it, is the
    price of a non-interactive pull."""
    now = datetime.datetime.now().astimezone()
    mine = machine_id()
    found = []
    for target in targets:
        ssh_argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target]
        is_cmd, detail = _probe_target(ssh_argv, timeout)
        if is_cmd is None:
            print(f"cw sessions: {target}: {detail}", file=sys.stderr)
            continue
        index = None
        self_index = False
        for command in (WINDOWS_ATTEMPTS if is_cmd else POSIX_ATTEMPTS):
            try:
                proc = subprocess.run(ssh_argv + [command],
                                      capture_output=True, text=True, timeout=timeout)
            except (subprocess.TimeoutExpired, OSError):
                detail = "timed out"
                continue
            if proc.returncode != 0:
                detail = _answer_detail(proc)
                continue
            data = _parse_json(proc.stdout)
            if isinstance(data, dict) and data.get("machine_id") == mine:
                self_index = True  # pulled ourselves: nothing to add, no complaint
                break
            index = _coerce_index(data, f"ssh {target}", now, mine)
            if index:
                break
            detail = "did not return an index"
        if index:
            found.append(index)
        elif not self_index:
            print(f"cw sessions: {target}: {detail}", file=sys.stderr)
    return found


def cmd_machines(args):
    """The ssh endpoint of the pull mode: print this machine's index JSON.

    `cw sessions --machines <host>` runs `cw machines` on the named hosts
    (through the fallback cascade above) and merges what comes back; this
    command is that remote half. Not for human eyes -- pipe it through
    `python -m json.tool` if you want to read it."""
    if args:
        print("cw machines: takes no arguments -- this prints the machine "
              "index JSON that `cw sessions --machines <host>` pulls",
              file=sys.stderr)
        return 2
    print(json.dumps(own_index()))
    return 0


if __name__ == "__main__":
    # `python -m crossweave.machines`: the same JSON `cw machines` prints,
    # for pip installs and debugging without a launcher on PATH.
    print(json.dumps(own_index()))
