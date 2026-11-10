"""A topic's note: every session of one topic distilled into one note.

Nine sessions on one request had a note each for three of them, and the
three newest notes -- all of that one topic, each its own slice -- were
what a new session started from. A topic note states where the topic
stands across all its sessions; the new session starts from it, and the
session notes it covers give their slots to other topics.
"""

import subprocess
import time
from types import SimpleNamespace

import pytest

from crossweave import cli, note, rename, search, sessions

TOPIC_REPLY = ("## 现状\n- vc8 已发布到内部测试轨\n\n## 决定\n- versionCode 从 50 起\n\n"
               "## 还剩什么\n- 在设备上做真实升级验证\n\n## 经过\n- 10-27 claude：上架流程")


def _rec(sid, tool, title, age_h):
    return {"tool": tool, "id": sid, "title": title, "cwd": "/w/ApiService",
            "ts": time.time() - age_h * 3600, "path": None}


@pytest.fixture
def world(monkeypatch, tmp_path):
    recs = {
        "k3": _rec("k3", "kimi", "(handoff) DevGetter 上架", 72),
        "c2": _rec("c2", "claude", "(handoff) DevGetter 上架", 96),
        "c1": _rec("c1", "claude", "DevGetter 上架", 120),
        "o1": _rec("o1", "codex", "other work", 80),
    }
    monkeypatch.setattr(sessions, "_find_record", lambda tool, sid: dict(recs[sid]) if sid in recs else None)
    monkeypatch.setattr(sessions, "_resolve_title_and_cwd", lambda r: (r["title"], r["cwd"]))
    monkeypatch.setattr(sessions, "session_turns", lambda r: 10)
    # the directory's other sessions, found when the topic is noted
    monkeypatch.setattr(sessions, "_all_light_records", lambda: [dict(r) for r in recs.values()])
    monkeypatch.setattr(sessions, "resolve_row",
                        lambda r: (r["tool"], r["id"], "", r["id"], r["cwd"], r["title"]))
    monkeypatch.setattr(sessions, "is_tool_started_row", lambda row: False)
    # the topic is its links: k3 continues c2, which continues c1
    links = {"k3": [("claude", "c2")], "c2": [("claude", "c1")]}
    monkeypatch.setattr(sessions, "session_parents", lambda r: links.get(r["id"], []))
    monkeypatch.setattr(rename, "_digest_for", lambda r: f"User asked: {r['id']} work")
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    sessions.write_list_cache([
        {"tool": "kimi", "id": "k3", "cwd": "/w/ApiService", "title": "(handoff) DevGetter 上架",
         "topic_titles": ["(handoff) DevGetter 上架"] * 2 + ["DevGetter 上架"],
         "topic_members": [{"tool": "kimi", "id": "k3"}, {"tool": "claude", "id": "c2"},
                           {"tool": "claude", "id": "c1"}]},
        {"tool": "codex", "id": "o1", "cwd": "/w/ApiService", "title": "other work"},
    ])
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("CW_VAULT", str(vault))
    w = SimpleNamespace(vault=vault, calls=[], reply=TOPIC_REPLY, recs=recs, links=links)

    def judge(name, prompt):
        w.calls.append(prompt)
        return subprocess.CompletedProcess([name], 0, stdout=w.reply, stderr="")

    monkeypatch.setattr(search, "_call_judge", judge)
    return w


def _write_session_note(vault, sid, title, updated, body="## 还剩什么\n- old open item"):
    folder = vault / "ApiService"
    folder.mkdir(exist_ok=True)
    (folder / f"{updated[:10]} {sid}.md").write_text(
        f'---\ntitle: "{title}"\ntool: "claude"\nsession: "{sid}"\n'
        f'updated: {updated}\nnoted: {updated}\n---\n# {title}\n\n{body}\n', encoding="utf-8")


def test_a_folded_row_notes_its_whole_topic(world, capsys):
    assert note.cmd_note(["1"]) == 0
    path = world.vault / "ApiService" / "DevGetter 上架.md"
    text = path.read_text(encoding="utf-8")
    assert 'topic_key: "c1"' in text  # its first session: the one the rest continue
    assert 'sessions: ["k3", "c2", "c1"]' in text
    assert 'resume: "cw resume kimi k3"' in text
    assert "# DevGetter 上架\n" in text and "## 现状" in text
    # one judge call over all three sessions, oldest first
    assert len(world.calls) == 1
    prompt = world.calls[0]
    assert prompt.index("c1 work") < prompt.index("c2 work") < prompt.index("k3 work")
    assert "noted topic (3 sessions)" in capsys.readouterr().out


def test_noting_the_topic_again_rewrites_its_note(world):
    note.cmd_note(["1"])
    world.reply = TOPIC_REPLY.replace("vc8", "vc9")
    note.cmd_note(["1"])
    files = [p.name for p in (world.vault / "ApiService").glob("*.md") if p.name != note.INDEX_NAME]
    assert files == ["DevGetter 上架.md"]
    assert "vc9" in (world.vault / "ApiService" / "DevGetter 上架.md").read_text(encoding="utf-8")


