"""workbuddy, the eighth store: the CodeBuddy Code engine inside a desktop app.

`~/.workbuddy/projects/<slug>/<sessionId>.jsonl` is the chain codebuddy.py
reads -- the app bundles `@genie/agent-cli`, the package the `codebuddy`
binary is built from -- so these tests do not re-litigate that format
(test_codebuddy.py holds it). What they hold is the part that is WorkBuddy's
own: two editions read as one tool with each row remembering the scheme it
came in under, the wrapper the shell decorates every user message with (which
hides the prompt inside the scaffolding the readers throw away), the two
record kinds a plain CLI session never carries (`session-meta`,
`file-history-snapshot`), a subagent transcript under its parent's id, the
siblings of a transcript that are not transcripts, and the way back in -- a
session-level deep link handed to the app, because there is no CLI on PATH to
hand it to.
"""

import json
import os
import sys
import urllib.parse

import pytest

from crossweave import cli, doctor, search, sessions, workbuddy

SID = "4e7afc20-f51a-4d54-a97f-4d872f4626b1"
SID2 = "a34a5214-8353-4616-8362-ca0794416c85"
SUB = "cd780e97-fc0e-40de-bbd1-dd532dd88156"
CWD = "/home/hunt/work/stave"
SLUG = "home-hunt-work-stave"
T0 = 1_789_467_411_548  # epoch ms, the store's unit

EDITIONS = ("workbuddy", "workbuddy-ai")


def use_roots(tmp_path, *editions):
    """Point the reader at `<tmp>/<edition>` for each edition named.

    A direct assignment, not a monkeypatch: conftest's autouse isolation owns
    WORKBUDDY_ROOTS for every test and restores the real value afterwards, so
    nothing here leaks between tests."""
    workbuddy.WORKBUDDY_ROOTS = tuple((str(tmp_path / e), e)
                                      for e in (editions or EDITIONS))
    return workbuddy.WORKBUDDY_ROOTS


def msg(role, text, ts=T0, cwd=CWD, sid=SID):
    return {"id": "m-%d" % ts, "timestamp": ts, "type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text",
                         "text": text}],
            "sessionId": sid, "cwd": cwd}


def ai_title(text, ts=T0 + 1_000, cwd=CWD, sid=SID):
    return {"id": "t-%d" % ts, "timestamp": ts, "type": "ai-title", "aiTitle": text,
            "sessionId": sid, "cwd": cwd}


def fn_call(name, arguments, ts=T0 + 3_000, cwd=CWD, sid=SID):
    return {"id": "fc-%d" % ts, "timestamp": ts, "type": "function_call",
            "name": name, "arguments": json.dumps(arguments),
            "callId": "call-%d" % ts, "sessionId": sid, "cwd": cwd}


def fn_result(text, ts=T0 + 4_000, cwd=CWD, sid=SID):
    return {"id": "fcr-%d" % ts, "timestamp": ts, "type": "function_call_result",
            "name": "Bash", "callId": "call-%d" % (ts - 1_000), "status": "completed",
            "output": {"type": "text", "text": text}, "sessionId": sid, "cwd": cwd}


def session_meta(sid=SID, ts=T0 - 1_000, cwd=CWD, host="unopted"):
    """The header a WorkBuddy session opens with and a CLI session never
    carries: which host kind started it."""
    return {"type": "session-meta", "id": "sm-%d" % ts, "sessionId": sid,
            "timestamp": ts, "cwd": cwd, "meta": {"codebuddy.ai/hostKind": host}}


def snapshot(sid=SID, ts=T0 + 500, cwd=CWD):
    """A rollback point behind `<id>.file-rollback.ndjson`, mirrored into the
    transcript. A real session carries hundreds of them, so they have to stay
    records the readers look past rather than turns, titles or text."""
    return {"type": "file-history-snapshot", "id": "fh-%d" % ts, "sessionId": sid,
            "timestamp": ts, "cwd": cwd,
            "snapshot": {"files": [{"path": "src/main.c", "hash": "x" * 40}]}}


