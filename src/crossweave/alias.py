"""cw alias - personal command names, managed by the tool that owns them.

Two renames left a trail of compatibility aliases, and everyone running
crossweave across several machines eventually wants their own on top:
one user's `ai` outlived both renames (98 invocations against 7 for the
official short command), and `xx`/`cx` arrived the same afternoon. The
installer mints only the project's own names -- a deliberate line, drawn
twice -- so personal ones were hand-made files per machine: a four-step
ritual with MSYS-symlink and profile-function pitfalls, different on
each shell, and easy to half-do (one such half-migration stranded a
function mid-rename).

`cw alias` replaces the ritual with the mechanism: `add` writes a
launcher for any name you choose, pointing at the installation you are
running right now; `list` reads back the manifest; `remove` undoes. The
names are yours -- the installer never creates or removes them. The
manifest (~/.config/crossweave/aliases.json) is what makes the set
portable: on a fresh machine, install, copy the manifest (or remember
the names), and `cw alias add` with no arguments re-creates every one.

Names are validated against the union of every namespace they must
survive: letters, digits, `.`, `_`, `-` only -- `#` dies as a PyPI name
(PEP 503), as a repo name, and as shell comment syntax, which is
exactly where a two-letter name lives. The installer's own commands
(cw, crossweave, cb) are refused: an alias on top of them could only
shadow, never add.
"""
import json
import os
import re
import sys

from .common import HOME

# Where personal launchers go -- the same directory the installer uses,
# overridable by the same variable. Resolved at import like the other
# module constants; tests reassign it.
ALIAS_BIN_DIR = os.environ.get("CROSSWEAVE_BIN_DIR") or os.path.join(HOME, ".local", "bin")

# The manifest of names created through this command. Config, not cache:
# it is user intent, meant to outlive updates and to be copied to a new
# machine as the restore list.
ALIAS_MANIFEST = os.path.join(HOME, ".config", "crossweave", "aliases.json")

# The union of every namespace a command name must survive: a filename in
# the bin dir, a PyPI-adjacent word (PEP 503 allows letters, digits, . _ -),
# and a shell word (`#` begins a comment in three major shells -- see the
# module docstring's `x#` story). One spec, no exceptions per shell.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

# The installer's primary commands: an alias on top of these could only
# shadow the thing it names, never add to it. Heritage links (aim, aimux,
# clisweave) are allowed targets -- overwriting them changes nothing they
# point at (bin/cw either way), and they are the user's to manage.
PROTECTED = {"cw", "crossweave", "cb"}

# Windows device names are filenames that silently mean hardware.
RESERVED = {"CON", "PRN", "AUX", "NUL"}


def _read_manifest():
    from .common import read_json

    data = read_json(ALIAS_MANIFEST)
    if not isinstance(data, dict) or not isinstance(data.get("names"), list):
        return []
    return [n for n in data["names"] if isinstance(n, str)]


def _write_manifest(names):
    """Atomic for the same reason the listing cache is (see
    sessions.write_list_cache): a truncated manifest reads back as
    'no aliases', and `cw alias add` with no arguments would then
    restore nothing."""
    os.makedirs(os.path.dirname(ALIAS_MANIFEST), exist_ok=True)
    tmp = f"{ALIAS_MANIFEST}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"names": sorted(set(names))}, fh, indent=2)
    os.replace(tmp, ALIAS_MANIFEST)


def _launcher_target(package_dir=None):
    """(kind, target) for the installation this process runs from.

    script: a git/curl-clone layout -- the repo's bin/cw sits beside the
    package, and launchers point at it (the installer's own target, so
    aliases and installer links follow the same clone).
    module: a pip install -- no bin/cw; launchers run this interpreter
    against the installed package (-m crossweave.cli)."""
    if package_dir is None:
        package_dir = os.path.dirname(os.path.abspath(__file__))
    # src layout: <repo>/src/crossweave -- the repo root is TWO dirs up,
    # and its bin/cw is the installer's own target. One dirname short and
    # the clone install misreads as pip, whose -m launchers then die on
    # the system interpreter (seen live 2026-10-11: three launchers, one
    # ModuleNotFoundError each).
    repo_root = os.path.dirname(os.path.dirname(package_dir))
    bin_cw = os.path.join(repo_root, "bin", "cw")
    if os.path.isfile(bin_cw):
        return "script", bin_cw
    return "module", None


def _cmd_encoding():
    # install.ps1 writes its launchers in the OEM codepage for the same
    # reason: cmd.exe reads .cmd files through it, and a non-ASCII clone
    # path in UTF-8 arrives as mojibake.
    return "mbcs" if os.name == "nt" else "utf-8"


