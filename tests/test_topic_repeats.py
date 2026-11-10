"""The listing's answer to one topic reopened nine times.

A request that keeps coming back gets a fresh session every time, and the
listing used to show those as nine unrelated rows. These cover the
same-topic clustering (the `⧉N` mark and the summary line under the
header), the cache fields the launch-time hint reads, and the hint
itself.
"""

import re

import pytest

from crossweave import cli, render, sessions


def _row(tool, sid, title, cwd="/work", when="2h ago"):
    return (tool, sid, when, sid[:8], cwd, title)


# ---------- topic keys ----------

def test_a_handoff_and_its_source_share_a_topic():
    """`cw 3 codex` starts a session titled after the one it continues;
    nine sessions on one request is usually a handoff chain, not nine
    unrelated rows."""
    assert (sessions.topic_key("(handoff) DevGetter 上架")
            == sessions.topic_key("DevGetter 上架"))


def test_topic_keys_fold_case():
    assert sessions.topic_key("Fix The NFC Lock") == sessions.topic_key("fix the nfc lock")


@pytest.mark.parametrize("title", [
    None, "", "   ", "(no title)", "ok", "https://example.com/a",
])
def test_titles_that_say_nothing_cluster_nothing(title):
    """200 rows of `(no title)` are one absence of a topic, not a topic
    with 200 sessions."""
    assert sessions.topic_key(title) is None


# ---------- clustering ----------

def test_topic_clusters_needs_two_rows():
    rows = [_row("claude", "c1", "the nfc lock"), _row("codex", "x1", "something else")]
    assert sessions.topic_clusters(rows) == {}


def test_topic_clusters_groups_by_topic_and_keeps_listing_order():
    """The listing is newest first, so the first member of a cluster is the
    freshest take on the topic -- what the summary line and the steer hint
    both point at."""
    rows = [
        _row("kimi", "k9", "DevGetter 上架与 MDM"),
        _row("codex", "x1", "other work"),
        _row("claude", "c3", "devgetter 上架与 mdm"),
        _row("step", "s4", "DevGetter 上架与 MDM"),
    ]
    clusters = sessions.topic_clusters(rows)
    assert list(clusters) == ["devgetter 上架与 mdm"]
    members = clusters["devgetter 上架与 mdm"]
    assert [r[1] for r in members] == ["k9", "c3", "s4"]


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


# ---------- the listing ----------

_NO_SOURCES = object()


def _listing(monkeypatch, capsys, rows, sources=_NO_SOURCES):
    if sources is _NO_SOURCES:
        sources = {r[1]: {"tool": r[0], "id": r[1], "ts": 0} for r in rows}
    monkeypatch.setattr(render.color, "_STATE", False)
    render.render_rows(rows, write_cache=False, sources=sources)
    return capsys.readouterr().out.splitlines()


def test_the_listing_marks_and_summarizes_repeated_topics(monkeypatch, capsys):
    rows = [
        _row("kimi", "k9", "DevGetter 上架与 MDM"),
        _row("codex", "x1", "other work"),
        _row("claude", "c3", "devgetter 上架与 mdm"),
        _row("claude", "c4", "DevGetter 上架与 MDM"),
    ]
    out = _listing(monkeypatch, capsys, rows)

    row_lines = [l for l in out if re.match(r"^\s*\d+\s", l)]
    assert "⧉ 3" in row_lines[0] and "⧉ 3" in row_lines[2] and "⧉ 3" in row_lines[3]
    assert "⧉" not in row_lines[1]
    # One summary line, under the header: the count leads, and it points at
    # the newest member's row number rather than a range -- the members are
    # not contiguous.
    summary = next(l for l in out if " shown share " in l)
    # "of 4 shown": the count is the screen's, not the history's.
    assert "3 of 4 shown share 'DevGetter 上架与 MDM'" in summary
    assert "cw full" not in summary  # nothing was left out
    assert "newest row #1" in summary


def test_the_summary_lines_stop_at_five_and_count_the_rest(monkeypatch, capsys):
    """`cw full` can hold a cluster for every topic anyone has reopened;
    a preamble longer than the first screen of rows is the table's own
    worst enemy. The rows keep their marks either way."""
    rows = []
    for n in range(render.TOPIC_SUMMARY_MAX + 2):
        rows.append(_row("claude", f"c{n}", f"topic {n}"))
        rows.append(_row("codex", f"x{n}", f"Topic {n}"))
    out = _listing(monkeypatch, capsys, rows)

    summaries = [l for l in out if " shown share " in l]
    assert len(summaries) == render.TOPIC_SUMMARY_MAX
    assert any(f"and {len(rows) // 2 - render.TOPIC_SUMMARY_MAX} more repeated topics" in l
               for l in out)
    row_lines = [l for l in out if re.match(r"^\s*\d+\s", l)]
    assert sum(l.count("⧉ 2") for l in row_lines) == len(rows)  # every row keeps its mark


