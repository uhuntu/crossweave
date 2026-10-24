"""Direct installer for Codex CLI's standalone package, used as `cw update`'s
fallback when codex's own updater cannot run.

Codex's `codex update` bootstraps by downloading OpenAI's install script
(chatgpt.com/codex/install.ps1 / install.sh) and running it.  On 2026-10-24
that URL started answering 404 (verified from two independent networks), so
`codex update` dies before it downloads anything, and no proxy can help a
missing file.

What the install script would have done is small enough to redo here: fetch
the ready-made `codex-package-<target>.tar.gz` asset from openai/codex's
GitHub Releases -- the same laid-out payload install.ps1 unpacks (bin/,
codex-path/, codex-resources/, codex-package.json) -- install it as a new
release under ~/.codex/packages/standalone/releases/, and repoint the
`current` link.  The previous release directory stays in place, so rolling
back is pointing `current` back.

Only standalone installs can be serviced this way.  The layout is detected
from the real path of the codex binary on PATH (an npm-installed codex has
no packages/standalone and gets a clear "cannot fall back" message), and the
target triple is read from the installed release directory's name, so an
aarch64 install gets the aarch64 package even on an x86_64 host."""
import json
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

RELEASES_API = "https://api.github.com/repos/openai/codex/releases/latest"
PACKAGE_URL = ("https://github.com/openai/codex/releases/download/"
               "{tag}/codex-package-{target}.tar.gz")

_MIB = 1024 * 1024


def find_standalone_install():
    """(standalone_root, installed_version, target_triple) for the codex on
    PATH, or None when codex is not a standalone install.  Expects the real
    binary path to end in packages/standalone/releases/<version>-<target>/bin/
    codex(.exe), which is the layout both install.ps1 and install.sh create
    (CODEX_HOME relocation included: realpath follows whatever symlinks the
    launcher uses, e.g. %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin -> current).
    """
    exe = shutil.which("codex")
    if not exe:
        return None
    exe = os.path.realpath(exe)
    if os.path.basename(exe) not in ("codex", "codex.exe"):
        return None
    bin_dir = os.path.dirname(exe)
    release_dir = os.path.dirname(bin_dir)
    releases_dir = os.path.dirname(release_dir)
    standalone = os.path.dirname(releases_dir)
    if (os.path.basename(bin_dir) != "bin"
            or os.path.basename(release_dir) == ""
            or os.path.basename(releases_dir) != "releases"
            or os.path.basename(standalone) != "standalone"
            or os.path.basename(os.path.dirname(standalone)) != "packages"):
        return None
    version, _, target = os.path.basename(release_dir).partition("-")
    if not version or not target:
        return None
    current = os.path.join(standalone, "current")
    if not (os.path.islink(current) or os.path.isdir(current)):
        return None
    return standalone, version, target


def parse_release_tag(tag):
    """'rust-v0.164.0' -> '0.164.0'.  Rust-based releases tag 'rust-vX.Y.Z';
    older ones shipped plain 'vX.Y.Z' -- either way the version is whatever
    follows the last 'v'."""
    return tag.rsplit("v", 1)[-1]


