"""cw ui: the picker's filter and key handling, without a terminal."""
import sys
import types

from crossweave import ui
from crossweave.render import _display_width


def row(tool, sid, cwd, title):
    return (tool, sid, "1h ago", sid[:20], cwd, title)


ITEMS = [
    (row("claude", "aaa-1", "/w/crossweave", "Obsidian browser connection"), {"id": "aaa-1"}),
    (row("codex", "bbb-2", "/w/other", "fix the NFC lock"), {"id": "bbb-2"}),
    (row("kimi", "ccc-3", "/w/crossweave", "在共享盘上打开 vault"), {"id": "ccc-3"}),
]


LINKS = {}  # id -> [(tool, id)] the session continues; set per test


def picker(monkeypatch, links=None):
    monkeypatch.setattr(ui.note, "noted_index", lambda: {})
    monkeypatch.setattr(ui.note, "topic_noted_index", lambda: [])
    monkeypatch.setattr(ui.sessions, "session_parents", lambda r: (links or {}).get(r["id"], []))
    return ui.Picker(list(ITEMS), "/w/crossweave")


def relink(p, items):
    p.items = items
    p.link(items)
    p.refilter()


def test_every_word_has_to_appear_case_folded():
    assert ui.matches(ITEMS[0][0], ["obsidian", "browser"], None, None)
    assert not ui.matches(ITEMS[0][0], ["obsidian", "nfc"], None, None)
    # cwd, tool and id are searchable too, not only the title
    assert ui.matches(ITEMS[1][0], ["other"], None, None)
    assert ui.matches(ITEMS[1][0], ["codex"], None, None)
    assert ui.matches(ITEMS[2][0], ["共享盘"], None, None)


def test_tool_and_directory_narrow_the_list():
    assert not ui.matches(ITEMS[0][0], [], "codex", None)
    assert ui.matches(ITEMS[2][0], [], None, "/w/crossweave")
    assert not ui.matches(ITEMS[1][0], [], None, "/w/crossweave")


def test_typing_filters_and_enter_resumes_the_highlighted_row(monkeypatch):
    p = picker(monkeypatch)
    assert p.key("/", 10) is None and p.typing
    for ch in "nfc":
        p.key(ch, 10)
    assert [it[0][1] for it in p.shown] == ["bbb-2"]
    assert p.key("\n", 10) == "resume"
    assert p.selected()[0][1] == "bbb-2"


def test_escape_clears_the_filter_and_a_second_one_quits(monkeypatch):
    p = picker(monkeypatch)
    for ch in "/nfc":
        p.key(ch, 10)
    p.key("\x1b", 10)
    assert p.query == "" and len(p.shown) == 3 and not p.typing
    assert p.key("\x1b", 10) == "quit"


def test_tab_cycles_tools_present_and_back_to_all(monkeypatch):
    p = picker(monkeypatch)
    seen = []
    for _ in range(4):
        p.key("\t", 10)
        seen.append(p.tool)
    assert seen == ["claude", "codex", "kimi", None]


def test_dot_keeps_only_this_directory(monkeypatch):
    p = picker(monkeypatch)
    p.key(".", 10)
    assert [it[0][1] for it in p.shown] == ["aaa-1", "ccc-3"]


def test_cursor_stays_inside_the_list(monkeypatch):
    p = picker(monkeypatch)
    p.key("KEY_UP", 10)
    assert p.cursor == 0
    p.key("KEY_NPAGE", 10)
    assert p.cursor == 2
    p.key("j", 10)
    assert p.cursor == 2


def test_enter_on_an_empty_list_does_nothing(monkeypatch):
    p = picker(monkeypatch)
    for ch in "/zzz":
        p.key(ch, 10)
    assert p.shown == []
    assert p.key("\n", 10) is None


def test_wrap_fits_wide_text_and_marks_a_cut():
    lines = ui.wrap("共享盘" * 20, 10, 2)
    assert len(lines) == 2
    assert all(_display_width(line) <= 10 for line in lines)
    assert lines[-1].endswith("…")
    assert ui.wrap("short", 10, 2) == ["short"]