def typed(query, ts=T0, cwd=CWD, sid=SID):
    """A user message as the app's shell records one: the per-turn context
    wrapper, and the words the user actually typed after it inside the same
    block. The CLI writes no such wrapper, so codebuddy.py's own tests never
    see this shape."""
    return msg("user", '<system-reminder data-role="user-context">\n'
                       "<user_info>OS Version: win32</user_info>\n"
                       "</system-reminder>\n"
                       f"<user_query>{query}</user_query>", ts=ts, cwd=cwd, sid=sid)


def recap(quoted, ts=T0, cwd=CWD, sid=SID):
    """The engine's compaction recap of the conversation so far, injected as a
    *user* record. It quotes the messages it summarises, so an earlier turn's
    `<user_query>` sits inside it -- which is the one thing that keeps the
    wrapper unwrap from counting a recap as the prompt it repeats."""
    return msg("user", "<cb_summary> Summary of the conversation so far: "
                       f"<user_query>{quoted}</user_query> </cb_summary>",
               ts=ts, cwd=cwd, sid=sid)


def paste(name, ts=T0, cwd=CWD, sid=SID):
    """A captionless screenshot paste, in the two blocks the app writes for
    one: the query as a reference to the image, and the file it was written
    to. Neither names a topic, so neither is a turn."""
    return {"id": "m-%d" % ts, "timestamp": ts, "type": "message", "role": "user",
            "sessionId": sid, "cwd": cwd,
            "content": [
                {"type": "input_text",
                 "text": '<system-reminder data-role="user-context">\n'
                         "</system-reminder>\n"
                         f"<user_query>@image#1:{name}</user_query>"},
                {"type": "image_blob_ref"},
                {"type": "input_text",
                 "text": "<image_local_path>C:\\tmp\\%s</image_local_path>" % name}]}


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def store(tmp_path, scheme, slug, name, lines):
    return write(tmp_path / scheme / "projects" / slug / name, lines)


def one_session(tmp_path, sid=SID, cwd=CWD, scheme="workbuddy", slug=SLUG, lines=()):
    """A one-session store: the app's own header, a genuine user prompt, then
    `lines`."""
    use_roots(tmp_path)
    return store(tmp_path, scheme, slug, "%s.jsonl" % sid,
                 [session_meta(sid, cwd=cwd), msg("user", "fix the nfc lock", sid=sid, cwd=cwd),
                  *lines])


def subagent(tmp_path, parent=SID, sid=SUB, scheme="workbuddy", slug=SLUG,
             name="agent-90731ca1.jsonl", lines=()):
    """A subagent's transcript: its parent's session directory, its own id."""
    return write(tmp_path / scheme / "projects" / slug / parent / "subagents" / name,
                 [msg("user", "scan the disk, change nothing", sid=sid), *lines])


def record(sid=SID):
    return next(r for r in workbuddy.workbuddy_light_records(show_all=True)
                if r["id"] == sid)


# ---------- discovery ----------

def test_light_records_read_id_cwd_title_and_span(tmp_path):
    one_session(tmp_path, lines=[msg("assistant", "patched the HAL", ts=T0 + 9_000)])
    records = workbuddy.workbuddy_light_records()
    assert len(records) == 1
    r = records[0]
    assert r["tool"] == "workbuddy"
    assert r["id"] == SID
    assert r["cwd"] == CWD
    assert r["title"] == "fix the nfc lock"
    assert r["ts"] == (T0 + 9_000) // 1000
    assert r["turns"] == 1
    assert os.path.isfile(r["path"])


