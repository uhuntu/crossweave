"""Topics: the sessions the user carried on from one another.

A request that keeps coming back used to be nine unrelated rows. A topic
is now what the user's own continuations join -- a `cw handoff`
(`cw 1 codex`), a Claude /clear in the same directory, a codex fork --
never two titles that happen to read alike. These cover the links, the
grouping, the folded listing, `cw full`'s marks, the cache fields and the
launch-time hint.
"""

import json
import re

import pytest

from crossweave import cli, codex, render, sessions


def _row(tool, sid, title, cwd="/work", when="2h ago"):
    return (tool, sid, when, sid[:8], cwd, title)


def _parents(links):
    """A session_parents stand-in: {id: [(tool, id), ...]}."""
    return lambda r: links.get(r["id"], [])


# ---------- the links ----------

def _claude_file(folder, sid, prior=None):
    lines = []
    if prior:
        lines.append({"type": "user", "message": {"role": "user", "content": (
            "<system-reminder> The user started this conversation instead of resuming an "
            f"earlier, inactive one (session {prior}), so its history was not re-sent.")}})
    lines.append({"type": "user", "message": {"role": "user", "content": "next step"}})
    path = folder / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(d) + "\n" for d in lines), encoding="utf-8")
    return path


A = "722b3e55-faef-43fc-8660-e60660c8e183"
B = "74b2ff9f-28eb-410f-9c81-65b69b2ea08b"


def test_a_claude_clear_continues_the_session_it_names(tmp_path):
    _claude_file(tmp_path, A)
    path = _claude_file(tmp_path, B, prior=A)
    assert sessions.claude_continues(str(path)) == A
    assert sessions.session_parents({"tool": "claude", "id": B, "path": str(path)}) == [("claude", A)]


def test_a_clear_into_another_directory_is_not_a_continuation(tmp_path):
    """TesSmartUpdater after DevGetter: the /clear was to start something
    else, somewhere else. Claude files each directory's sessions apart."""
    (tmp_path / "other").mkdir()
    _claude_file(tmp_path / "other", A)
    here = tmp_path / "here"
    here.mkdir()
    path = _claude_file(here, B, prior=A)
    assert sessions.session_parents({"tool": "claude", "id": B, "path": str(path)}) == []


def test_a_codex_subagent_is_not_the_users_fork(monkeypatch):
    monkeypatch.setattr(codex, "codex_parent_thread_id", lambda sid: "parent")
    monkeypatch.setattr(codex, "codex_rollout_path", lambda sid: "/x")
    monkeypatch.setattr(codex, "_codex_session_meta", lambda path: {
        "payload": {"source": {"subagent": {"other": "guardian"}}}})
    assert codex.codex_fork_parent("child") is None
    monkeypatch.setattr(codex, "_codex_session_meta", lambda path: {"payload": {"source": "cli"}})
    assert codex.codex_fork_parent("child") == "parent"


def test_link_groups_joins_chains_branches_and_unseen_parents():
    records = [{"tool": "kimi", "id": "k2"}, {"tool": "claude", "id": "c2"},
               {"tool": "step", "id": "s1"}, {"tool": "kimi", "id": "k1"},
               {"tool": "codex", "id": "lone"}, {"tool": "claude", "id": "c1"}]
    group = sessions.link_groups(records, _parents({
        "k2": [("claude", "c2")], "c2": [("claude", "c1")],   # a chain
        "k1": [("claude", "c1")],                              # a branch off c1
        "s1": [("zcode", "never-listed")],                     # parent not in records
    }))
    g = lambda sid, tool: group[(tool, sid)]  # noqa: E731
    assert g("k2", "kimi") == g("c2", "claude") == g("c1", "claude") == g("k1", "kimi")
    assert g("s1", "step") != g("k2", "kimi")
    assert g("lone", "codex") not in {g("k2", "kimi"), g("s1", "step")}


def test_alike_titles_with_no_link_are_two_topics():
    """Two sessions both titled "cw 会话列表", fourteen days apart."""
    records = [{"tool": "claude", "id": "a"}, {"tool": "claude", "id": "b"}]
    group = sessions.link_groups(records, _parents({}))
    assert group[("claude", "a")] != group[("claude", "b")]


@pytest.mark.parametrize("title", ["中文", "用中文", "In English", "chinese.", "好的", "继续",
                                   "Session greeting", "Try again"])
def test_an_opener_that_says_nothing_is_not_a_title(title):
    """Typed before the question it was about: seven unrelated sessions in
    ~ were titled "中文"; the title now moves on to the real request."""
    assert sessions.is_trivial_title(title)


# ---------- the cache carries the display facts ----------

def test_list_cache_entry_keeps_tool_and_id_for_resume():
    assert sessions.list_cache_entry(_row("claude", "c1" * 20, "t")) == {
        "tool": "claude", "id": "c1" * 20, "when": "2h ago",
        "cwd": "/work", "title": "t",
    }