def test_session_flag_and_a_plain_row_note_one_session(world, monkeypatch):
    seen = []
    monkeypatch.setattr(note, "_note_one", lambda r, *a, **k: seen.append(r["id"]) or None)
    monkeypatch.setattr(note, "_resolve", lambda t, cache: (dict(world.recs[cache[int(t) - 1]["id"]]), None))
    note.cmd_note(["1", "--session"])
    note.cmd_note(["2"])
    assert seen == ["k3", "o1"]


def test_a_session_note_is_read_instead_of_its_digest(world):
    _write_session_note(world.vault, "c2", "(handoff) DevGetter 上架",
                        time.strftime("%Y-%m-%dT%H:%M"), body="## 做了什么\n- 上传 vc8")
    note.cmd_note(["1"])
    prompt = world.calls[0]
    assert "Earlier note: [做了什么] 上传 vc8" in prompt
    assert "c2 work" not in prompt


def test_context_starts_from_the_topic_and_drops_what_it_covers(world):
    _write_session_note(world.vault, "c2", "(handoff) DevGetter 上架", "2026-11-06T10:00")
    _write_session_note(world.vault, "c1", "DevGetter 上架", "2026-10-27T10:00")
    _write_session_note(world.vault, "o1", "other work", "2026-10-20T10:00")
    note.cmd_note(["1"])
    notes = note.context_notes(str(world.vault), "ApiService", 3)
    assert [m.get("topic_key") or m["session"] for m, _b in notes] == ["c1", "o1"]
    text = note.render_context("ApiService", notes)
    assert "## DevGetter 上架 (topic, 3 sessions," in text


def test_the_topic_note_is_not_a_session_note(world):
    note.cmd_note(["1"])
    assert note.noted_index(str(world.vault)) == {}
    assert note.session_notes(str(world.vault)) == {}
    index = note.topic_noted_index(str(world.vault))
    assert len(index) == 1 and index[0][1] == {"k3", "c2", "c1"}
    # found by the sessions it shares, whatever the topic is called now
    assert note.topic_note_for(index, ["k4", "k3"]) == index[0]
    assert note.topic_note_for(index, ["o1"]) is None


def test_the_topic_note_falls_behind(world):
    note.cmd_note(["1"])
    got = note.topic_note_for(note.topic_noted_index(str(world.vault)), ["k3"])
    members = [world.recs["k3"], world.recs["c2"], world.recs["c1"]]
    assert not note.topic_note_is_stale(members, got)
    # a session joined the topic after the note
    assert note.topic_note_is_stale(members + [_rec("k4", "kimi", "DevGetter 上架", 1)], got)
    # or one of its sessions moved on
    moved = dict(world.recs["k3"], ts=time.time() + 3600)
    assert note.topic_note_is_stale([moved], got)


def test_the_index_lists_topics_and_takes_their_open_items(world):
    _write_session_note(world.vault, "c2", "(handoff) DevGetter 上架", "2026-11-06T10:00")
    note.cmd_note(["1"])
    index = (world.vault / "ApiService" / note.INDEX_NAME).read_text(encoding="utf-8")
    assert "## Topics" in index and "[[DevGetter 上架]] -- 3 sessions" in index
    assert "在设备上做真实升级验证" in index
    assert "old open item" not in index  # the covered session's older open item


def test_the_hint_points_at_the_folded_rows_number(world, monkeypatch, tmp_path, capsys):
    here = tmp_path / "ApiService"
    here.mkdir()
    sessions.write_list_cache([
        {"tool": "codex", "id": "o" * 32, "cwd": str(here), "title": "other work"},
        {"tool": "kimi", "id": "k" * 32, "cwd": str(here), "title": "(handoff) DevGetter",
         "topic_titles": ["(handoff) DevGetter"] * 3,
         "topic_members": [{"tool": "kimi", "id": "k" * 32}] * 3},
    ])
    monkeypatch.chdir(here)
    cli.steer_hint()
    assert "distill: `cw note 2`" in capsys.readouterr().err


def test_the_topic_takes_in_the_sessions_the_listing_did_not_walk(world):
    """A listing cached before c0 joined the topic still notes all of it;
    a same-titled session with no link stays out."""
    world.recs["c0"] = _rec("c0", "claude", "DevGetter 上架", 400)
    world.links["c1"] = [("claude", "c0")]
    world.recs["x9"] = _rec("x9", "claude", "DevGetter 上架", 500)
    note.cmd_note(["1"])
    text = (world.vault / "ApiService" / "DevGetter 上架.md").read_text(encoding="utf-8")
    assert 'sessions: ["k3", "c2", "c1", "c0"]' in text
    assert world.calls[0].index("c0 work") < world.calls[0].index("c1 work")