def test_both_editions_read_as_one_tool_and_each_row_keeps_its_scheme(tmp_path):
    """One product, two apps, two stores, two URL schemes. Splitting them
    would ask the reader to know which is which; merging them without the
    scheme would open a session in an app that has never heard of it."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID,
          [msg("user", "fix the nfc lock", sid=SID)])
    store(tmp_path, "workbuddy-ai", "other-project", "%s.jsonl" % SID2,
          [msg("user", "chase the memory leak", sid=SID2, cwd="/home/hunt/work/other")])

    by_id = {r["id"]: r for r in workbuddy.workbuddy_light_records()}
    assert set(by_id) == {SID, SID2}
    assert {r["tool"] for r in by_id.values()} == {"workbuddy"}
    assert by_id[SID]["scheme"] == "workbuddy"
    assert by_id[SID2]["scheme"] == "workbuddy-ai"


def test_one_edition_installed_is_not_a_half_broken_store(tmp_path):
    use_roots(tmp_path)
    store(tmp_path, "workbuddy-ai", SLUG, "%s.jsonl" % SID2,
          [msg("user", "hi", sid=SID2)])
    assert workbuddy.workbuddy_roots_present() == [(str(tmp_path / "workbuddy-ai"),
                                                    "workbuddy-ai")]


def test_a_missing_store_is_an_empty_listing_not_a_crash():
    assert workbuddy.workbuddy_light_records() == []
    assert workbuddy.workbuddy_light_records(show_all=True) == []
    assert sessions._find_record("workbuddy", SID) is None
    assert workbuddy.workbuddy_resolve(SID[:8]) == []
    assert workbuddy.workbuddy_roots_present() == []


def test_the_cwd_comes_from_the_records_not_the_slug(tmp_path):
    """WorkBuddy's slug names the workspace directory the app made for the
    conversation (`c-Users-hunt-WorkBuddy-2026-09-15-18-16-50`), not the
    project the work happened in. Nothing parses it: the row's cwd is the one
    the records carry."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", "c-Users-hunt-WorkBuddy-2026-09-15-18-16-50",
          "%s.jsonl" % SID, [msg("user", "fix the nfc lock", cwd=CWD)])
    assert workbuddy.workbuddy_light_records()[0]["cwd"] == CWD


def test_resolve_matches_by_prefix_across_both_editions(tmp_path):
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", "a", "%s.jsonl" % SID, [msg("user", "one", sid=SID)])
    store(tmp_path, "workbuddy-ai", "b", "%s.jsonl" % SID2,
          [msg("user", "two", sid=SID2)])
    assert workbuddy.workbuddy_resolve("4e7afc") == [SID]
    assert workbuddy.workbuddy_resolve(SID) == [SID]
    assert workbuddy.workbuddy_resolve("a34a") == [SID2]
    assert workbuddy.workbuddy_resolve("zz") == []


def test_an_id_prefix_finds_a_workbuddy_session_with_no_tool_named(tmp_path):
    """`cw <id-prefix>` walks every store's resolver, so the new one has to be
    in that walk or a WorkBuddy id from `cw search` is a dead end."""
    one_session(tmp_path)
    assert sessions.resolve_any_id(SID[:8]) == [("workbuddy", SID)]


# ---------- the app's own record kinds ----------

def test_the_header_and_rollback_records_do_not_disturb_the_row(tmp_path):
    """`session-meta` and `file-history-snapshot` are records a CLI session
    never carries. They are not turns, not titles and not conversation text --
    a real session holds 295 snapshots against 42 messages, so reading them as
    either would bury the row."""
    one_session(tmp_path, lines=[snapshot(), ai_title("NFC lock investigation"),
                                 snapshot(ts=T0 + 2_000)])
    r = record()
    assert r["title"] == "NFC lock investigation"
    assert r["turns"] == 1
    assert sessions.session_turns(r) == 1