def test_list_cache_entry_tolerates_a_bare_tuple():
    """A search row can be shorter than a listing row; the entry still has
    the two fields `cw resume <N>` indexes into."""
    assert sessions.list_cache_entry(("codex", "abc", "1h ago", "abc")) == {
        "tool": "codex", "id": "abc",
    }


def test_a_folded_rows_entry_lists_its_topic():
    members = [_row("kimi", "k1", "t"), _row("claude", "c1", "")]
    assert sessions.list_cache_entry(members[0], members)["topic_members"] == [
        {"tool": "kimi", "id": "k1"}, {"tool": "claude", "id": "c1"}]


# ---------- cw full: every session its own row, topics marked ----------

def _listing(monkeypatch, capsys, rows, links, sources=None):
    if sources is None:
        sources = {r[1]: {"tool": r[0], "id": r[1], "ts": 0} for r in rows}
    monkeypatch.setattr(render.color, "_STATE", False)
    monkeypatch.setattr(sessions, "session_parents", _parents(links))
    render.render_rows(rows, write_cache=False, sources=sources)
    return capsys.readouterr().out.splitlines()


def _row_lines(out):
    return [l for l in out if re.match(r"^\s*\d+\s", l)]


def test_cw_full_marks_and_names_the_topics(monkeypatch, capsys):
    rows = [
        _row("kimi", "k9", "(handoff) DevGetter 上架与 MDM"),
        _row("codex", "x1", "DevGetter 上架与 MDM"),  # same title, no link
        _row("claude", "c3", "(handoff) DevGetter 上架与 MDM"),
        _row("claude", "c4", "DevGetter 上架与 MDM"),
    ]
    out = _listing(monkeypatch, capsys, rows, {"k9": [("claude", "c3")], "c3": [("claude", "c4")]})
    lines = _row_lines(out)
    assert "⧉ 3" in lines[0] and "⧉ 3" in lines[2] and "⧉ 3" in lines[3]
    assert "⧉" not in lines[1]
    summary = next(l for l in out if " sessions continue " in l)
    assert "3 sessions continue 'DevGetter 上架与 MDM'" in summary
    assert "newest row #1" in summary


def test_the_summary_lines_stop_at_five_and_count_the_rest(monkeypatch, capsys):
    rows, links = [], {}
    for n in range(render.TOPIC_SUMMARY_MAX + 2):
        rows += [_row("claude", f"c{n}", f"topic {n}"), _row("codex", f"x{n}", f"topic {n}")]
        links[f"c{n}"] = [("codex", f"x{n}")]
    out = _listing(monkeypatch, capsys, rows, links)
    assert len([l for l in out if " sessions continue " in l]) == render.TOPIC_SUMMARY_MAX
    assert any("and 2 more topics" in l for l in out)
    assert sum(l.count("⧉ 2") for l in _row_lines(out)) == len(rows)


def test_search_rows_are_not_grouped(monkeypatch, capsys):
    rows = [_row("claude", "c1", "nfc lock"), _row("codex", "x1", "NFC Lock")]
    monkeypatch.setattr(render.color, "_STATE", False)
    render.render_rows(rows, write_cache=False, sources=None)
    assert not any(" sessions continue " in l for l in capsys.readouterr().out.splitlines())


# ---------- a bare `cw`: one row per topic ----------

def _rec(sid, title, ts, turns, when, tool="claude"):
    return {"tool": tool, "id": sid, "ts": ts, "title": title, "turns": turns, "when": when}


DEV = [
    _rec("d3", "(handoff) DevGetter", 90, 3, "3d ago", "kimi"),
    _rec("o1", "other work", 80, 5, "3d ago"),
    _rec("d2", "(handoff) DevGetter", 70, 117, "4d ago"),
    _rec("o2", "more work", 50, 2, "6d ago"),
    _rec("d1", "DevGetter", 40, 164, "9d ago"),
]
DEV_LINKS = {"d3": [("claude", "d2")], "d2": [("claude", "d1")]}


def _fold_listing(monkeypatch, tmp_path, records, links, limit="15"):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    monkeypatch.setattr(render.color, "_STATE", False)
    for name in ("codex_light_records",):
        monkeypatch.setattr(sessions, name, lambda: [])
    for name in ("kimi_light_records", "step_light_records", "pi_light_records",
                 "opencode_light_records", "zcode_light_records",
                 "codebuddy_light_records", "workbuddy_light_records"):
        monkeypatch.setattr(sessions, name, lambda show_all=False: [])
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [dict(r) for r in records])
    resolved = []

    def resolve(r):
        resolved.append(r["id"])
        return (r["tool"], r["id"], r["when"], r["id"][:8], "/work", r["title"])

    monkeypatch.setattr(sessions, "resolve_row", resolve)
    monkeypatch.setattr(sessions, "relative_time", lambda ts: {r["ts"]: r["when"] for r in records}[ts])
    monkeypatch.setattr(sessions, "session_parents", _parents(links))
    monkeypatch.setattr(sessions, "session_turns", lambda r: r.get("turns"))
    monkeypatch.setattr(sessions, "session_last_message", lambda r: None)
    monkeypatch.setattr(sessions, "claude_is_empty", lambda p: False)
    monkeypatch.setattr(sessions, "is_tool_started_row", lambda row: False)
    sessions.cmd_list(["--limit", limit])
    return resolved


