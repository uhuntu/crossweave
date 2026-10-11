"""One read of a rollout, shared by every question asked of it.

`cw search` asks each codex session for its title, its snippet, its cwd and,
for a handoff or a fork, its parent thread. Each of those used to read and
json-parse the rollout again from scratch: three full passes over a 220MB
store per search, most of what the command cost.

The scan is now shared, which makes the *results* the interesting thing to pin
down -- every codex title heuristic in this repo is the product of a past
wrong answer, so the filter over the shared scan has to keep producing exactly
what the separate readers produced before.
"""

import json

import pytest

from crossweave import codex, common, sessions


@pytest.fixture(autouse=True)
def _reset_codex_caches():
    codex._codex_path_index = None
    codex._codex_scan_cache.clear()
    codex._codex_meta_cache.clear()
    yield
    codex._codex_path_index = None
    codex._codex_scan_cache.clear()
    codex._codex_meta_cache.clear()


ROLLOUT = "019ffdbe-1234-7abc-8def-0000000000aa"


def write_rollout(monkeypatch, tmp_path, sid, lines):
    monkeypatch.setattr(common, "CODEX_HOME", str(tmp_path / ".codex"))
    day_dir = tmp_path / ".codex" / "sessions" / "2026" / "08" / "15"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-15T10-33-27-{sid}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def meta(sid, cwd="/work/proj", parent=None):
    payload = {"id": sid, "cwd": cwd}
    if parent:
        payload["parent_thread_id"] = parent
    return {"type": "session_meta", "payload": payload}


def message(role, text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": role,
        "content": [{"type": "input_text", "text": text}]}}


def tool_call(command):
    return {"type": "response_item", "payload": {
        "type": "function_call", "name": "shell",
        "arguments": json.dumps({"command": command})}}


def test_the_transcript_is_scanned_once_for_four_readers(monkeypatch, tmp_path):
    """Title, snippet, cwd and parent all come out of one read of the file.

    The file is still opened once more, for the session_meta questions (cwd,
    parent) -- that is a two-line read, kept out of the transcript scan on
    purpose -- but the two of them share that one read through the meta
    cache instead of re-reading the head apiece. What the scan stops is the
    four separate passes over every line of the transcript."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT), message("user", "fix the nfc lock")])
    scans = []
    real_scan = sessions._codex_scan

    def counting_scan(*args, **kwargs):
        scans.append(args)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(codex, "_codex_scan", counting_scan)
    opens = []
    real_open_text = common.open_text

    def counting_open_text(path):
        opens.append(path)
        return real_open_text(path)

    monkeypatch.setattr(common, "open_text", counting_open_text)

    sessions.codex_rollout_title(ROLLOUT)
    opens_after_title = len(opens)

    sessions.codex_rollout_snippet(ROLLOUT)

    # both transcript readers ask the scan, and the second one reads nothing
    assert len(scans) == 2
    assert opens_after_title == 1
    assert len(opens) == opens_after_title

    # cwd and parent still read the session_meta themselves -- two lines,
    # not worth folding into the scan -- but the second one reads nothing
    sessions.codex_cwd(ROLLOUT)
    sessions.codex_parent_thread_id(ROLLOUT)
    assert len(opens) == 2


def test_the_scan_serves_both_the_title_and_the_snippet(monkeypatch, tmp_path):
    """Boilerplate is flagged rather than dropped, because the two readers want
    opposite things from it: the title and the snippet skip it, while the seed
    fallback that names a session a tool started for itself has to see it."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT),
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "# AGENTS.md\n\nbe terse"}]}},
        message("user", "the real request"),
        tool_call("aria2c https://example.com/x.tgz"),
        message("assistant", "downloaded it"),
    ])

    assert sessions.codex_rollout_title(ROLLOUT) == "the real request"
    snippet = sessions.codex_rollout_snippet(ROLLOUT)
    assert "the real request" in snippet
    assert "downloaded it" in snippet
    # a tool call is snippet material but never a title
    assert "aria2c" in snippet
    assert "AGENTS.md" not in snippet