def fetch_latest_version(proxy=None):
    """(version, tag) of openai/codex's latest GitHub release."""
    from . import __version__
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(RELEASES_API, headers={
        "User-Agent": f"crossweave/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    with opener.open(request, timeout=30) as resp:
        payload = json.load(resp)
    tag = payload["tag_name"]
    version = parse_release_tag(tag)
    if not re.match(r"^\d+\.\d+\.\d+", version):
        raise RuntimeError(f"unexpected codex release tag {tag!r}")
    return version, tag


def download_package(tag, target, dest, proxy=None, out=print):
    """Stream codex-package-<target>.tar.gz to dest, with a progress line
    every 64 MiB -- the asset is ~150-350MB, long enough on a throttled
    connection that silence reads as a hang."""
    from . import __version__
    url = PACKAGE_URL.format(tag=tag, target=target)
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": f"crossweave/{__version__}"})
    with opener.open(request, timeout=60) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(_MIB)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if done % (64 * _MIB) < len(chunk):
                out(f"  {done // _MIB} MiB"
                    + (f" of {total // _MIB}" if total else "") + " ...", flush=True)
    out(f"  downloaded {done // _MIB} MiB", flush=True)


def safe_extract(archive, dest):
    """Extract only regular files and directories, paths normalized and
    forced inside dest.  The payload is OpenAI's own release, but writing
    symlinks/hardlinks or a ../ path into the user's home would be a bad
    habit even over TLS -- and tarfile's built-in filter= only exists on
    3.11.8+/3.12, while crossweave still supports 3.9."""
    dest = os.path.realpath(dest)
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            if not (member.isfile() or member.isdir()):
                continue
            name = member.name.replace("\\", "/")
            if (name.startswith("/") or ntpath.splitdrive(name)[0]
                    or ".." in name.split("/")):
                continue
            target = os.path.realpath(os.path.join(dest, name))
            if os.path.commonpath((dest, target)) != dest:
                continue
            member.name = name
            tf.extract(member, dest)


def validate_package(extract_dir):
    """The layout install.ps1 validates: codex-package.json present, sane,
    and its entrypoint unpacked next to it."""
    manifest_path = os.path.join(extract_dir, "codex-package.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("layoutVersion") != 1:
        raise RuntimeError(f"unsupported codex package layoutVersion "
                           f"{manifest.get('layoutVersion')!r}")
    entrypoint = manifest.get("entrypoint", "")
    if not entrypoint or not os.path.isfile(os.path.join(extract_dir, entrypoint)):
        raise RuntimeError(f"codex package entrypoint {entrypoint!r} missing from archive")
    return manifest


def _is_reparse_point(path):
    if os.name != "nt":
        return False
    st = os.lstat(path)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(st, "st_file_attributes", 0) & flag)


def _create_directory_link(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # Unprivileged Windows can't create symlinks; a junction behaves the
        # same for a same-volume directory target, which releases always is.
        link, target = (os.path.normpath(p) for p in (link, target))
        subprocess.run(f'cmd /c mklink /J "{link}" "{target}"', check=True, shell=False)


def repoint_current(standalone, release_dir):
    """Prepare a replacement link before moving current, and restore the
    original link if activation fails.  Real directories are never replaced."""
    current = os.path.join(standalone, "current")
    if (os.path.lexists(current) and not os.path.islink(current)
            and not _is_reparse_point(current)):
        raise RuntimeError(f"{current} is not a link; refusing to replace it")
    staging = tempfile.mkdtemp(prefix=".cw-current-", dir=standalone)
    candidate = os.path.join(staging, "next")
    backup = os.path.join(staging, "previous")
    preserve_backup = False
    try:
        _create_directory_link(candidate, release_dir)
        if os.path.lexists(current):
            os.replace(current, backup)
        try:
            os.replace(candidate, current)
        except OSError:
            if os.path.lexists(backup):
                try:
                    os.replace(backup, current)
                except OSError as exc:
                    preserve_backup = True
                    raise RuntimeError(f"could not restore {current}; the original "
                                       f"link is preserved at {backup}") from exc
            raise
    finally:
        if not preserve_backup:
            for path in (candidate, backup):
                if os.path.islink(path):
                    os.unlink(path)
                elif os.path.lexists(path) and _is_reparse_point(path):
                    # rmdir removes the junction itself, never its target.
                    os.rmdir(path)
            os.rmdir(staging)


def install_latest(proxy=None):
    """Install the latest codex release straight from GitHub Releases.
    Returns a process-style exit code: 0 installed (or already current), 1
    with a stderr message otherwise."""
    found = find_standalone_install()
    if not found:
        print("codex is not a standalone install (no packages/standalone next "
              "to the codex binary); cannot fall back to a direct install",
              file=sys.stderr)
        return 1
    standalone, version, target = found
    try:
        latest, tag = fetch_latest_version(proxy)
    except Exception as exc:
        print(f"could not query the latest codex release from GitHub: {exc}",
              file=sys.stderr)
        return 1
    if latest == version:
        print(f"codex {latest} is already installed; nothing to do")
        return 0
    releases_dir = os.path.join(standalone, "releases")
    workdir = tempfile.mkdtemp(prefix="cw-codex-", dir=releases_dir)
    try:
        archive = os.path.join(workdir, "package.tar.gz")
        print(f"  installing codex {latest} ({target}) from GitHub Releases ...", flush=True)
        download_package(tag, target, archive, proxy)
        extract_dir = os.path.join(workdir, "pkg")
        os.mkdir(extract_dir)
        safe_extract(archive, extract_dir)
        manifest = validate_package(extract_dir)
        if manifest.get("version") not in (None, latest):
            raise RuntimeError(f"package says version {manifest['version']}, "
                               f"expected {latest}")
        dest = os.path.join(releases_dir, f"{latest}-{target}")
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.move(extract_dir, dest)
        repoint_current(standalone, dest)
        # install.ps1 writes the exact "<version>-<target>" string with no
        # trailing newline (30 bytes for 0.163.0-x86_64-pc-windows-msvc).
        with open(os.path.join(standalone, "auto-update-version"), "w",
                  encoding="utf-8") as fh:
            fh.write(f"{latest}-{target}")
        print(f"codex {latest} installed from GitHub Releases; the previous "
              f"release ({version}) is kept under releases/ -- point "
              f"'current' back at it to roll back", flush=True)
        return 0
    except Exception as exc:
        print(f"codex install from GitHub Releases failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
