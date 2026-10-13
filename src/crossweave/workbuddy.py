"""Reader for WorkBuddy's session store: the CodeBuddy Code engine running
inside a desktop app, writing the JSONL chain that engine always writes --
under its own root instead of CodeBuddy's.

`~/.workbuddy/projects/<slug>/<sessionId>.jsonl` is byte-for-byte the format
codebuddy.py reads: `message` records with a top-level role and typed
input_text/output_text blocks, `function_call`/`function_call_result` as
their own records, `reasoning` siblings, an `ai-title` the auto-titler
writes, big tool results in `<slug>/<sessionId>/tool-results/*.txt`, and a
resume that appends to the same file. That is not a coincidence worth
parsing twice -- the app bundles `@genie/agent-cli`, which is the package
the `codebuddy` binary on PATH is built from (its bin/codebuddy and the
2.161.x version line are the same ones), so this module reuses codebuddy's
record readers instead of copying them. What WorkBuddy adds on top are two
record kinds a plain CLI session never carries -- a `session-meta` header
(which host kind started the session) and `file-history-snapshot` (the
rollback points behind `<id>.file-rollback.ndjson`) -- and both are records
the shared readers already look past.

Two apps keep two stores: WorkBuddy in `~/.workbuddy` and WorkBuddy AI in
`~/.workbuddy-ai`, each registering its own URL scheme. They are read as one
tool, because they are one product's two editions and a listing that splits
them asks the reader to know which is which; a row remembers the scheme it
came in under so the way back opens the app that owns it.

There is no `workbuddy` executable on PATH, and the bundled CLI cannot be
borrowed either: `resources/app.asar.unpacked/cli/bin/codebuddy` is a node
script whose requires resolve into `app.asar`, which only Electron's patched
loader can see -- run under the node WorkBuddy itself bundles, it dies with
MODULE_NOT_FOUND before it reads a flag (checked, 5.8.2). So the route back
into a session is the app's own deep link, `workbuddy://chat/<id>`, which is
what its tray controller builds for "navigate to session" -- a session-level
route, where zcode's desktop link only reaches a workspace.

The standalone `codebuddy` does read these transcripts: point
CODEBUDDY_CONFIG_DIR at the root and run `-r <id>` from the session's own
directory and it resolves the session (verified -- it got as far as the
model gateway and quoted the id back in the error). It is not wired up as a
resume because it answers on the CodeBuddy account rather than WorkBuddy's,
which makes `cw resume workbuddy` a different product's bill.

Conversations the app keeps in the cloud (`workbuddy.db`'s `sessions` rows
with transport='cloud') have no transcript on this machine and so no row
here: cw lists what is on disk, which is what makes a row searchable and
handoff-able.
"""
import os
import shutil
import subprocess
import sys
import urllib.parse

from . import common
from .codebuddy import _scan_file, _sec



# (store root, URL scheme) per edition. The scheme is the root's own name --
# the AI edition registers `workbuddy-ai`, and its handler points at a
# different executable, so a link built under the wrong one opens an app
# that has never heard of the session.
WORKBUDDY_ROOTS = (
    (os.path.join(common.HOME, ".workbuddy"), "workbuddy"),
    (os.path.join(common.HOME, ".workbuddy-ai"), "workbuddy-ai"),
)

PROJECTS_DIRNAME = "projects"

# A subagent's transcript lives under its parent's session directory, in the
# same format and with a session id of its own: a session WorkBuddy started
# for itself, holding another session's context rather than a request of
# anyone's. step's `subagent-<uuid>.jsonl` files are the same case and get
# the same treatment -- left out unless --all asks for them.
SUBAGENTS_DIRNAME = "subagents"

# The app's own spelling: `workbuddy://chat/${encodeURIComponent(id)}` in
# its tray controller, where a session id is the only argument.
CHAT_LINK = "%s://chat/%s"

# Where the Windows installer puts the two executables, for a machine whose
# HKCU handler is missing or points somewhere stale.
_WINDOWS_EXE_NAMES = {"workbuddy": ("WorkBuddy", "WorkBuddy.exe"),
                      "workbuddy-ai": ("WorkBuddyAI", "WorkBuddyAI.exe")}


def _transcripts(pdir, show_all):
    """Every transcript path under one project directory: the sessions
    themselves, plus their subagents' when `show_all` asks.

    The siblings of a transcript are not transcripts -- `<id>.meta.json` is
    the host kind, `<id>.file-rollback.ndjson` is the file-history log -- so
    only the `.jsonl` at the top of the directory is read, and a subagent's
    is found one level deeper under its parent's id."""
    for fn in sorted(os.listdir(pdir)):
        if not fn.endswith(".jsonl"):
            continue
        yield os.path.join(pdir, fn)
        if not show_all:
            continue
        sdir = os.path.join(pdir, fn[:-len(".jsonl")], SUBAGENTS_DIRNAME)
        if not os.path.isdir(sdir):
            continue
        for sub in sorted(os.listdir(sdir)):
            if sub.endswith(".jsonl"):
                yield os.path.join(sdir, sub)