def test_a_bare_listing_folds_a_topic_into_its_newest_row(monkeypatch, tmp_path, capsys):
    resolved = _fold_listing(monkeypatch, tmp_path, DEV, DEV_LINKS)
    rows = _row_lines(capsys.readouterr().out.splitlines())
    assert len(rows) == 3
    assert "DevGetter ⧉ 3 · 284 turns · since 9d ago" in rows[0]
    assert "(handoff)" not in rows[0]  # the row is the topic
    assert "d1" not in resolved  # an older session's title is never read


def test_the_count_is_the_whole_historys(monkeypatch, tmp_path, capsys):
    """d1 lies past the rows a limit of 2 fills, and is still counted."""
    _fold_listing(monkeypatch, tmp_path, DEV, DEV_LINKS, limit="2")
    rows = _row_lines(capsys.readouterr().out.splitlines())
    assert [r.split()[4] for r in rows] == ["d3", "o1"]
    assert "⧉ 3" in rows[0]
    entry = sessions.read_list_cache()[0]
    assert [m["id"] for m in entry["topic_members"]] == ["d3", "d2", "d1"]


def test_cw_full_still_lists_every_session(monkeypatch, tmp_path, capsys):
    _fold_listing(monkeypatch, tmp_path, DEV, DEV_LINKS, limit="all")
    assert len(_row_lines(capsys.readouterr().out.splitlines())) == 5


# ---------- the steer hint ----------

@pytest.fixture
def cached_listing(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    return sessions


def _hint(monkeypatch, cwd, capsys):
    monkeypatch.chdir(cwd)
    cli.steer_hint()
    return capsys.readouterr().err


def _topic_entry(here, n, tool="kimi", sid="k" * 32, title="(handoff) DevGetter", **extra):
    return {"tool": tool, "id": sid, "cwd": str(here), "title": title,
            "topic_members": [{"tool": tool, "id": f"{sid}{i}"} for i in range(n)], **extra}


def test_the_hint_names_a_topic_that_keeps_going_here(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        {"tool": "codex", "id": "o" * 32, "cwd": str(here), "title": "other work"},
        _topic_entry(here, 3, when="8m ago"),
        _topic_entry(tmp_path / "elsewhere", 5, sid="x" * 32),
    ])
    out = _hint(monkeypatch, here, capsys)
    assert "3 sessions here continue 'DevGetter', newest 8m ago" in out
    assert "cw resume kimi " + "k" * sessions.ID_SHOW + "`" in out
    assert "distill: `cw note 2`" in out  # the folded row's number notes the topic
    assert "x" * 8 not in out


def test_the_hint_says_nothing_about_one_handoff(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([_topic_entry(here, 2),
                                     {"tool": "kimi", "id": "z" * 32, "cwd": str(here), "title": "x"}])
    assert _hint(monkeypatch, here, capsys) == ""


def test_the_hint_is_silenced_with_the_context(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([_topic_entry(here, 4)])
    monkeypatch.setenv("CW_CONTEXT", "off")
    assert _hint(monkeypatch, here, capsys) == ""


def test_the_hint_skips_a_machine_row(cached_listing, monkeypatch, tmp_path, capsys):
    """A row pulled from another machine has no cwd of this one's to
    compare, and a remote session cannot be resumed by its row here."""
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([_topic_entry(here, 4, machine="kestrel")])
    assert _hint(monkeypatch, here, capsys) == ""


# ---------- what counts as starting a session ----------

@pytest.mark.parametrize("tool,rest", [
    ("claude", []),
    ("codex", ["fix the lock"]),  # one argument: a prompt, not a subcommand
    ("kimi", ["--", "-p"]),  # the tool's own flags after `--`
    ("claude", ["--model", "sonnet", "hi"]),
])
def test_starts_session_is_true_for_a_fresh_session(tool, rest):
    assert cli.starts_session(tool, rest)


@pytest.mark.parametrize("tool,rest", [
    ("claude", ["doctor"]),        # a subcommand, not a prompt
    ("claude", ["-p", "one shot"]),  # a print run
    ("kimi", ["-p"]),
    ("zcode", []),                 # opens the app, starts no session
    ("workbuddy", []),
    ("nosuchtool", []),
])
def test_starts_session_is_false_for_everything_else(tool, rest):
    assert not cli.starts_session(tool, rest)


def test_the_hint_gives_a_kimi_id_that_resumes(cached_listing, monkeypatch, tmp_path, capsys):
    """Eight characters of a kimi id are "session_" -- every one of them."""
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        _topic_entry(here, 3, sid="session_80a9a7ce-2fd4-4925-a723-1decb313393d")])
    assert "`cw resume kimi session_80a9a7ce-2fd`" in _hint(monkeypatch, here, capsys)