def test_search_rows_are_not_clustered(monkeypatch, capsys):
    """Search hits share a line with a reason; there is no room for a mark,
    and the cluster summary would read as a ranking rather than a repeat."""
    rows = [_row("claude", "c1", "nfc lock"), _row("codex", "x1", "NFC Lock")]
    out = _listing(monkeypatch, capsys, rows, sources=None)
    assert not any(" shown share " in l for l in out)


# ---------- the steer hint ----------

@pytest.fixture
def cached_listing(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    return sessions


def _hint(monkeypatch, cwd, capsys):
    monkeypatch.chdir(cwd)
    cli.steer_hint()
    return capsys.readouterr().err


def test_the_hint_names_the_repeat_and_points_at_both_ways(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        {"tool": "kimi", "id": "k" * 32, "cwd": str(here), "title": "DevGetter", "when": "8m ago"},
        {"tool": "claude", "id": "c" * 32, "cwd": str(here), "title": "devgetter", "when": "2h ago"},
        {"tool": "codex", "id": "x" * 32, "cwd": str(tmp_path / "elsewhere"), "title": "DevGetter"},
    ])
    out = _hint(monkeypatch, here, capsys)

    assert "2 sessions here on 'DevGetter'" in out
    assert "cw resume kimi " + "k" * 8 in out
    assert "cw note " + "k" * 8 in out
    assert "elsewhere" not in out


def test_the_hint_says_nothing_about_a_topic_opened_once(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        {"tool": "kimi", "id": "k" * 32, "cwd": str(here), "title": "DevGetter"},
        {"tool": "kimi", "id": "k2" + "k" * 30, "cwd": str(here), "title": "other work"},
    ])
    assert _hint(monkeypatch, here, capsys) == ""


def test_the_hint_is_silenced_with_the_context(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        {"tool": "kimi", "id": "k" * 32, "cwd": str(here), "title": "DevGetter"},
        {"tool": "claude", "id": "c" * 32, "cwd": str(here), "title": "devgetter"},
    ])
    monkeypatch.setenv("CW_CONTEXT", "off")
    assert _hint(monkeypatch, here, capsys) == ""


def test_the_hint_skips_a_machine_row(cached_listing, monkeypatch, tmp_path, capsys):
    """A row pulled from another machine has no cwd of this one's to
    compare, and a remote session cannot be resumed by its row here."""
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        {"tool": "kimi", "id": "k" * 32, "cwd": str(here), "title": "DevGetter"},
        {"tool": "claude", "id": "c" * 32, "cwd": str(here), "title": "devgetter",
         "machine": "kestrel"},
    ])
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


# ---------- one handoff is a continuation, not a repeat ----------

@pytest.mark.parametrize("titles, repeated", [
    (["(handoff) Rock 5B", "Rock 5B"], False),
    (["(fork) Rock 5B", "Rock 5B"], False),
    (["(handoff) Rock 5B", "(handoff) Rock 5B"], False),
    (["Rock 5B", "rock 5b"], True),
    (["(handoff) Rock 5B", "(handoff) Rock 5B", "(handoff) Rock 5B"], True),
])
def test_is_repeated_topic(titles, repeated):
    assert sessions.is_repeated_topic(titles) is repeated


def test_a_single_handoff_keeps_its_marks_but_gets_no_summary(monkeypatch, capsys):
    rows = [
        _row("claude", "c1", "(handoff) Rock 5B 蓝牙"),
        _row("codex", "x1", "other work"),
        _row("codex", "x2", "Rock 5B 蓝牙"),
    ]
    out = _listing(monkeypatch, capsys, rows)
    assert not any(" shown share " in l for l in out)
    assert sum(l.count("⧉ 2") for l in out) == 2


def test_the_hint_says_nothing_about_a_single_handoff(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        {"tool": "claude", "id": "c" * 32, "cwd": str(here), "title": "(handoff) Rock 5B"},
        {"tool": "codex", "id": "x" * 32, "cwd": str(here), "title": "Rock 5B"},
    ])
    assert _hint(monkeypatch, here, capsys) == ""


def test_a_cut_listing_points_the_first_summary_at_cw_full(monkeypatch, capsys):
    """A bare `cw` shows 15 rows; the topic it names may have more sessions
    further back (seen live: 4 on screen, 9 in the history)."""
    rows = [_row("claude", "c1", "topic a"), _row("codex", "x1", "Topic A"),
            _row("claude", "c2", "topic b"), _row("codex", "x2", "Topic B")]
    sources = {r[1]: {"tool": r[0], "id": r[1], "ts": 0} for r in rows}
    monkeypatch.setattr(render.color, "_STATE", False)
    render.render_rows(rows, write_cache=False, sources=sources, cut=True)
    summaries = [l for l in capsys.readouterr().out.splitlines() if " shown share " in l]
    assert len(summaries) == 2
    assert "`cw full` counts every session" in summaries[0]
    assert "cw full" not in summaries[1]