def test_refuses_without_a_terminal(capsys):
    assert ui.cmd_ui([]) == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_action_keys_name_the_command_for_the_highlighted_row(monkeypatch):
    p = picker(monkeypatch)
    assert p.key("n", 10) == "note"
    assert p.key("d", 10) == "delete"
    assert p.key("r", 10) == "rename"
    assert p.key("v", 10) == "show"


def test_action_keys_are_letters_while_typing_a_filter(monkeypatch):
    p = picker(monkeypatch)
    p.key("/", 10)
    for ch in "hndrv":
        assert p.key(ch, 10) is None
    assert p.query == "hndrv"


def test_handoff_asks_for_a_target_other_than_the_source(monkeypatch):
    p = picker(monkeypatch)  # the top row is a claude session
    assert p.key("h", 10) is None
    assert "claude" not in p.handoff_to and "workbuddy" not in p.handoff_to
    target = p.handoff_to[0]
    assert p.key("1", 10) == ("handoff", target)
    assert p.handoff_to is None


def test_any_other_key_cancels_the_handoff_question(monkeypatch):
    p = picker(monkeypatch)
    p.key("h", 10)
    assert p.key("x", 10) is None
    assert p.handoff_to is None
    assert p.key("9", 10) is None  # no longer asking: a digit is just a key


def test_reload_keeps_the_filters_and_the_highlighted_session(monkeypatch):
    p = picker(monkeypatch)
    p.key("/", 10)
    p.key("w", 10)  # every row's cwd holds a "w"
    p.key("KEY_DOWN", 10)
    p.key("KEY_DOWN", 10)  # on ccc-3, the third row
    p.reload([ITEMS[1], ITEMS[2]])  # aaa-1 was deleted
    assert p.query == "w"
    assert [it[0][1] for it in p.shown] == ["bbb-2", "ccc-3"]
    assert p.selected()[0][1] == "ccc-3"


def test_reload_drops_a_tool_filter_whose_tool_is_gone(monkeypatch):
    p = picker(monkeypatch)
    p.key("\t", 1 if False else 10)  # claude
    p.reload([ITEMS[1], ITEMS[2]])
    assert p.tool is None and len(p.shown) == 2


def test_the_picker_counts_the_rows_sharing_a_topic(monkeypatch):
    """A topic is the sessions joined by a continuation, not alike titles;
    a filter that narrows to one session of it leaves no topic to name.
    The topic folds into its newest session, which carries the count."""
    p = picker(monkeypatch, {"ddd-4": [("codex", "bbb-2")]})
    assert p.topic_count == {}
    relink(p, list(ITEMS) + [
        (row("claude", "ddd-4", "/w/other", "fix the nfc lock"), {"id": "ddd-4"}),
        (row("claude", "eee-5", "/w/other", "fix the NFC lock"), {"id": "eee-5"})])  # no link
    assert p.topic_count == {"bbb-2": 2}
    assert [it[0][1] for it in p.shown] == ["aaa-1", "bbb-2", "ccc-3", "eee-5"]
    p.query = "ddd-4"  # the id: one row, so no repetition to name
    p.refilter()
    assert p.topic_count == {}


def _topic_picker(monkeypatch):
    p = picker(monkeypatch, {"dev-3": [("claude", "dev-2")], "dev-2": [("claude", "dev-1")]})
    relink(p, [
        ITEMS[0],
        (row("kimi", "dev-3", "/w/api", "(handoff) DevGetter 上架"), {"id": "dev-3"}),
        ITEMS[1],
        (row("claude", "dev-2", "/w/api", "(handoff) DevGetter 上架"), {"id": "dev-2"}),
        (row("claude", "dev-1", "/w/api", "DevGetter 上架"), {"id": "dev-1"}),
    ])
    return p