def test_the_scan_keeps_a_sessions_own_shape(monkeypatch, tmp_path):
    """Tool calls and boilerplate are recorded in place, in file order, so the
    title (the first genuine message) and the snippet (a stride sample) both
    come out in the order the conversation happened."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT),
        message("user", "one"),
        tool_call("echo two"),
        message("assistant", "three"),
    ])
    entries = sessions._codex_scan(sessions.codex_rollout_path(ROLLOUT))
    assert [e[0] for e in entries] == ["message", "tool", "message"]
    assert entries[1] == ("tool", "echo two")
    assert entries[0][1] == "user" and entries[0][3] is False


def test_a_rewritten_rollout_is_reread(monkeypatch, tmp_path):
    """A resumed thread rewrites the store and a test rewrites the same path,
    so a cache keyed by path alone would serve the old transcript forever."""
    path = write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT), message("user", "first version")])
    assert sessions.codex_rollout_title(ROLLOUT) == "first version"

    # same mtime, same size, different content -- the size check is what
    # catches this one
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta(ROLLOUT)) + "\n")
        fh.write(json.dumps(message("user", "second version, longer")) + "\n")

    assert sessions.codex_rollout_title(ROLLOUT) == "second version, longer"


def test_the_cache_holds_the_extracted_texts_not_the_records(monkeypatch, tmp_path):
    """The point of caching extracted texts: a 220MB store costs kilobytes per
    session, rather than the megabytes of parsed rollout it came from."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT), message("user", "x" * 5000), tool_call("echo " + "y" * 500)])
    path = sessions.codex_rollout_path(ROLLOUT)
    entries = sessions._codex_scan(path)

    assert [e[2] for e in entries if e[0] == "message"] == ["x" * 5000]
    assert entries[-1] == ("tool", "echo " + "y" * 500)
    # nothing of the surrounding payload structure is retained
    assert len(json.dumps(entries, default=str)) < 7000
    assert entries  # and the same object is handed back next time
    assert sessions._codex_scan(path) is entries


def test_an_unreadable_rollout_scans_as_empty(monkeypatch, tmp_path):
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [meta(ROLLOUT)])
    path = sessions.codex_rollout_path(ROLLOUT)

    def unreadable(p):
        raise PermissionError(p)

    monkeypatch.setattr(common, "open_text", unreadable)

    assert sessions._codex_scan(path) == []
    assert sessions.codex_rollout_snippet(ROLLOUT) == ""
    assert sessions.codex_rollout_title(ROLLOUT) == "(no title)"

# ---------- the literal pre-pass (sessions.literal_matches) ----------

def _claude_record(path, sid="claude-1"):
    return {"tool": "claude", "id": sid, "path": str(path)}


def test_literal_matches_finds_term_case_insensitively(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"content": "downloading with ARIA2C -x8"}}) + "\n")

    assert [r["id"] for r in sessions.literal_matches([_claude_record(f)], "aria2c")] == ["claude-1"]
    assert [r["id"] for r in sessions.literal_matches([_claude_record(f)], "ARIA2C")] == ["claude-1"]
    assert sessions.literal_matches([_claude_record(f)], "rsync") == []


def test_literal_matches_tolerates_missing_files(tmp_path):
    assert sessions.literal_matches([_claude_record(tmp_path / "gone.jsonl")], "aria2c") == []


# The literal pass answers "which sessions contain this" by scanning every
# transcript, so it has to stay exact while it goes fast: it now rejects a line
# on raw bytes before decoding it, and a pre-filter that is not a strict
# superset of the real pattern loses a match with no way to notice.

def test_literal_matches_still_finds_a_topic_in_a_line_with_non_ascii(tmp_path):
    """The bytes cut only applies to a line that is itself ASCII; this one
    isn't, so it has to take the decoded path and still match."""
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {
        "content": "用 aria2c 下载镜像，速度很慢"}}) + "\n")
    assert [r["id"] for r in sessions.literal_matches([_claude_record(f)], "aria2c")] == ["claude-1"]


def test_literal_matches_matches_a_non_ascii_topic(tmp_path):
    """A non-ASCII topic gets no bytes cut at all, because folding case on
    bytes is ASCII-only. The text scan has to be the one that matches it, so
    this pins a Chinese topic against a Chinese transcript."""
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {
        "content": "排查 run.sh 启动卡住的问题"}}) + "\n")
    assert [r["id"] for r in sessions.literal_matches([_claude_record(f)], "启动卡住")] == ["claude-1"]
    assert sessions.literal_matches([_claude_record(f)], "没有提到的东西") == []


def test_literal_matches_finds_a_topic_that_only_folds_via_unicode(tmp_path):
    """U+212A KELVIN SIGN lowercases to an ASCII "k" and U+0130 to an "i", so
    a session about a temperature expressed in kelvin matches a search for
    "k" with no "k" anywhere in its bytes. Those two characters are the whole
    set (enumerated over Unicode), and a cut taken on raw bytes has to let a
    line holding one through to be judged properly."""
    kelvin = chr(0x212A)
    # The only "k" in the transcript is the Kelvin sign, in both spellings
    # these files write non-ASCII as: raw UTF-8, and the escaped code point.
    for ensure_ascii in (False, True):
        f = tmp_path / "s.jsonl"
        f.write_text(json.dumps({"type": "user", "message": {
            "content": f"the {kelvin} value"}}, ensure_ascii=ensure_ascii) + chr(10),
            encoding="utf-8")
        assert sessions.literal_matches([_claude_record(f)], "k") == [_claude_record(f)], ensure_ascii


