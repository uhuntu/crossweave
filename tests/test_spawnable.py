"""common.spawnable: npm's Windows .cmd launchers are started by their target.

Real report: `cw 1` on a pi row answered "cw: 'pi' not found on PATH" with pi
installed -- npm puts pi.cmd on PATH, and a bare name only ever finds .exe.
The two launcher shapes below are copied from a real npm install.
"""

import shutil

import pytest

from crossweave import common

NODE_SHIM = r'''@ECHO off
SETLOCAL
CALL :find_dp0
IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
)
endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & set PATHEXT=%PATHEXT:;.JS;=;% & "%_prog%"  "%dp0%\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js" %*
'''

EXE_SHIM = r'''@ECHO off
SETLOCAL
CALL :find_dp0
"%dp0%\node_modules\@opencode\cli\bin\opencode.exe"   %*
'''


@pytest.fixture
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(common.os, "name", "nt")

    def which(name):
        for ext in (".cmd", ".exe"):
            path = tmp_path / (name + ext)
            if path.exists():
                return str(path)
        return None

    monkeypatch.setattr(shutil, "which", which)
    return tmp_path


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_node_script_launcher_runs_the_script_under_node(windows):
    _write(windows / "pi.cmd", NODE_SHIM)
    script = windows / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    _write(script)
    _write(windows / "node.exe")
    argv = common.spawnable(["pi", "--session", "a & b %X%"])
    assert argv[1].endswith("cli.js") and argv[0].endswith("node.exe")
    assert argv[2:] == ["--session", "a & b %X%"]  # no cmd.exe to re-parse them


def test_a_binary_launcher_runs_the_binary(windows):
    _write(windows / "opencode.cmd", EXE_SHIM)
    _write(windows / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe")
    argv = common.spawnable(["opencode", "--prompt", "hi"])
    assert argv[0].endswith("opencode.exe") and argv[1:] == ["--prompt", "hi"]


def test_an_unknown_launcher_falls_back_to_cmd(windows):
    _write(windows / "odd.cmd", "@echo off\r\nsomething else %*\r\n")
    argv = common.spawnable(["odd", "x"])
    assert argv[1] == "/c" and argv[2].endswith("odd.cmd") and argv[3:] == ["x"]


def test_a_real_exe_and_a_missing_tool_pass_through(windows):
    _write(windows / "claude.exe")
    assert common.spawnable(["claude", "-p"]) == ["claude", "-p"]
    assert common.spawnable(["nosuch"]) == ["nosuch"]
