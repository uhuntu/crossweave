"""cw alias: personal command names as a managed mechanism.

The ritual this replaces was four hand-made files per machine, each shell
with its own pitfalls, one of which (a PS profile function) held the only
living copy of a command. These tests hold the mechanism to: add writes a
launcher that actually runs, the manifest is what makes the set portable,
remove undoes cleanly, and the validation refuses exactly the names that
cannot survive every namespace a command name lives in.
"""

import json
import os
import subprocess
import sys

import pytest

from crossweave import alias, cli


@pytest.fixture(autouse=True)
def _alias_env(monkeypatch, tmp_path):
    monkeypatch.setattr(alias, "ALIAS_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(alias, "ALIAS_MANIFEST", str(tmp_path / "config" / "aliases.json"))


@pytest.fixture(autouse=True)
def _script_target(monkeypatch, tmp_path):
    """Default: a fake clone layout (<repo>/src/crossweave + bin/cw), so
    add writes script-form launchers naming the clone. Tests that want
    the pip form re-patch _launcher_target themselves."""
    make_repo(tmp_path)
    bin_cw = str(tmp_path / "repo" / "bin" / "cw")
    monkeypatch.setattr(alias, "_launcher_target", lambda: ("script", bin_cw))


def make_repo(tmp_path, with_bin_cw=True):
    pkg = tmp_path / "repo" / "src" / "crossweave"
    pkg.mkdir(parents=True)
    if with_bin_cw:
        (tmp_path / "repo" / "bin").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "bin" / "cw").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return str(pkg)


def launcher_path(name):
    suffix = ".cmd" if os.name == "nt" else ""
    return os.path.join(alias.ALIAS_BIN_DIR, name + suffix)


def read_launcher(path):
    encoding = "mbcs" if os.name == "nt" else "utf-8"
    with open(path, encoding=encoding) as fh:
        return fh.read()


def manifest_names():
    with open(alias.ALIAS_MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)["names"]


# ---------- the layout probe itself ----------

def test_the_probe_reads_a_src_layout_clone(tmp_path):
    """The math that cost three launchers live: package_dir is
    <repo>/src/crossweave, so the repo root -- where bin/cw lives -- is
    two dirnames up, not one."""
    pkg = tmp_path / "repo" / "src" / "crossweave"
    pkg.mkdir(parents=True)
    bin_cw = tmp_path / "repo" / "bin" / "cw"
    bin_cw.parent.mkdir(parents=True)
    bin_cw.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    assert alias._launcher_target(str(pkg)) == ("script", str(bin_cw))


def test_the_probe_reads_a_pip_layout_as_module(tmp_path):
    pkg = tmp_path / "venv" / "Lib" / "site-packages" / "crossweave"
    pkg.mkdir(parents=True)
    assert alias._launcher_target(str(pkg)) == ("module", None)


# ---------- add ----------

def test_add_writes_a_launcher_pointing_at_the_clone(tmp_path):
    assert alias.cmd_alias(["add", "xx"]) == 0

    path = launcher_path("xx")
    assert os.path.isfile(path)
    body = read_launcher(path)
    # both platforms' launchers name the clone they target
    assert str(tmp_path / "repo") in body
    if os.name != "nt":
        # the shim must find the package: the clone's src goes on sys.path
        assert "sys.path.insert" in body


def test_add_without_bin_cw_falls_back_to_the_module_form(monkeypatch, tmp_path):
    """A pip install has no bin/cw: the launcher runs this interpreter
    against the installed package instead (-m)."""
    monkeypatch.setattr(alias, "_launcher_target", lambda: ("module", None))
    assert alias.cmd_alias(["add", "xx"]) == 0

    body = read_launcher(launcher_path("xx"))
    assert "crossweave.cli" in body
    assert "sys.path.insert" not in body