def _write_launcher(name, kind, target):
    """Write one launcher; returns the path it created.

    Windows: a .cmd wrapping the running interpreter (sys.executable --
    the one interpreter that demonstrably runs this package; probing a
    second one is how install.ps1 spends its Get-WorkingPython).
    POSIX: an executable shim. A symlink to bin/cw would be closer to
    install.sh, but symlinks need privilege on Windows and the shim
    needs no syscall beyond write -- and MSYS's ln -s silently degrades
    to a copy, which is exactly the half-broken shim this command
    exists to stop shipping by hand."""
    os.makedirs(ALIAS_BIN_DIR, exist_ok=True)
    exe = sys.executable or "python3"
    if os.name == "nt":
        path = os.path.join(ALIAS_BIN_DIR, name + ".cmd")
        if kind == "script":
            body = '@echo off\r\n"%s" "%s" %%*\r\n' % (exe, target)
        else:
            body = '@echo off\r\n"%s" -m crossweave.cli %%*\r\n' % exe
        with open(path, "w", encoding=_cmd_encoding(), newline="") as fh:
            fh.write(body)
    else:
        path = os.path.join(ALIAS_BIN_DIR, name)
        lines = ["#!" + exe]
        if " " in exe:
            # a shebang carries exactly one argument; a spaced interpreter
            # path cannot ride in one -- route through env instead
            lines = ["#!/usr/bin/env python3"]
        if kind == "script":
            src_dir = os.path.dirname(os.path.dirname(os.path.abspath(target)))
            lines += ["import sys",
                      "sys.path.insert(0, %r)" % src_dir]
        lines += ["from crossweave.cli import main", "main()"]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(path, 0o755)
    return path


def _remove_launcher(name):
    for suffix in (".cmd", ""):
        path = os.path.join(ALIAS_BIN_DIR, name + suffix)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _validate(names):
    """Every refusal up front, so `add` never half-applies a list."""
    for name in names:
        if not NAME_RE.match(name):
            print(f"cw alias: '{name}' is not a valid command name "
                  "(letters, digits, . _ - only -- the union of filename, "
                  "PyPI-name and shell-word rules)", file=sys.stderr)
            return False
        if name.upper() in RESERVED:
            print(f"cw alias: '{name}' is a reserved device name on Windows",
                  file=sys.stderr)
            return False
        if name in PROTECTED:
            print(f"cw alias: '{name}' is crossweave's own command -- an alias "
                  "on top of it could only shadow it", file=sys.stderr)
            return False
        existing = os.path.join(ALIAS_BIN_DIR, name + (".cmd" if os.name == "nt" else ""))
        if os.path.exists(existing) and name not in _read_manifest():
            # A file this command did not create: overwriting it silently
            # is how someone's unrelated `xx` tool dies.
            print(f"cw alias: {existing} already exists and was not created by "
                  "`cw alias` -- remove it first if it is yours", file=sys.stderr)
            return False
    return True


def cmd_alias(argv):
    """Dispatch for `cw alias add|list|remove`."""
    if not argv or argv[0] not in ("add", "list", "remove"):
        print("Usage: cw alias add <name>... | list | remove <name>...\n"
              "  add with no names re-creates every name in the manifest "
              "(the fresh-machine restore).", file=sys.stderr)
        return 2

    sub, rest = argv[0], argv[1:]

    if sub == "list":
        names = _read_manifest()
        if not names:
            print("(no personal aliases -- `cw alias add <name>` creates one)")
            return 0
        for name in names:
            suffix = ".cmd" if os.name == "nt" else ""
            path = os.path.join(ALIAS_BIN_DIR, name + suffix)
            state = "ok" if os.path.isfile(path) else "MISSING"
            print(f"{name}  {state}  {path}")
        return 0

    names = rest
    if sub == "add" and not names:
        # The fresh-machine restore: every name ever added, re-created.
        names = _read_manifest()
        if not names:
            print("cw alias: nothing to restore -- the manifest is empty", file=sys.stderr)
            return 1
        print("restoring: " + ", ".join(names), file=sys.stderr)

    if not names:
        print("cw alias: %s requires at least one name" % sub, file=sys.stderr)
        return 2
    if not _validate(names):
        return 1

    if sub == "remove":
        for name in names:
            _remove_launcher(name)
        kept = [n for n in _read_manifest() if n not in names]
        _write_manifest(kept)
        for name in names:
            print(f"removed {name}")
        return 0

    kind, target = _launcher_target()
    for name in names:
        path = _write_launcher(name, kind, target)
        shown = target if kind == "script" else "crossweave.cli (python -m)"
        print(f"linked {path} -> {shown}")
    _write_manifest(_read_manifest() + names)
    return 0