def test_a_session_that_is_only_scaffolding_still_lists(tmp_path):
    """The header alone is a session the app started and abandoned: no title,
    no turns, and no crash in the row that renders it."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID,
          [session_meta(), snapshot(),
           msg("user", "<command-name>/model</command-name>", ts=T0 + 1_000)])
    r = record()
    assert r["title"] == "(no title)"
    assert r["turns"] == 0


def test_the_siblings_of_a_transcript_are_not_transcripts(tmp_path):
    """`<id>.meta.json` is the host kind and `<id>.file-rollback.ndjson` is the
    file-history log; both sit next to the session file and neither is one.
    The ndjson is the sharp edge -- it is line-delimited JSON that a reader
    looking for "the session files" would happily parse."""
    path = one_session(tmp_path)
    path.parent.joinpath(SID + ".meta.json").write_text(
        json.dumps({"codebuddy.ai/hostKind": "unopted",
                    "acpConnectionId": "b9aa8550-b020-4c3f-9a73-4ca8b8641085"}),
        encoding="utf-8")
    path.parent.joinpath(SID + ".file-rollback.ndjson").write_text(
        json.dumps({"type": "file-history-snapshot", "sessionId": "not-a-session"}) + "\n",
        encoding="utf-8")

    assert [r["id"] for r in workbuddy.workbuddy_light_records(show_all=True)] == [SID]


def test_a_subagent_transcript_is_a_session_only_under_all(tmp_path):
    """A subagent is a session WorkBuddy started for itself, holding another
    session's context rather than a request of anyone's -- step's
    `subagent-<uuid>.jsonl` case, and the same answer: out of the way unless
    --all asks for it."""
    one_session(tmp_path)
    subagent(tmp_path)

    assert [r["id"] for r in workbuddy.workbuddy_light_records()] == [SID]
    assert sorted(r["id"] for r in workbuddy.workbuddy_light_records(show_all=True)) == \
        sorted([SID, SUB])
    # its own id, so `cw resume workbuddy <sub-id>` can reach it
    assert sessions._find_record("workbuddy", SUB)["id"] == SUB


def test_the_listing_hides_subagents_until_all(monkeypatch, tmp_path, capsys):
    one_session(tmp_path)
    subagent(tmp_path)

    sessions.cmd_list(["--tool", "workbuddy"])
    out = capsys.readouterr().out
    assert SID[:8] in out and SUB[:8] not in out

    sessions.cmd_list(["--tool", "workbuddy", "--all"])
    out = capsys.readouterr().out
    assert SID[:8] in out and SUB[:8] in out


# ---------- the shell's own message shape ----------

def test_the_context_wrapper_does_not_eat_the_prompt(tmp_path):
    """Every word typed in the app arrives inside a `<system-reminder>` block
    the readers otherwise throw away wholesale -- so a session of nothing but
    real prompts would list as having none, and be titled by whatever leaked
    out of the scaffolding instead."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID, [
        session_meta(),
        typed("排查 Cisco VPN 连接报错", ts=T0),
        msg("assistant", "vpncli 的证书已过期", ts=T0 + 6_000),
        typed("再试一次", ts=T0 + 12_000),
    ])
    r = record()
    assert r["turns"] == 2
    assert r["title"] == "排查 Cisco VPN 连接报错"


def test_a_recap_that_quotes_an_earlier_turn_is_not_a_turn(tmp_path):
    """The engine compacts a long session by injecting a summary of it as a
    user message, and that summary quotes the queries it covers. Taking the
    first `<user_query>` anywhere would read the recap as the opening prompt
    a second time, so the unwrap only fires on a message that opens with the
    wrapper."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID, [
        session_meta(),
        typed("fix the nfc lock", ts=T0),
        recap("fix the nfc lock", ts=T0 + 6_000),
        typed("now run the vts suite", ts=T0 + 12_000),
    ])
    r = record()
    assert r["turns"] == 2
    assert r["title"] == "fix the nfc lock"


def test_a_captionless_screenshot_paste_is_no_turn_and_no_title(tmp_path):
    """The app sends a pasted screenshot as a reference to a file it wrote.
    That names no topic, so it counts the way claude's `[Image: ...]` paste
    does -- and a session opened and left on one still lists, at zero turns."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID, [
        session_meta(),
        paste("Clipboard_Screenshot.png", ts=T0),
    ])
    r = record()
    assert not r["turns"]
    assert "image_local_path" not in (r["title"] or "").lower()
    assert "@image#" not in (r["title"] or "")

    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID2, [
        session_meta(sid=SID2),
        typed("看看这个截图", ts=T0),
        paste("second.png", ts=T0 + 6_000, sid=SID2),
    ])
    r2 = record(SID2)
    assert r2["turns"] == 1
    assert r2["title"] == "看看这个截图"