def test_literal_matches_is_case_insensitive_in_both_directions(tmp_path):
    """The bytes cut folds ASCII case; this pins it against a topic and a    transcript that disagree on case in either order."""
    for topic, text in (("aria2c", "ARIA2C"), ("ARIA2C", "aria2c"), ("Aria2c", "aRIA2c")):
        f = tmp_path / "s.jsonl"
        f.write_text(json.dumps({"type": "user", "message": {"content": f"using {text} now"}}) + "\n")
        assert sessions.literal_matches([_claude_record(f)], topic) == [_claude_record(f)], topic


def test_literal_matches_stops_at_the_scan_limit(tmp_path):
    """Bounded work: a transcript is not read forever, however interesting it
    is past the cap. The cap is deliberately far past anything a normal
    conversation reaches."""
    f = tmp_path / "s.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        for _ in range(sessions.LITERAL_SCAN_LIMIT + 50):
            fh.write(json.dumps({"type": "user", "message": {"content": "padding"}}) + "\n")
        fh.write(json.dumps({"type": "user", "message": {"content": "aria2c at the very end"}}) + "\n")
    assert sessions.literal_matches([_claude_record(f)], "aria2c") == []

    f2 = tmp_path / "early.jsonl"
    f2.write_text(json.dumps({"type": "user", "message": {"content": "aria2c at the start"}}) + "\n")
    assert [r["id"] for r in sessions.literal_matches([_claude_record(f2)], "aria2c")] == ["claude-1"]

def test_literal_matches_scans_kimi_wire(tmp_path):
    sdir = tmp_path / "session"
    (sdir / "agents" / "main").mkdir(parents=True)
    (sdir / "agents" / "main" / "wire.jsonl").write_text(
        json.dumps({"type": "turn.prompt", "input": [{"type": "text", "text": "used aria2c here"}]}) + "\n"
    )
    rec = {"tool": "kimi", "id": "k1", "dir": str(sdir)}
    assert [r["id"] for r in sessions.literal_matches([rec], "aria2c")] == ["k1"]


def test_literal_matches_scans_kimi_background_task_output_logs(tmp_path):
    """kimi streams bash-task output to tasks/<id>/output.log, which stays
    OUT of wire.jsonl -- a real session mentioned the searched term only in
    such a log and was unfindable before the literal pre-pass."""
    sdir = tmp_path / "session"
    (sdir / "agents" / "main" / "tasks" / "bash-9").mkdir(parents=True)
    (sdir / "agents" / "main" / "wire.jsonl").write_text(
        json.dumps({"type": "turn.prompt", "input": [{"type": "text", "text": "run the build"}]}) + "\n"
    )
    (sdir / "agents" / "main" / "tasks" / "bash-9" / "output.log").write_text(
        "12:00:01 aria2c --continue --max-tries=20 ruby.tar.gz\n"
    )
    rec = {"tool": "kimi", "id": "k2", "dir": str(sdir)}
    assert [r["id"] for r in sessions.literal_matches([rec], "aria2c")] == ["k2"]


def test_literal_matches_codex_rollout(monkeypatch, tmp_path):
    rollout = tmp_path / "r.jsonl"
    rollout.write_text(json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "grab it with aria2c"}]},
    }) + "\n")
    monkeypatch.setattr(sessions, "codex_rollout_path", lambda sid: str(rollout))

    rec = {"tool": "codex", "id": "c1"}
    assert [r["id"] for r in sessions.literal_matches([rec], "aria2c")] == ["c1"]
    assert sessions.literal_matches([rec], "wget") == []


def test_literal_matches_short_acronym_ignores_longer_words_and_metadata(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "attachment", "rendered": "Project: CRA Article 14"},
        {"type": "user", "message": {"content": "Help craft a plan for the crash"}},
    ]))
    record = _claude_record(f)
    assert sessions.literal_matches([record], "cra") == []
    f.write_text(f.read_text() + json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "The CRA meeting is next week."}]
    }}) + "\n")
    assert sessions.literal_matches([record], "cra") == [record]


def test_literal_matches_ignores_codex_injected_context(monkeypatch, tmp_path):
    rollout = tmp_path / "r.jsonl"
    rollout.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "session_meta", "payload": {"title": "CRA"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": "CRA policy"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Help with wifi"}]}},
    ]))
    monkeypatch.setattr(sessions, "codex_rollout_path", lambda sid: str(rollout))
    record = {"tool": "codex", "id": "c1"}
    assert sessions.literal_matches([record], "cra") == []
    rollout.write_text(rollout.read_text() + json.dumps({"type": "response_item", "payload": {
        "type": "custom_tool_call", "input": 'x({cmd:"ls CRA"})'
    }}) + "\n")
    assert sessions.literal_matches([record], "cra") == [record]


