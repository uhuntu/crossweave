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

from crossweave import codebuddy, common, machines, opencode, pi, sessions, step, workbuddy, zcode


@pytest.fixture(autouse=True)
def _isolate_stores(monkeypatch, tmp_path):
    nowhere = tmp_path / "no-stores"
    monkeypatch.setattr(common, "CLAUDE_PROJECTS", str(nowhere / "claude"))
    monkeypatch.setattr(common, "CODEX_HOME", str(nowhere / "codex"))
    monkeypatch.setattr(common, "KIMI_HOME", str(nowhere / "kimi"))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(nowhere / "step"))
    monkeypatch.setattr(pi, "PI_SESSIONS", str(nowhere / "pi"))
    monkeypatch.setattr(opencode, "OPENCODE_DB", str(nowhere / "opencode" / "opencode.db"))
    monkeypatch.setattr(codebuddy, "CODEBUDDY_PROJECTS", str(nowhere / "cb-cli"))
    # Both editions, so a machine with the real WorkBuddy installed leaks
    # neither of its stores into a test that forgot its own isolation.
    monkeypatch.setattr(workbuddy, "WORKBUDDY_ROOTS",
                        ((str(nowhere / "workbuddy"), "workbuddy"),
                         (str(nowhere / "workbuddy-ai"), "workbuddy-ai")))
    monkeypatch.setattr(zcode, "ZCODE_DB", str(nowhere / "zcode" / "db.sqlite"))
    monkeypatch.setattr(zcode, "ZCODE_TUI_MEMO", str(nowhere / "zcode-no-tui.json"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(nowhere / "last_list.json"))
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(nowhere / "handoffs"))
    # remembered ssh targets would send a --machines test out over ssh
    monkeypatch.setattr(machines, "KNOWN_TARGETS_FILE", str(nowhere / "ssh_targets.json"))
    # the machine's own npm launchers would rewrite argv[0] (pi -> node.exe)
    # under every test that fakes a subprocess; test_spawnable covers it
    monkeypatch.setattr(sessions, "spawnable", list)
    # the developer's own vault would put ✎ marks into every listing test
    monkeypatch.delenv("CW_VAULT", raising=False)
    # ...and so would the default one, when CW_VAULT is unset
    from crossweave import note
    monkeypatch.setattr(note, "DEFAULT_VAULT", str(nowhere / "default-vault"))
    # the developer's own LLM Wiki would attach its pages to every launch
    # test, and its token would reach a test's fake API
    for name in ("CW_LLM_WIKI", "CW_LLM_WIKI_API", "LLM_WIKI_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    # A test that lists or launches with a vault would start the real daily
    # upkeep in the background, outside this isolation; test_autoexport
    # opts back in.
    monkeypatch.setenv("CW_AUTO", "off")
    monkeypatch.setenv("CW_LLM_WIKI_EXPORT", "off")
