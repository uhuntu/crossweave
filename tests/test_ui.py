"""cw ui: the picker's filter and key handling, without a terminal."""
from crossweave import ui
from crossweave.render import _display_width


def row(tool, sid, cwd, title):
    return (tool, sid, "1h ago", sid[:20], cwd, title)


ITEMS = [
    (row("claude", "aaa-1", "/w/crossweave", "Obsidian browser connection"), {"id": "aaa-1"}),
    (row("codex", "bbb-2", "/w/other", "fix the NFC lock"), {"id": "bbb-2"}),
    (row("kimi", "ccc-3", "/w/crossweave", "在共享盘上打开 vault"), {"id": "ccc-3"}),
]


def picker(monkeypatch):
    monkeypatch.setattr(ui.note, "noted_index", lambda: {})
    return ui.Picker(list(ITEMS), "/w/crossweave")


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