def test_the_wrapped_words_are_what_the_snippet_and_the_scan_read(tmp_path):
    """The unwrap lives in the reader's one text seam, so everything down-
    stream of it -- the line under the row, the judge's snippet, the literal
    scan, the handoff export -- reads the request rather than its wrapper."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID, [
        session_meta(),
        typed("为什么 pnpm 不识别这个依赖", ts=T0),
        msg("assistant", "workspace 文件里的协议写错了", ts=T0 + 6_000),
    ])
    r = record()
    snippet = search.snippet_for(r)
    assert "pnpm" in snippet
    assert "OS Version" not in snippet
    assert [x["id"] for x in sessions.literal_matches([r], "pnpm")] == [SID]


# ---------- the way back in ----------

def test_the_link_names_the_edition_that_owns_the_row(tmp_path):
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "%s.jsonl" % SID, [msg("user", "one", sid=SID)])
    store(tmp_path, "workbuddy-ai", SLUG, "%s.jsonl" % SID2,
          [msg("user", "two", sid=SID2)])

    assert workbuddy.workbuddy_chat_link(record(SID)) == "workbuddy://chat/" + SID
    assert workbuddy.workbuddy_chat_link(record(SID2)) == "workbuddy-ai://chat/" + SID2


def test_a_row_with_no_scheme_gets_the_default_one():
    """A record built by hand (or a cache row from an older listing) has no
    scheme on it; the link still has to be a link."""
    assert workbuddy.workbuddy_chat_link({"id": SID}) == "workbuddy://chat/" + SID


def test_a_hostile_id_reaches_the_app_as_one_inert_argument():
    """The id is percent-encoded with nothing left safe, which is what keeps a
    corrupt or hostile store from turning the link into a second command: the
    spelling escapes everything a shell or cmd.exe reads as a metacharacter."""
    sid = 'x"& rm -rf ~ ;|%41'
    link = workbuddy.workbuddy_chat_link({"id": sid, "scheme": "workbuddy"})
    tail = link.split("://chat/", 1)[1]

    assert link.startswith("workbuddy://chat/")
    assert not (set(tail) & set('"&;|<>$`\\ \'/'))
    assert urllib.parse.unquote(tail) == sid


@pytest.mark.parametrize("platform,name,which,expected", [
    ("darwin", "posix", None, ["open"]),
    ("linux", "posix", "/usr/bin/xdg-open", ["xdg-open"]),
    ("linux", "posix", None, None),
])
def test_the_opener_is_whatever_the_platform_hands_a_url_to(monkeypatch, platform, name,
                                                            which, expected):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "name", name)
    monkeypatch.setattr(workbuddy.shutil, "which", lambda _cmd: which)

    cmd = workbuddy.workbuddy_open_command({"id": SID, "scheme": "workbuddy"})

    if expected is None:
        assert cmd is None  # no way to hand a URL to an app: the caller says so
    else:
        assert cmd == expected + ["workbuddy://chat/" + SID]


def test_on_windows_the_handler_registry_decides_which_app_opens_it(monkeypatch):
    """Both editions register a scheme and each handler points at a different
    executable, so the key is the authority -- and the installer's known path
    is the fallback for a machine whose key was never written."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")

    monkeypatch.setattr(workbuddy, "_windows_scheme_exe",
                        lambda scheme: r"C:\Apps\WorkBuddyAI.exe" if scheme == "workbuddy-ai" else None)
    assert workbuddy.workbuddy_open_command({"id": SID2, "scheme": "workbuddy-ai"}) == \
        [r"C:\Apps\WorkBuddyAI.exe", "workbuddy-ai://chat/" + SID2]
    # nothing to read and nothing to guess at: let the shell do what it does
    assert workbuddy.workbuddy_open_command({"id": SID2, "scheme": "workbuddy"}) == \
        ["cmd", "/c", "start", "", "workbuddy://chat/" + SID2]