def _record(path, scheme):
    """One light record, or None when the file holds no session.

    codebuddy's single-pass scan answers everything the listing needs -- id,
    cwd, the titler's title, the first genuine prompt behind it, the time
    span, the turn count -- and WorkBuddy's records are its records. What the
    app adds is a wrapper around every user message, which the reader unwraps
    on the user's side (see common.USER_QUERY_RE); without that the listing
    would show these sessions as holding no prompts at all."""
    (sid, cwd, custom_title, ai_title, fallback, first, last,
     turns) = _scan_file(path)
    if not sid:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    title = custom_title or ai_title
    if title and common.is_seed_restatement(title):
        title = None
    if not title:
        title = common._title_or_placeholder(None, fallback)
    return {
        "tool": "workbuddy", "id": sid, "path": path,
        "cwd": cwd, "title": title,
        "ts": _sec(last) or int(mtime),
        "started": _sec(first) or int(mtime),
        "turns": turns,
        "scheme": scheme,
    }


def workbuddy_light_records(show_all=False):
    """One record per WorkBuddy session on this machine, both editions."""
    records = []
    for root, scheme in WORKBUDDY_ROOTS:
        projects = os.path.join(root, PROJECTS_DIRNAME)
        if not os.path.isdir(projects):
            continue
        for slug in sorted(os.listdir(projects)):
            pdir = os.path.join(projects, slug)
            if not os.path.isdir(pdir):
                continue
            for path in _transcripts(pdir, show_all):
                record = _record(path, scheme)
                if record is not None:
                    records.append(record)
    return records


def workbuddy_resolve(prefix, show_all=False):
    matches = []
    for r in workbuddy_light_records(show_all=True):
        if r["id"].startswith(prefix):
            matches.append(r["id"])
    return sorted(set(matches))


def workbuddy_chat_link(record):
    """The deep link that opens this session in the app that owns it.

    The id is percent-encoded with nothing left safe, which is also what
    makes it inert as an argument: encodeURIComponent's spelling leaves
    alphanumerics and `_.-~` and escapes everything a shell or cmd.exe would
    read as a metacharacter, so a corrupt or hostile store cannot turn this
    into a second command."""
    scheme = (record or {}).get("scheme") or WORKBUDDY_ROOTS[0][1]
    return CHAT_LINK % (scheme, urllib.parse.quote(str((record or {}).get("id") or ""), safe=""))


def _windows_scheme_exe(scheme):
    """The executable HKCU hands `scheme://` URLs to, else the one the
    installer is known to have written, else None. cb.py reads the same key
    for codebuddy:// -- the handler is the authority on which app owns a
    scheme, and the install path is the fallback for a machine whose key was
    never written."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["reg", "query", rf"HKCU\Software\Classes\{scheme}\shell\open\command"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        if '"' not in line:
            continue
        first = line.split('"')[1]
        if first.lower().endswith(".exe") and os.path.isfile(first):
            return first
    names = _WINDOWS_EXE_NAMES.get(scheme)
    base = os.environ.get("LOCALAPPDATA", "")
    if names and base:
        cand = os.path.join(base, "Programs", names[0], names[1])
        if os.path.isfile(cand):
            return cand
    return None


def workbuddy_open_command(record):
    """An argv that opens one session in its app, or None when this machine
    has no way to hand a URL to one.

    Popen rather than exec, and the caller's terminal stays where it was:
    the app is not the terminal's successor process, it is a window that
    happens to have been asked for (the same compromise zcode's desktop
    fallback and cb's desktop handler make)."""
    link = workbuddy_chat_link(record)
    scheme = (record or {}).get("scheme") or WORKBUDDY_ROOTS[0][1]
    if sys.platform == "darwin":
        return ["open", link]
    if os.name == "nt":
        exe = _windows_scheme_exe(scheme)
        if exe:
            return [exe, link]
        # No handler to read and no install to guess at: let the shell do
        # whatever it does with the scheme, which on a machine that has the
        # app is the right thing and on one that does not is a visible
        # failure rather than a silent one.
        return ["cmd", "/c", "start", "", link]
    opener = "xdg-open"
    return [opener, link] if shutil.which(opener) else None


def workbuddy_roots_present():
    """The (root, scheme) pairs whose store directory exists -- what doctor
    reports, so a machine with only one edition installed says so instead of
    reading as half broken."""
    return [(root, scheme) for root, scheme in WORKBUDDY_ROOTS
            if os.path.isdir(os.path.join(root, PROJECTS_DIRNAME))]
