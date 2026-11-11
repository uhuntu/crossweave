"""Windows resume works even when the shared Codex daemon is inaccessible."""
import subprocess

import pytest

from crossweave import sessions


@pytest.mark.parametrize("command", ["resume", "fork"])
def test_supported_cli_bypasses_daemon_and_preserves_arguments(monkeypatch, command):
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 0, "  --no-daemon\n"))
    argv = ["codex", command, "saved-id", "-m", "chosen-model"]
    assert sessions._codex_without_daemon(argv) == [
        "codex", "--no-daemon", command, "saved-id", "-m", "chosen-model"]


@pytest.mark.parametrize("output,rc", [("old help", 0), ("--no-daemon", 1)])
def test_older_or_failed_cli_probe_keeps_command(monkeypatch, output, rc):
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], rc, output))
    argv = ["codex", "resume", "saved-id"]
    assert sessions._codex_without_daemon(argv) == argv


@pytest.mark.parametrize("error", [OSError("unavailable"),
                                  subprocess.TimeoutExpired("codex", 5)])
def test_probe_failure_does_not_prevent_launch(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(sessions.subprocess, "run", fail)
    argv = ["codex", "resume", "saved-id"]
    assert sessions._codex_without_daemon(argv) == argv


@pytest.mark.parametrize("argv", [
    ["claude", "--resume", "saved-id"], ["codex", "exec", "prompt"],
    ["codex", "resume", "saved-id", "--no-daemon"],
    ["codex", "resume", "saved-id", "--remote", "ws://localhost:1234"],
    ["codex", "resume", "saved-id", "--remote=ws://localhost:1234"],
])
def test_explicit_options_and_other_commands_are_preserved(monkeypatch, argv):
    def unexpected(*args, **kwargs):
        raise AssertionError("must not probe")
    monkeypatch.setattr(sessions.subprocess, "run", unexpected)
    assert sessions._codex_without_daemon(argv) == argv


def test_windows_launch_inherits_terminal_handles(monkeypatch):
    monkeypatch.setattr(sessions, "_codex_without_daemon", lambda argv:
                        ["codex", "--no-daemon", *argv[1:]])
    monkeypatch.setattr(sessions, "spawnable", list)
    calls = []
    monkeypatch.setattr(sessions.subprocess, "call", lambda *a, **kw:
                        calls.append((a, kw)) or 0)
    # Undo immediately: os is shared with pytest and pathlib.
    with monkeypatch.context() as patch:
        patch.setattr(sessions.os, "name", "nt")
        with pytest.raises(SystemExit) as result:
            sessions.exec_or_die(["codex", "resume", "saved-id"])
    assert result.value.code == 0
    assert calls == [((["codex", "--no-daemon", "resume", "saved-id"],), {})]