def test_the_windows_handler_is_read_out_of_the_registry(monkeypatch, tmp_path):
    exe = tmp_path / "WorkBuddy.exe"
    exe.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(os, "name", "nt")

    class Done:
        stdout = (r'HKEY_CURRENT_USER\Software\Classes\workbuddy\shell\open\command' "\n"
                  r'    (Default)    REG_SZ    "%s" "--" "%%1"' "\n" % exe)

    calls = []
    monkeypatch.setattr(workbuddy.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or Done())

    assert workbuddy._windows_scheme_exe("workbuddy") == str(exe)
    assert calls[0][:2] == ["reg", "query"]
    assert r"HKCU\Software\Classes\workbuddy\shell\open\command" in calls[0][2]


def test_a_registry_that_answers_nothing_falls_back_to_the_install_path(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(workbuddy.subprocess, "run",
                        lambda argv, **kw: type("Done", (), {"stdout": ""})())
    installed = tmp_path / "Programs" / "WorkBuddy" / "WorkBuddy.exe"
    installed.parent.mkdir(parents=True)
    installed.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert workbuddy._windows_scheme_exe("workbuddy") == str(installed)
    assert workbuddy._windows_scheme_exe("no-such-scheme") is None


# ---------- resume ----------

def _no_exec(monkeypatch):
    """The app is a window that was asked for, not the terminal's successor
    process: nothing on this route may replace the wrapper."""
    monkeypatch.setattr(sessions, "exec_or_die",
                        lambda argv: pytest.fail("resume must not exec: %r" % (argv,)))


def test_resume_opens_the_app_on_the_session(monkeypatch, tmp_path, capsys):
    one_session(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    _no_exec(monkeypatch)
    launched = []
    monkeypatch.setattr(sessions.subprocess, "Popen", lambda argv: launched.append(argv))

    sessions.cmd_resume(["workbuddy", "4e7afc"])

    assert launched == [["open", "workbuddy://chat/" + SID]]
    err = capsys.readouterr().err
    assert CWD in err                      # where the work lives, since we stay here
    assert "workbuddy://chat/" + SID in err


def test_resume_by_number_opens_the_app_too(monkeypatch, tmp_path):
    """`cw <N>` and `cw <N> workbuddy` both end up here: the row cache carries
    no scheme, so the record is looked up again rather than trusted."""
    one_session(tmp_path)
    sessions.write_list_cache([{"tool": "workbuddy", "id": SID}])
    monkeypatch.setattr(sys, "platform", "darwin")
    _no_exec(monkeypatch)
    launched = []
    monkeypatch.setattr(sessions.subprocess, "Popen", lambda argv: launched.append(argv))

    sessions.cmd_resume(["1", "workbuddy"])

    assert launched == [["open", "workbuddy://chat/" + SID]]


def test_resume_refuses_words_after_the_id(monkeypatch, tmp_path, capsys):
    """A GUI takes no instruction from a terminal. The refusal names the route
    that does work rather than leaving the words to vanish."""
    one_session(tmp_path)
    monkeypatch.setattr(sessions.subprocess, "Popen",
                        lambda argv: pytest.fail("nothing should launch"))

    with pytest.raises(SystemExit):
        sessions.cmd_resume(["workbuddy", SID, "now", "run", "the", "tests"])

    err = capsys.readouterr().err
    assert "takes no instruction" in err and "cw <N> claude" in err


def test_resume_refuses_a_cwd_override(monkeypatch, tmp_path, capsys):
    """Every other tool relocates by exporting and seeding a fresh session;
    WorkBuddy has nothing to seed, so the honest answer is no."""
    one_session(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(sessions.subprocess, "Popen",
                        lambda argv: pytest.fail("nothing should launch"))

    with pytest.raises(SystemExit):
        sessions.cmd_resume(["workbuddy", SID, "--cwd", str(other)])

    assert "can't be relocated" in capsys.readouterr().err


def test_resume_of_an_id_this_machine_does_not_have_says_where_it_might_be(monkeypatch,
                                                                            tmp_path, capsys):
    """The two real causes are another machine and a conversation the app keeps
    in the cloud, which stores no transcript here -- naming them beats a bare
    "not found"."""
    use_roots(tmp_path)
    with pytest.raises(SystemExit):
        sessions.cmd_resume(["workbuddy", "deadbeef-0000"])

    err = capsys.readouterr().err
    assert "no workbuddy session" in err and "cloud" in err


def test_resume_with_no_way_to_open_a_link_says_so(monkeypatch, tmp_path, capsys):
    one_session(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(workbuddy.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(sessions.subprocess, "Popen",
                        lambda argv: pytest.fail("nothing should launch"))

    with pytest.raises(SystemExit):
        sessions.cmd_resume(["workbuddy", SID])

    assert "nothing here can open a workbuddy:// link" in capsys.readouterr().err


def test_resume_without_an_id_points_at_the_listing(monkeypatch, tmp_path, capsys):
    """No CLI, so no picker to open -- and no silence either."""
    one_session(tmp_path)
    _no_exec(monkeypatch)
    monkeypatch.setattr(sessions.subprocess, "Popen",
                        lambda argv: pytest.fail("nothing should launch"))

    sessions.cmd_resume(["workbuddy"])

    err = capsys.readouterr().err
    assert "no CLI to pick a session with" in err
    assert "cw sessions --tool workbuddy" in err


# ---------- handoff ----------

def test_a_row_handed_to_a_terminal_tool_exports_the_conversation(monkeypatch, tmp_path):
    """Handoff *from* WorkBuddy is the route that works: the transcript is
    there to read, and a terminal tool takes a seed. Speech only -- calls and
    results stay behind, the line every other exporter holds."""
    one_session(tmp_path, lines=[
        msg("assistant", "patched the HAL", ts=T0 + 1_000),
        fn_call("Bash", {"command": "git commit -m fix"}),
        fn_result("1 file changed"),
    ])
    sessions.write_list_cache([{"tool": "workbuddy", "id": SID}])
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.handoff_by_number(1, "claude", [])

    assert calls and calls[-1][0] == "claude"
    assert calls[-1][1].startswith("Continue the work from this workbuddy session")
    body = open(calls[-1][1].split("export at ")[1].split(". First")[0],
                encoding="utf-8").read()
    assert "fix the nfc lock" in body and "patched the HAL" in body
    assert "1 file changed" not in body
    assert "git commit" not in body


def test_nothing_can_seed_a_workbuddy_session(monkeypatch, tmp_path, capsys):
    """...and handoff *to* it is refused unconditionally, not left to fail
    with an export nobody reads. The message names the tools that can."""
    sessions.write_list_cache([{"tool": "claude", "id": "c1"}])
    monkeypatch.setattr(sessions, "exec_or_die",
                        lambda argv: pytest.fail("nothing should run"))

    with pytest.raises(SystemExit):
        sessions.handoff_by_number(1, "workbuddy", [])

    err = capsys.readouterr().err
    assert "starts no session from a prompt" in err and "codebuddy" in err


# ---------- listing, search, stats ----------

def test_workbuddy_rows_appear_in_the_listing(tmp_path, capsys):
    one_session(tmp_path, lines=[msg("assistant", "patched the HAL", ts=T0 + 9_000)])

    sessions.cmd_list(["--tool", "workbuddy"])
    out = capsys.readouterr().out
    assert "workbuddy" in out and "fix the nfc lock" in out
    assert "patched the HAL" in out  # the "where it left off" line

    sessions.cmd_list([])
    out = capsys.readouterr().out
    assert "fix the nfc lock" in out
    assert "codebuddy" not in out  # the other stores were empty, not merely quiet


def test_stats_counts_workbuddy_rows(tmp_path, capsys):
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", "a", "%s.jsonl" % SID, [msg("user", "one", sid=SID)])
    store(tmp_path, "workbuddy-ai", "b", "%s.jsonl" % SID2,
          [msg("user", "two", sid=SID2)])

    sessions.cmd_stats(["--tool", "workbuddy"])
    out = capsys.readouterr().out
    assert "Total sessions: 2" in out
    line = next(line for line in out.splitlines() if line.strip().startswith("workbuddy"))
    assert "2" in line


def test_workbuddy_joins_the_search_candidates(tmp_path):
    one_session(tmp_path)
    assert [r["id"] for r in search.gather_candidates("workbuddy")] == [SID]
    assert SID in [r["id"] for r in search.gather_candidates(None)]


def test_the_snippet_spans_speech_and_calls(tmp_path):
    one_session(tmp_path, lines=[
        fn_call("Bash", {"command": "cat JSENGINES.TXT"}),
        fn_result("4 of 4 records"),
    ])
    snippet = search.snippet_for(record())
    assert "fix the nfc lock" in snippet
    assert "cat JSENGINES.TXT" in snippet
    assert "4 of 4 records" not in snippet  # outputs are the literal scan's, not the snippet's


def test_the_literal_scan_reads_the_transcript_and_its_sidecars(tmp_path):
    """A big tool result lives in `<slug>/<id>/tool-results/<callId>.txt`,
    outside the transcript -- the shape of miss that gave kimi its output.log
    scan and codebuddy its sidecar scan."""
    path = one_session(tmp_path, lines=[
        fn_call("Bash", {"command": "grep -rn B_NFC_LOCATION_27 ."}),
        fn_result("no matches anywhere"),
    ])
    sidecar = path.parent / SID / "tool-results"
    sidecar.mkdir(parents=True)
    (sidecar / "call-1.txt").write_text("the aria2c flag set is --max-conn=16",
                                        encoding="utf-8")
    rec = record()

    assert [r["id"] for r in sessions.literal_matches([rec], "B_NFC_LOCATION_27")] == [SID]
    assert [r["id"] for r in sessions.literal_matches([rec], "aria2c")] == [SID]
    assert [r["id"] for r in sessions.literal_matches([rec], "no matches anywhere")] == [SID]
    assert sessions.literal_matches([rec], "nothing like this") == []


def test_the_last_message_is_the_last_thing_anyone_said(tmp_path):
    one_session(tmp_path, lines=[
        fn_call("Bash", {"command": "git commit -m fix"}),
        fn_result("1 file changed"),
        snapshot(ts=T0 + 5_000),
        msg("assistant", "patched the HAL", ts=T0 + 6_000),
    ])
    assert sessions.session_last_message(record()) == "patched the HAL"


# ---------- doctor ----------

def test_doctor_names_the_editions_it_found(tmp_path, capsys):
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", "a", "%s.jsonl" % SID, [msg("user", "one", sid=SID)])
    store(tmp_path, "workbuddy-ai", "b", "%s.jsonl" % SID2,
          [msg("user", "two", sid=SID2)])

    assert doctor.cmd_doctor(["--tool", "workbuddy"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "2 sessions" in out
    assert "desktop app only (workbuddy + workbuddy-ai)" in out


def test_doctor_reports_a_missing_store(tmp_path, capsys):
    """Neither edition installed is the ordinary case, not a failure."""
    use_roots(tmp_path)
    assert doctor.cmd_doctor(["--tool", "workbuddy"]) == 0
    assert "missing" in capsys.readouterr().out


def test_doctor_calls_a_store_it_cannot_see_into_broken(tmp_path, capsys):
    """Transcript files that resolve to no sessions: the shape a format change
    takes before it takes everything, and the reason doctor exists."""
    use_roots(tmp_path)
    store(tmp_path, "workbuddy", SLUG, "session-without-an-id.jsonl",
          [{"type": "message", "role": "user", "timestamp": T0,
            "content": [{"type": "input_text", "text": "hi"}]}])

    assert doctor.cmd_doctor(["--tool", "workbuddy"]) == 1
    out = capsys.readouterr().out
    assert "broken" in out and "1 transcript file(s)" in out


# ---------- the wrapper side ----------

def test_cw_workbuddy_is_refused_with_the_route_that_works():
    """No executable on PATH and no bundled CLI that runs outside Electron, so
    there is nothing to pass flags to -- the error says where the sessions are
    instead of dying on a spawn."""
    with pytest.raises(cli.UsageError) as exc:
        cli.build_command("workbuddy", ["-p", "summarize"])
    assert "cw sessions --tool workbuddy" in str(exc.value)

    with pytest.raises(cli.UsageError):
        cli.build_command("workbuddy", [])
