"""Tests for the Kiro session reader (kiro.py). Each test builds a small
~/.kiro/sessions tree in a temp directory; nothing touches the real store."""
import calendar
import json

import pytest

from crossweave import kiro


def _session(root, ws, sid, *, title=None, cwd="/home/hunt/work/demo",
             created="2026-11-27T02:42:10.803Z", modified="2026-11-27T03:32:18.235Z",
             turns=(("user", "fix the parser"), ("assistant", "done")), raw_lines=()):
    sdir = root / ws / sid
    sdir.mkdir(parents=True)
    meta = {"id": sid, "createdAt": created, "lastModifiedAt": modified,
            "workspacePaths": [cwd] if cwd else []}
    if title is not None:
        meta["title"] = title
    (sdir / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    if turns or raw_lines:
        lines = []
        for i, (role, text) in enumerate(turns):
            lines.append(json.dumps({"id": f"m{i}", "timestamp": created,
                                     "payload": {"type": role, "content": text}}))
        lines.extend(raw_lines)
        (sdir / "messages.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sdir


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(kiro, "SESSIONS_DIR", str(root))
    return root


def test_lists_sessions_from_every_workspace_hash(store):
    _session(store, "aaaa", "sess_one", cwd="/home/hunt/work/one")
    _session(store, "bbbb", "sess_two", cwd="/home/hunt/work/two")
    recs = {r["id"]: r for r in kiro.kiro_light_records(show_all=False)}
    assert set(recs) == {"sess_one", "sess_two"}
    assert recs["sess_one"]["tool"] == "kiro"
    assert recs["sess_one"]["cwd"] == "/home/hunt/work/one"
    assert recs["sess_one"]["path"].endswith("sess_one")


def test_timestamps_are_epoch_seconds(store):
    _session(store, "aaaa", "sess_t", created="2026-11-27T02:42:10.803Z",
             modified="2026-11-27T03:32:18.235Z")
    rec = kiro.kiro_light_records(show_all=True)[0]
    assert rec["ts"] == calendar.timegm((2026, 11, 27, 3, 32, 18))
    assert rec["started"] == calendar.timegm((2026, 11, 27, 2, 42, 10))


def test_empty_session_is_not_listed(store):
    _session(store, "aaaa", "sess_empty", turns=())
    _session(store, "aaaa", "sess_assistant_only", turns=(("assistant", "hi"),))
    _session(store, "aaaa", "sess_real")
    assert [r["id"] for r in kiro.kiro_light_records(show_all=False)] == ["sess_real"]


def test_corrupt_lines_and_metadata_are_skipped(store):
    _session(store, "aaaa", "sess_messy", turns=(("user", "ok"),),
             raw_lines=["{not json", "", "[1, 2]", json.dumps({"payload": "str"})])
    bad = store / "aaaa" / "sess_badmeta"
    bad.mkdir()
    (bad / "session.json").write_text("{truncated", encoding="utf-8")
    (bad / "messages.jsonl").write_text(json.dumps(
        {"payload": {"type": "user", "content": "hello"}}) + "\n", encoding="utf-8")
    recs = kiro.kiro_light_records(show_all=False)
    assert [r["id"] for r in recs] == ["sess_messy"]


def test_stored_title_wins_over_first_message(store):
    sdir = _session(store, "aaaa", "sess_t", title="Real Name")
    assert kiro.kiro_title(str(sdir)) == "Real Name"


@pytest.mark.parametrize("placeholder", ["New Session", "New Kiro Session", "  ", None])
def test_placeholder_title_falls_back_to_first_user_message(store, placeholder):
    sdir = _session(store, "aaaa", "sess_p", title=placeholder,
                    turns=(("user", "  fix\n the   parser  "), ("assistant", "ok")))
    assert kiro.kiro_title(str(sdir)) == "fix the parser"


def test_fallback_title_is_cut_to_limit(store):
    long = "x" * 200
    sdir = _session(store, "aaaa", "sess_long", title="New Session", turns=(("user", long),))
    assert len(kiro.kiro_title(str(sdir))) == kiro.KIRO_TITLE_CHARS


def test_resolve_by_prefix(store):
    _session(store, "aaaa", "sess_abc123")
    _session(store, "bbbb", "sess_abd999")
    assert kiro.kiro_resolve("sess_abc") == ["sess_abc123"]
    assert kiro.kiro_resolve("sess_ab") == ["sess_abc123", "sess_abd999"]
    assert kiro.kiro_resolve("abc123") == ["sess_abc123"]
    assert kiro.kiro_resolve("nope") == []


def test_session_cwd(store):
    _session(store, "aaaa", "sess_c", cwd="/home/hunt/work/crossweave")
    assert kiro.kiro_session_cwd("sess_c") == "/home/hunt/work/crossweave"
    assert kiro.kiro_session_cwd("sess_missing") is None


def test_handoff_keeps_user_and_assistant_text_in_order_untruncated(store):
    big = "y" * 5000
    sdir = _session(store, "aaaa", "sess_h",
                    turns=(("user", "first"), ("assistant", big), ("user", "last")),
                    raw_lines=[json.dumps({"payload": {"type": "tool_call", "content": "x"}})])
    assert kiro.kiro_handoff_messages(str(sdir)) == [
        ("user", "first"), ("assistant", big), ("user", "last")]


def test_snippet_samples_user_and_assistant_text(store):
    sdir = _session(store, "aaaa", "sess_s",
                    turns=(("user", "alpha"), ("assistant", "beta"), ("user", "gamma")))
    snippet = kiro.kiro_snippet(str(sdir), max_messages=12, max_chars=800)
    for word in ("alpha", "beta", "gamma"):
        assert word in snippet


def test_reader_does_not_write_to_store(store):
    sdir = _session(store, "aaaa", "sess_ro")
    before = sorted(p.name for p in sdir.iterdir())
    kiro.kiro_light_records(show_all=False)
    kiro.kiro_title(str(sdir))
    assert sorted(p.name for p in sdir.iterdir()) == before
