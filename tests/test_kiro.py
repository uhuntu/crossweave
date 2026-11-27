"""Tests for the Kiro session reader (kiro.py). Each test builds a small
~/.kiro/sessions tree in a temp directory; nothing touches the real store."""
import calendar
import json
import os

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
    kiro_home = tmp_path / "kiro"
    root = kiro_home / "sessions"
    root.mkdir(parents=True)
    monkeypatch.setattr(kiro.common, "KIRO_HOME", str(kiro_home))
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


# --- wiring into sessions.py -------------------------------------------------

from crossweave import sessions  # noqa: E402


def test_turn_count_and_last_message(store):
    sdir = _session(store, "aaaa", "sess_k",
                    turns=(("user", "one"), ("assistant", "two"), ("user", "three")))
    assert kiro.kiro_turn_count(str(sdir)) == 2
    assert kiro.kiro_last_message(str(sdir)) == "three"


def test_kiro_is_a_registered_session_tool():
    assert "kiro" in sessions.TOOLS
    assert "kiro" in sessions.ID_RESOLVERS


def test_listing_row_and_turns_come_from_the_store(store, tmp_path, monkeypatch):
    _session(store, "aaaa", "sess_row", cwd=str(tmp_path),
             turns=(("user", "fix the parser"), ("assistant", "done")))
    rec = next(r for r in sessions._all_light_records() if r["id"] == "sess_row")
    assert rec["tool"] == "kiro"
    assert sessions.session_turns(rec) == 1
    assert sessions._resolve_title_and_cwd(rec)[0] == "fix the parser"


def test_resolve_and_find_record(store):
    _session(store, "aaaa", "sess_find1")
    assert sessions.ID_RESOLVERS["kiro"]("sess_find") == ["sess_find1"]
    assert sessions._find_record("kiro", "sess_find1")["id"] == "sess_find1"
    assert sessions._find_record("kiro", "sess_nope") is None


def test_handoff_export_has_full_transcript_for_kiro(store, tmp_path, monkeypatch):
    big = "z" * 3000
    _session(store, "aaaa", "sess_exp", cwd=str(tmp_path),
             turns=(("user", "start"), ("assistant", big)))
    cwd, transcript = sessions.session_handoff_details("kiro", "sess_exp")
    assert cwd == str(tmp_path)
    assert "# Crossweave handoff from kiro" in transcript
    assert "start" in transcript and big in transcript


def test_handoff_into_kiro_is_one_interactive_step(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "handoffs"))
    _session(store, "aaaa", "sess_src", cwd=str(tmp_path),
             turns=(("user", "hi"), ("assistant", "hello")))
    launched = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: launched.append(argv))
    sessions.perform_handoff("kiro", "sess_src", "kiro", [])
    assert launched, "kiro was not launched"
    argv = launched[-1]
    assert argv[:4] == ["kiro-cli", "chat", "--trust-tools=fs_read", argv[3]]
    assert "Read the complete conversation export at" in argv[3]
    assert "--no-interactive" not in argv


def test_resume_by_id_uses_resume_id(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _session(store, "aaaa", "sess_res1", cwd=str(tmp_path))
    launched = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: launched.append(argv))
    sessions.cmd_resume(["kiro", "sess_res1"])
    assert launched == [["kiro-cli", "chat", "--resume-id", "sess_res1"]]


def test_resume_without_id_opens_kiro_picker(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    launched = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: launched.append(argv))
    sessions.cmd_resume(["kiro"])
    assert launched == [["kiro-cli", "chat", "--resume-picker"]]


# --- search ------------------------------------------------------------------

from crossweave import retrieval, search  # noqa: E402


def test_search_gathers_kiro_candidates(store, tmp_path):
    _session(store, "aaaa", "sess_cand", cwd=str(tmp_path), turns=(("user", "topic one"),))
    everything = search.gather_candidates(None)
    assert "sess_cand" in {r["id"] for r in everything if r["tool"] == "kiro"}
    only_kiro = search.gather_candidates("kiro")
    assert only_kiro and all(r["tool"] == "kiro" for r in only_kiro)


def test_search_snippet_reads_user_and_assistant_text(store, tmp_path):
    _session(store, "aaaa", "sess_snip", cwd=str(tmp_path),
             turns=(("user", "why is the build slow"), ("assistant", "the linker is single threaded")))
    rec = next(r for r in search.gather_candidates("kiro") if r["id"] == "sess_snip")
    snippet = search.snippet_for(rec)
    assert "build" in snippet and "linker" in snippet


def test_literal_scan_finds_a_term_in_kiro_turns(store, tmp_path):
    _session(store, "aaaa", "sess_lit", cwd=str(tmp_path),
             turns=(("user", "hello"), ("assistant", "the zebra_marker_77 is in the config")))
    rec = next(r for r in search.gather_candidates("kiro") if r["id"] == "sess_lit")
    hits = sessions.literal_matches([rec], "zebra_marker_77")
    assert [h["id"] for h in hits] == ["sess_lit"]


def test_retrieval_context_names_the_matching_turn(store, tmp_path):
    _session(store, "aaaa", "sess_ctx", cwd=str(tmp_path),
             turns=(("user", "hello"), ("assistant", "the zebra_marker_77 is in the config")))
    rec = next(r for r in search.gather_candidates("kiro") if r["id"] == "sess_ctx")
    assert "zebra_marker_77" in retrieval.context_for(rec, "zebra_marker_77")


def test_web_conversation_view_shows_kiro_turns(store, tmp_path):
    from crossweave import web
    sdir = _session(store, "aaaa", "sess_web", cwd=str(tmp_path),
                    turns=(("user", "where is the export"), ("assistant", "in the handoffs folder")))
    record = {"tool": "kiro", "id": "sess_web", "path": str(sdir)}
    assert web._session_messages("kiro", "sess_web", record) == [
        ("user", "where is the export"), ("assistant", "in the handoffs folder")]


def test_delete_moves_the_kiro_session_to_a_backup_and_restores(store, tmp_path, monkeypatch):
    from crossweave import delete
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "cache" / "list.json"))
    sdir = _session(store, "aaaa", "sess_del", cwd=str(tmp_path),
                    turns=(("user", "delete me"), ("assistant", "ok")))
    record = next(r for r in kiro.kiro_light_records(show_all=True) if r["id"] == "sess_del")
    assert delete._paths(record) == [str(sdir)]
    backup = delete.delete_record(record)
    assert not sdir.exists(), "session directory should have moved out of the store"
    with open(os.path.join(backup, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["tool"] == "kiro" and manifest["id"] == "sess_del"
    # restoring by moving the backup copy back returns the session to the store
    import shutil
    shutil.move(manifest["files"][0]["backup"], manifest["files"][0]["original"])
    assert sdir.exists()
    assert kiro.kiro_light_records(show_all=True)


def test_rename_sets_the_title_and_keeps_recency_and_other_fields(store, tmp_path):
    from crossweave import rename
    sdir = _session(store, "aaaa", "sess_ren", cwd=str(tmp_path), title="New Session",
                    modified="2026-11-27T03:32:18.235Z")
    before = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    assert rename._rename_kiro(str(sdir), "Parser work") is True
    after = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    assert after["title"] == "Parser work"
    assert after["lastModifiedAt"] == before["lastModifiedAt"]
    assert after["workspacePaths"] == before["workspacePaths"]
    assert kiro.kiro_title(str(sdir)) == "Parser work"
    assert not list(sdir.glob("*.tmp"))


def test_rename_refuses_a_missing_session(store, tmp_path):
    from crossweave import rename
    assert rename._rename_kiro(str(tmp_path / "nope"), "x") is False