def test_a_batch_of_one_turn_sessions_gets_no_summary(monkeypatch, capsys):
    """Eleven one-turn probes ("只回复两个字：正常") or greeting-only
    sessions topped `cw full`; nobody reopened anything."""
    rows = [_row("zcode", "z1", "只回复两个字：正常"), _row("step", "s1", "只回复两个字：正常"),
            _row("zcode", "z2", "只回复两个字：正常")]
    monkeypatch.setattr(sessions, "session_turns", lambda record: 1)
    out = _listing(monkeypatch, capsys, rows)
    assert not any(" shown share " in l for l in out)
    assert sum(l.count("⧉ 3") for l in out) == 3  # the marks stay


def test_one_member_with_real_work_keeps_the_summary(monkeypatch, capsys):
    rows = [_row("claude", "c1", "nfc lock"), _row("codex", "x1", "NFC Lock")]
    monkeypatch.setattr(sessions, "session_turns", lambda record: 40 if record["id"] == "x1" else 1)
    out = _listing(monkeypatch, capsys, rows)
    assert any("2 of 2 shown share 'nfc lock' -- 41 turns" in l for l in out)


@pytest.mark.parametrize("title", ["中文", "用中文", "In English", "chinese.", "好的", "继续",
                                   "Session greeting", "Try again"])
def test_a_language_switch_is_not_a_topic(title):
    """Typed before the question it was about: seven unrelated sessions in
    ~ were titled "中文" and clustered as one topic."""
    assert sessions.topic_key(title) is None


# ---------- folding the listing ----------

def _fold_listing(monkeypatch, tmp_path, records, limit="15"):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    monkeypatch.setattr(render.color, "_STATE", False)
    monkeypatch.setattr(sessions, "_all_light_records", lambda: records, raising=False)
    for name in ("claude_light_records", "codex_light_records"):
        monkeypatch.setattr(sessions, name, lambda: [])
    for name in ("kimi_light_records", "step_light_records", "pi_light_records",
                 "opencode_light_records", "zcode_light_records",
                 "codebuddy_light_records", "workbuddy_light_records"):
        monkeypatch.setattr(sessions, name, lambda show_all=False: [])
    monkeypatch.setattr(sessions, "claude_light_records", lambda: records)
    monkeypatch.setattr(sessions, "resolve_row",
                        lambda r: (r["tool"], r["id"], r["when"], r["id"][:8], "/work", r["title"]))
    monkeypatch.setattr(sessions, "session_turns", lambda r: r.get("turns"))
    monkeypatch.setattr(sessions, "session_last_message", lambda r: None)
    monkeypatch.setattr(sessions, "claude_is_empty", lambda p: False)
    monkeypatch.setattr(sessions, "is_tool_started_row", lambda row: False)
    sessions.cmd_list(["--limit", limit])


def _rec(sid, title, ts, turns, when):
    return {"tool": "claude", "id": sid, "ts": ts, "title": title, "turns": turns, "when": when}


DEV = [
    _rec("d3", "(handoff) DevGetter", 90, 3, "3d ago"),
    _rec("o1", "other work", 80, 5, "3d ago"),
    _rec("d2", "(handoff) DevGetter", 70, 117, "4d ago"),
    _rec("d1", "DevGetter", 60, 164, "5d ago"),
    _rec("o2", "more work", 50, 2, "6d ago"),
]


def test_a_limited_listing_folds_a_topic_into_its_newest_row(monkeypatch, tmp_path, capsys):
    _fold_listing(monkeypatch, tmp_path, DEV)
    rows = [l for l in capsys.readouterr().out.splitlines() if re.match(r"^\s*\d+\s", l)]
    assert len(rows) == 3
    assert "DevGetter ⧉ 3 · 284 turns · since 5d ago" in rows[0]
    assert "(handoff)" not in rows[0]  # the row is the topic
    assert " shown share " not in "".join(rows)
    # the folded sessions do not eat the limit: o2 still made the cut
    _fold_listing(monkeypatch, tmp_path, DEV, limit="3")
    rows = [l for l in capsys.readouterr().out.splitlines() if re.match(r"^\s*\d+\s", l)]
    assert [r.split()[4] for r in rows] == ["d3", "o1", "o2"]


def test_cw_full_still_lists_every_session(monkeypatch, tmp_path, capsys):
    _fold_listing(monkeypatch, tmp_path, DEV, limit="all")
    rows = [l for l in capsys.readouterr().out.splitlines() if re.match(r"^\s*\d+\s", l)]
    assert len(rows) == 5


def test_the_hint_counts_a_folded_rows_sessions(monkeypatch, tmp_path, capsys):
    """The older sessions have no row of their own in the cache any more;
    the folded row carries their titles."""
    here = tmp_path / "app"
    here.mkdir()
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    sessions.write_list_cache([
        {"tool": "kimi", "id": "k" * 32, "cwd": str(here), "title": "(handoff) DevGetter",
         "when": "3d ago",
         "topic_titles": ["(handoff) DevGetter", "(handoff) DevGetter", "DevGetter"]},
    ])
    out = _hint(monkeypatch, here, capsys)
    assert "3 sessions here on 'DevGetter'" in out
    assert "cw resume kimi " + "k" * 8 in out
