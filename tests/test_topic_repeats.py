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
    assert "⧉3" in row_lines[0] and "⧉3" in row_lines[2] and "⧉3" in row_lines[3]
    assert "⧉" not in row_lines[1]
    # One summary line, under the header: the count leads, and it points at
    # the newest member's row number rather than a range -- the members are
    # not contiguous.
    summary = next(l for l in out if "sessions share" in l)
    assert "3 sessions share 'DevGetter 上架与 MDM'" in summary
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

    summaries = [l for l in out if "sessions share" in l]
    assert len(summaries) == render.TOPIC_SUMMARY_MAX
    assert any(f"and {len(rows) // 2 - render.TOPIC_SUMMARY_MAX} more repeated topics" in l
               for l in out)
    assert sum(l.count("⧉2") for l in out) == len(rows)  # every row keeps its mark


def test_search_rows_are_not_clustered(monkeypatch, capsys):
    """Search hits share a line with a reason; there is no room for a mark,
    and the cluster summary would read as a ranking rather than a repeat."""
    rows = [_row("claude", "c1", "nfc lock"), _row("codex", "x1", "NFC Lock")]
    out = _listing(monkeypatch, capsys, rows, sources=None)
    assert not any("sessions share" in l for l in out)


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
    assert not any("sessions share" in l for l in out)
    assert sum(l.count("⧉2") for l in out) == 2


def test_the_hint_says_nothing_about_a_single_handoff(cached_listing, monkeypatch, tmp_path, capsys):
    here = tmp_path / "app"
    here.mkdir()
    cached_listing.write_list_cache([
        {"tool": "claude", "id": "c" * 32, "cwd": str(here), "title": "(handoff) Rock 5B"},
        {"tool": "codex", "id": "x" * 32, "cwd": str(here), "title": "Rock 5B"},
    ])
    assert _hint(monkeypatch, here, capsys) == ""