def test_the_launcher_actually_runs(monkeypatch, tmp_path):
    """End to end: add, then invoke. The point of the mechanism is that
    the created file works -- not that it looks right."""
    monkeypatch.setattr(alias, "_launcher_target", lambda: ("module", None))
    assert alias.cmd_alias(["add", "yy"]) == 0

    result = subprocess.run([launcher_path("yy"), "--version"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "crossweave" in result.stdout


def test_add_records_the_manifest_and_restores_from_it(tmp_path):
    assert alias.cmd_alias(["add", "xx", "cx"]) == 0
    assert manifest_names() == ["cx", "xx"]

    # a fresh machine: launchers gone, manifest copied over -- one command
    for name in ("xx", "cx"):
        os.remove(launcher_path(name))
        assert not os.path.isfile(launcher_path(name))

    assert alias.cmd_alias(["add"]) == 0
    assert os.path.isfile(launcher_path("xx"))
    assert os.path.isfile(launcher_path("cx"))


def test_add_with_no_args_and_empty_manifest_says_so(capsys):
    assert alias.cmd_alias(["add"]) == 1
    assert "manifest is empty" in capsys.readouterr().err


def test_re_add_updates_an_alias_the_mechanism_owns():
    assert alias.cmd_alias(["add", "xx"]) == 0
    assert alias.cmd_alias(["add", "xx"]) == 0  # own launcher: refresh, not refuse
    assert manifest_names() == ["xx"]


# ---------- remove / list ----------

def test_remove_deletes_the_launcher_and_the_manifest_entry(tmp_path, capsys):
    alias.cmd_alias(["add", "xx", "cx"])
    capsys.readouterr()  # discard the add output

    assert alias.cmd_alias(["remove", "xx"]) == 0
    assert not os.path.isfile(launcher_path("xx"))
    assert manifest_names() == ["cx"]
    capsys.readouterr()  # discard "removed xx"

    assert alias.cmd_alias(["list"]) == 0
    assert "xx" not in capsys.readouterr().out


def test_list_reports_a_missing_launcher(tmp_path, capsys):
    alias.cmd_alias(["add", "xx"])
    capsys.readouterr()
    os.remove(launcher_path("xx"))

    assert alias.cmd_alias(["list"]) == 0
    out = capsys.readouterr().out
    assert "xx" in out and "MISSING" in out


def test_list_on_an_empty_manifest(capsys):
    assert alias.cmd_alias(["list"]) == 0
    assert "no personal aliases" in capsys.readouterr().out


# ---------- validation ----------

def test_x_hash_is_rejected(tmp_path, capsys):
    """The name that motivated the spec: `x#` dies as a PyPI name (PEP
    503), as a repo name, and as shell comment syntax -- the validation
    is the union of the namespaces a command name lives in."""
    assert alias.cmd_alias(["add", "x#"]) == 1
    assert "not a valid command name" in capsys.readouterr().err
    assert not os.path.exists(launcher_path("x#"))


def test_a_partial_list_never_half_applies(tmp_path, capsys):
    """`add xx x#` writes neither: validation runs over the whole list
    before the first file is written."""
    assert alias.cmd_alias(["add", "xx", "x#"]) == 1
    assert not os.path.exists(launcher_path("xx"))
    assert not os.path.exists(alias.ALIAS_MANIFEST)


def test_the_installers_own_commands_are_protected(capsys):
    for name in ("cw", "crossweave", "cb"):
        assert alias.cmd_alias(["add", name]) == 1
        assert "crossweave's own command" in capsys.readouterr().err


def test_windows_device_names_are_refused(tmp_path, capsys):
    assert alias.cmd_alias(["add", "CON"]) == 1
    assert "reserved" in capsys.readouterr().err


def test_a_foreign_file_is_never_overwritten(tmp_path, capsys):
    """A name that already exists in the bin dir but was not created by
    `cw alias` could be anyone's tool -- refuse rather than clobber."""
    foreign = launcher_path("xx")
    os.makedirs(alias.ALIAS_BIN_DIR, exist_ok=True)
    with open(foreign, "w") as fh:
        fh.write("rem someone else's xx\n")

    assert alias.cmd_alias(["add", "xx"]) == 1
    assert "remove it first" in capsys.readouterr().err
    assert "someone else's xx" in read_launcher(foreign)


# ---------- wiring ----------

def test_usage_and_unknown_subcommand_are_refused(capsys):
    assert alias.cmd_alias([]) == 2
    assert "Usage: cw alias" in capsys.readouterr().err

    assert alias.cmd_alias(["wat"]) == 2
    assert "Usage: cw alias" in capsys.readouterr().err


def test_remove_requires_a_name(capsys):
    assert alias.cmd_alias(["remove"]) == 2
    assert "requires at least one name" in capsys.readouterr().err


def test_cli_dispatches_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["cw", "alias", "add", "zz"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert os.path.isfile(launcher_path("zz"))

    monkeypatch.setattr(sys, "argv", ["cw", "alias"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
