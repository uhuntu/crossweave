"""Shared test isolation: no test reads the machine's real session stores.

Every store root points at an empty path under the test's own tmp directory
by default, so a test that forgets its own isolation sees an empty world
rather than the developer's actual sessions. The per-module isolation
fixtures keep working -- and keep winning, since a module fixture set after
this one overrides it -- but they are now a way to give a test a specific
shape, not the thing standing between the suite and the real data.

The roots are the module-level constants the readers resolve at call time:
the per-tool stores, the two listing caches, and the handoff export dir.
"""

import pytest

from crossweave import cb, codebuddy, common, sessions, step, zcode


@pytest.fixture(autouse=True)
def _isolate_stores(monkeypatch, tmp_path):
    nowhere = tmp_path / "no-stores"
    monkeypatch.setattr(common, "CLAUDE_PROJECTS", str(nowhere / "claude"))
    monkeypatch.setattr(common, "CODEX_HOME", str(nowhere / "codex"))
    monkeypatch.setattr(common, "KIMI_HOME", str(nowhere / "kimi"))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(nowhere / "step"))
    monkeypatch.setattr(codebuddy, "CODEBUDDY_PROJECTS", str(nowhere / "cb-cli"))
    monkeypatch.setattr(zcode, "ZCODE_DB", str(nowhere / "zcode" / "db.sqlite"))
    monkeypatch.setattr(zcode, "ZCODE_TUI_MEMO", str(nowhere / "zcode-no-tui.json"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(nowhere / "last_list.json"))
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(nowhere / "handoffs"))
    monkeypatch.setattr(cb, "CLI_ROOT", str(nowhere / "cb" / "projects"))
    monkeypatch.setattr(cb, "VS_GENIE", str(nowhere / "cb" / "genie"))
    monkeypatch.setattr(cb, "VS_QUEUE", str(nowhere / "cb" / "queue"))
    monkeypatch.setattr(cb, "VS_TODOS", str(nowhere / "cb" / "todos"))
    monkeypatch.setattr(cb, "VS_CHANGES", str(nowhere / "cb" / "changes"))
    monkeypatch.setattr(cb, "DESKTOP_DB", str(nowhere / "cb" / "sessions.vscdb"))
    monkeypatch.setattr(cb, "LIST_CACHE_FILE", str(nowhere / "cb_last_list.json"))