def test_a_topic_is_one_row_until_unfolded(monkeypatch):
    """Nine sessions on one request were nine rows; now they are one, and
    → lays them out under it, newest first."""
    p = _topic_picker(monkeypatch)
    assert [it[0][1] for it in p.shown] == ["aaa-1", "dev-3", "bbb-2"]
    assert p.topic_count == {"dev-3": 3}
    p.cursor = 1
    p.key("KEY_RIGHT", 10)
    assert [it[0][1] for it in p.shown] == ["aaa-1", "dev-3", "dev-2", "dev-1", "bbb-2"]
    assert p.kind == ["row", "head", "member", "member", "row"]
    # Enter on a member resumes that older session, not the newest
    p.cursor = 3
    assert p.key("\n", 10) == "resume" and p.selected()[0][1] == "dev-1"
    # ← from a member folds the topic and lands on its row
    p.key("KEY_LEFT", 10)
    assert [it[0][1] for it in p.shown] == ["aaa-1", "dev-3", "bbb-2"]
    assert p.cursor == 1


def test_space_toggles_and_a_plain_row_ignores_it(monkeypatch):
    p = _topic_picker(monkeypatch)
    p.cursor = 1
    p.key(" ", 10)
    assert len(p.shown) == 5
    p.key(" ", 10)
    assert len(p.shown) == 3
    p.cursor = 0  # not a topic
    p.key("KEY_RIGHT", 10)
    assert len(p.shown) == 3


def test_a_topic_counts_its_turns_across_sessions(monkeypatch):
    p = _topic_picker(monkeypatch)
    turns = {"dev-3": 3, "dev-2": 117, "dev-1": None}
    monkeypatch.setattr(ui.sessions, "session_turns", lambda rec: turns.get(rec["id"]))
    assert p.topic_turns("dev-3") == 120


def _fake_curses(monkeypatch, actions):
    """Stand in for curses: cmd_ui only calls curses.wrapper, and stock
    Windows Python has no curses at all, so these tests must not need it."""
    fake = types.SimpleNamespace(wrapper=lambda *args: next(actions))
    monkeypatch.setitem(sys.modules, "curses", fake)


def test_view_returns_to_picker_without_pause(monkeypatch):
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(ui, "gather", lambda: list(ITEMS))
    monkeypatch.setattr(ui.note, "noted_index", lambda: {})
    monkeypatch.setattr(ui.note, "topic_noted_index", lambda: [])
    monkeypatch.setattr(ui.sessions, "session_parents", lambda record: [])
    actions = iter(["show", None])
    _fake_curses(monkeypatch, actions)
    calls = []
    monkeypatch.setattr(ui.show, "cmd_show", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(ui, "pause", lambda: (_ for _ in ()).throw(AssertionError("extra pause")))
    assert ui.cmd_ui([]) == 0
    assert calls == [["claude", "aaa-1"]]


def test_zcode_without_tui_enters_chat(monkeypatch):
    monkeypatch.setattr(ui.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(ui.sys.stdout, 'isatty', lambda: True)
    items = [(row('zcode', 'sess_saved', '/w', 'saved'), {'id': 'sess_saved'})]
    monkeypatch.setattr(ui, 'gather', lambda: items)
    monkeypatch.setattr(ui.note, 'noted_index', lambda: {})
    monkeypatch.setattr(ui.note, 'topic_noted_index', lambda: [])
    monkeypatch.setattr(ui.sessions, 'session_parents', lambda record: [])
    monkeypatch.setattr(ui.sessions, 'zcode_cli_on_path', lambda: True)
    monkeypatch.setattr(ui.sessions, 'zcode_tui_known_missing', lambda: True)
    actions = iter(['resume', None])
    _fake_curses(monkeypatch, actions)
    calls = []
    monkeypatch.setattr(ui, 'run_cw_command', lambda args: calls.append(args) or 0)
    monkeypatch.setattr(ui, 'pause', lambda: (_ for _ in ()).throw(AssertionError('extra pause')))
    assert ui.cmd_ui([]) == 0
    assert calls[0][-3:] == ['chat', 'zcode', 'sess_saved']
