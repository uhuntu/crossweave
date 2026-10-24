import io
import json
import os
import subprocess
import tarfile

import pytest

from crossweave import codexpkg

TARGET = "x86_64-pc-windows-msvc"


def _make_tar(entries):
    """entries: {arcname: bytes} -> a tar.gz in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


PACKAGE_164 = _make_tar({
    "bin/codex.exe": b"codex-0.164",
    "bin/codex-code-mode-host.exe": b"host-0.164",
    "codex-package.json": json.dumps({
        "layoutVersion": 1,
        "version": "0.164.0",
        "target": TARGET,
        "variant": "codex",
        "entrypoint": "bin/codex.exe",
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }).encode(),
    "codex-path/rg.exe": b"rg",
    "codex-resources/tool.txt": b"tool",
})


def _link(current, release):
    """Symlink where the platform allows, junction otherwise (unprivileged
    Windows) -- both are links repoint_current must handle."""
    try:
        os.symlink(str(release), str(current), target_is_directory=True)
    except OSError:
        subprocess.run(f'cmd /c mklink /J "{os.path.normpath(current)}" '
                       f'"{os.path.normpath(release)}"', check=True, shell=False)


def _standalone_tree(tmp_path, version="0.163.0", target=TARGET):
    """A minimal packages/standalone tree like install.ps1 leaves behind,
    nested where find_standalone_install expects it."""
    standalone = tmp_path / "codex-home" / "packages" / "standalone"
    release = standalone / "releases" / f"{version}-{target}"
    (release / "bin").mkdir(parents=True)
    (release / "bin" / "codex.exe").write_bytes(b"codex-0.163")
    (standalone / "auto-update-version").write_text(f"{version}-{target}")
    _link(standalone / "current", release)
    return standalone, release


def _fake_download(payload, recorded):
    def download(tag, target, dest, proxy=None, out=print):
        recorded.append((tag, target, proxy))
        with open(dest, "wb") as fh:
            fh.write(payload)
    return download


def test_parse_release_tag_rust_and_plain():
    assert codexpkg.parse_release_tag("rust-v0.164.0") == "0.164.0"
    assert codexpkg.parse_release_tag("v0.5.1") == "0.5.1"
    assert codexpkg.parse_release_tag("0.5.1") == "0.5.1"


def test_find_standalone_install_reads_layout(tmp_path, monkeypatch):
    standalone, release = _standalone_tree(tmp_path)
    monkeypatch.setattr(codexpkg.shutil, "which",
                        lambda _tool: str(release / "bin" / "codex.exe"))
    assert codexpkg.find_standalone_install() == (str(standalone), "0.163.0", TARGET)


def test_find_standalone_install_none_for_flat_install(tmp_path, monkeypatch):
    # An npm/brew-style install: codex on PATH with no packages/standalone.
    flat = tmp_path / "bin"
    flat.mkdir()
    (flat / "codex.exe").write_bytes(b"codex")
    monkeypatch.setattr(codexpkg.shutil, "which",
                        lambda _tool: str(flat / "codex.exe"))
    assert codexpkg.find_standalone_install() is None


def test_install_latest_unpacks_repoints_and_keeps_old_release(tmp_path, monkeypatch):
    standalone, old_release = _standalone_tree(tmp_path)
    monkeypatch.setattr(codexpkg.shutil, "which",
                        lambda _tool: str(old_release / "bin" / "codex.exe"))
    monkeypatch.setattr(codexpkg, "fetch_latest_version",
                        lambda proxy=None: ("0.164.0", "rust-v0.164.0"))
    recorded = []
    monkeypatch.setattr(codexpkg, "download_package",
                        _fake_download(PACKAGE_164, recorded))

    assert codexpkg.install_latest() == 0

    assert recorded == [("rust-v0.164.0", TARGET, None)]
    new_release = standalone / "releases" / f"0.164.0-{TARGET}"
    assert (new_release / "bin" / "codex.exe").read_bytes() == b"codex-0.164"
    assert (new_release / "bin" / "codex-code-mode-host.exe").read_bytes() == b"host-0.164"
    assert (new_release / "codex-path" / "rg.exe").read_bytes() == b"rg"
    assert (new_release / "codex-resources" / "tool.txt").read_bytes() == b"tool"
    assert (new_release / "codex-package.json").is_file()
    # the old release stays in place -- rollback is pointing current back
    assert (old_release / "bin" / "codex.exe").read_bytes() == b"codex-0.163"
    assert os.path.realpath(standalone / "current") == os.path.realpath(new_release)
    # install.ps1's format: exact string, no trailing newline
    assert (standalone / "auto-update-version").read_text() == f"0.164.0-{TARGET}"


def test_install_latest_passes_proxy_to_download(tmp_path, monkeypatch):
    standalone, old_release = _standalone_tree(tmp_path)
    monkeypatch.setattr(codexpkg.shutil, "which",
                        lambda _tool: str(old_release / "bin" / "codex.exe"))
    monkeypatch.setattr(codexpkg, "fetch_latest_version",
                        lambda proxy=None: ("0.164.0", "rust-v0.164.0"))
    recorded = []
    monkeypatch.setattr(codexpkg, "download_package",
                        _fake_download(PACKAGE_164, recorded))

    assert codexpkg.install_latest(proxy="http://127.0.0.1:7897") == 0
    assert recorded[0][2] == "http://127.0.0.1:7897"


def test_install_latest_already_current_downloads_nothing(tmp_path, monkeypatch, capsys):
    standalone, old_release = _standalone_tree(tmp_path)
    monkeypatch.setattr(codexpkg.shutil, "which",
                        lambda _tool: str(old_release / "bin" / "codex.exe"))
    monkeypatch.setattr(codexpkg, "fetch_latest_version",
                        lambda proxy=None: ("0.163.0", "rust-v0.163.0"))
    monkeypatch.setattr(codexpkg, "download_package",
                        lambda *a, **kw: pytest.fail("download attempted"))
    assert codexpkg.install_latest() == 0
    assert "already installed" in capsys.readouterr().out


def test_install_latest_refuses_non_standalone_install(tmp_path, monkeypatch, capsys):
    flat = tmp_path / "bin"
    flat.mkdir()
    monkeypatch.setattr(codexpkg.shutil, "which",
                        lambda _tool: str(flat / "codex.exe"))
    monkeypatch.setattr(codexpkg, "fetch_latest_version",
                        lambda proxy=None: pytest.fail("network attempted"))
    assert codexpkg.install_latest() == 1
    assert "not a standalone install" in capsys.readouterr().err


def test_safe_extract_skips_traversal_and_links(tmp_path):
    archive = tmp_path / "pkg.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        ok = tarfile.TarInfo("ok.txt")
        ok.size = 2
        tf.addfile(ok, io.BytesIO(b"ok"))
        bad = tarfile.TarInfo("../evil.txt")
        bad.size = 4
        tf.addfile(bad, io.BytesIO(b"evil"))
        link = tarfile.TarInfo("link.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "ok.txt"
        tf.addfile(link)
    dest = tmp_path / "out"
    dest.mkdir()
    codexpkg.safe_extract(str(archive), str(dest))
    assert (dest / "ok.txt").read_bytes() == b"ok"
    assert not (tmp_path / "evil.txt").exists()
    assert not (dest / "link.txt").exists()


@pytest.mark.parametrize("name", [
    "C:/outside.txt", "C:outside.txt", "../outside.txt", "..\\outside.txt",
    "/outside.txt", "\\\\server\\share\\outside.txt",
])
def test_safe_extract_rejects_unsafe_paths_on_every_platform(tmp_path, monkeypatch, name):
    archive = tmp_path / "pkg.tar.gz"
    archive.write_bytes(_make_tar({name: b"bad"}))
    monkeypatch.setattr(tarfile.TarFile, "extract",
                        lambda *a, **kw: pytest.fail("unsafe member extracted"))
    codexpkg.safe_extract(str(archive), str(tmp_path / "out"))


@pytest.mark.parametrize("kind", [
    tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE,
    tarfile.SYMTYPE, tarfile.LNKTYPE,
])
def test_safe_extract_rejects_special_members(tmp_path, monkeypatch, kind):
    archive = tmp_path / "pkg.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo("special")
        member.type = kind
        member.linkname = "target"
        tf.addfile(member)
    monkeypatch.setattr(tarfile.TarFile, "extract",
                        lambda *a, **kw: pytest.fail("special member extracted"))
    codexpkg.safe_extract(str(archive), str(tmp_path / "out"))


def test_safe_extract_does_not_follow_existing_directory_link(tmp_path):
    archive = tmp_path / "pkg.tar.gz"
    archive.write_bytes(_make_tar({"linked/outside.txt": b"bad"}))
    dest = tmp_path / "out"
    outside = tmp_path / "outside"
    dest.mkdir()
    outside.mkdir()
    _link(dest / "linked", outside)
    codexpkg.safe_extract(str(archive), str(dest))
    assert not (outside / "outside.txt").exists()


def test_repoint_current_refuses_real_directory(tmp_path):
    standalone = tmp_path / "standalone"
    current = standalone / "current"
    (current / "bin").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        codexpkg.repoint_current(str(standalone), str(tmp_path / "elsewhere"))


def test_repoint_current_preserves_original_when_link_creation_fails(tmp_path, monkeypatch):
    standalone, old_release = _standalone_tree(tmp_path)

    def fail_create(*args):
        raise OSError("cannot create replacement link")

    monkeypatch.setattr(codexpkg, "_create_directory_link", fail_create)
    with pytest.raises(OSError, match="cannot create replacement"):
        codexpkg.repoint_current(str(standalone), str(tmp_path / "next-release"))
    assert os.path.realpath(standalone / "current") == os.path.realpath(old_release)
    assert (old_release / "bin" / "codex.exe").is_file()
    assert not list(standalone.glob(".cw-current-*"))


def test_repoint_current_restores_original_when_activation_fails(tmp_path, monkeypatch):
    standalone, old_release = _standalone_tree(tmp_path)
    new_release = standalone / "releases" / f"0.164.0-{TARGET}"
    new_release.mkdir()
    replace = os.replace

    def fail_activate(source, dest):
        if os.path.basename(source) == "next":
            raise OSError("cannot activate replacement link")
        return replace(source, dest)

    monkeypatch.setattr(codexpkg.os, "replace", fail_activate)
    with pytest.raises(OSError, match="cannot activate replacement"):
        codexpkg.repoint_current(str(standalone), str(new_release))
    assert os.path.realpath(standalone / "current") == os.path.realpath(old_release)
    assert (old_release / "bin" / "codex.exe").is_file()
    assert new_release.is_dir()
    assert not list(standalone.glob(".cw-current-*"))
