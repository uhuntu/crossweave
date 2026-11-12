import datetime
import os

from crossweave import note, search, wiki


def _row(i):
    return ("codex", f"id-{i}", "2d ago", f"id-{i}"[:8], "/w", f"session {i}")


def _found(judged=(), exact=(), reasons=None):
    ids = {*judged, *exact}
    return search.Found(
        records={("codex", f"id-{i}"): {"tool": "codex", "id": f"id-{i}", "ts": 1} for i in ids},
        judged=[_row(i) for i in judged],
        exact_rows=[_row(i) for i in exact],
        semantic=[], reasons=reasons or {}, used_judge="step",
    )


class _Proc:
    returncode = 0
    stderr = ""

    def __init__(self, stdout):
        self.stdout = stdout


def _gather(ts_by_id):
    def fake(tool_filter, show_all=False):
        return [{"tool": "codex", "id": sid, "ts": ts} for sid, ts in ts_by_id.items()]
    return fake


def _setup(monkeypatch, tmp_path, found, reply="## 概要\n- 用 Dependency-Track 做 SCA [S1][S2]"):
    folder = tmp_path / "proj"
    folder.mkdir(exist_ok=True)
    (folder / "2026-10-27 SBOM.md").write_text(
        '---\ntitle: "SBOM"\ntool: "codex"\nsession: "id-1"\nnoted: 2099-01-01T00:00\n---\n'
        "# SBOM\n\n## 做了什么\n- 画了 SBOM 流程图\n", encoding="utf-8")
    monkeypatch.setattr(search, "find", lambda *a, **kw: found)
    from crossweave import rename
    monkeypatch.setattr(rename, "_digest_for", lambda r: f"digest of {r['id']}")
    # the vault scan for staleness sees no stores unless a test says otherwise
    monkeypatch.setattr(search, "gather_candidates", _gather({}))
    prompts = []
    monkeypatch.setattr(search, "call_judge_with_fallback",
                        lambda prompt, j, e, label: prompts.append(prompt) or (_Proc(reply), j))
    return prompts


def test_sources_put_the_judges_picks_first_then_literal_hits():
    got = wiki._sources(_found(judged=[3, 1], exact=[1, 2]), limit=10)
    assert [row[1] for row, _, _ in got] == ["id-3", "id-1", "id-2"]
    assert len(wiki._sources(_found(judged=[3, 1], exact=[2]), limit=2)) == 2


def test_build_reads_notes_and_digests_and_links_citations(monkeypatch, tmp_path):
    prompts = _setup(monkeypatch, tmp_path, _found(judged=[1, 2], reasons={("codex", "id-1"): "SBOM 流程"}))
    path = wiki.build("SBOM", str(tmp_path), "step")
    assert path == str(tmp_path / "_wiki" / "SBOM.md")
    # source 1 is read by its note, source 2 (no note) by its digest
    assert "画了 SBOM 流程图" in prompts[0] and "digest of id-2" in prompts[0]
    text = open(path, encoding="utf-8").read()
    assert "kind: cw-wiki" in text and 'sources: ["codex id-1", "codex id-2"]' in text
    assert 'topic: "SBOM"' in text
    # a citation of a noted source links to the note; the other stays plain
    assert "[[2026-10-27 SBOM|S1]][S2]" in text
    assert "原文: `cw show codex id-1`" in text
    assert "- **S1** [[2026-10-27 SBOM]] · codex · 2d ago · `cw resume codex id-1` — SBOM 流程" in text
    assert "- **S2** session 2 · codex" in text
    assert wiki.OWN_MARKER in text
    # the build leaves the index behind, listing the page
    assert "SBOM" in open(tmp_path / "_wiki" / note.INDEX_NAME, encoding="utf-8").read()


def test_a_rebuild_keeps_what_the_user_wrote_under_the_marker(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    path = wiki.build("SBOM", str(tmp_path), "step")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("- 我自己加的一条\n")
    wiki.build("SBOM", str(tmp_path), "step")
    text = open(path, encoding="utf-8").read()
    assert text.count(wiki.OWN_MARKER) == 1
    assert "- 我自己加的一条" in text


def test_wiki_pages_are_not_mistaken_for_session_notes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    wiki.build("SBOM", str(tmp_path), "step")
    assert set(note.noted_index(str(tmp_path))) == {"id-1"}
    assert set(note.session_notes(str(tmp_path))) == {"id-1"}
    # the wiki's own _index.md exists now -- but note's machinery neither
    # reads it as a note nor overwrites it with a project index
    mine = (tmp_path / "_wiki" / note.INDEX_NAME).read_text(encoding="utf-8")
    assert "tags: [cw-wiki-index]" in mine
    note.rebuild_indexes(str(tmp_path))
    assert (tmp_path / "_wiki" / note.INDEX_NAME).read_text(encoding="utf-8") == mine


def test_nothing_found_writes_nothing(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _found())
    assert wiki.build("SBOM", str(tmp_path), "step") is None
    assert not (tmp_path / "_wiki").exists()
    assert "nothing to write" in capsys.readouterr().err


def test_a_reply_without_sections_writes_no_page(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]), reply="Sorry, I can't.")
    assert wiki.build("SBOM", str(tmp_path), "step") is None


def test_cmd_wiki_lists_pages_without_a_topic(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _found(judged=[1, 2]))
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["SBOM"]) == 0
    # the platform's own separator: Windows prints _wiki\SBOM.md
    assert "wiki: " + os.path.join("_wiki", "SBOM.md") in capsys.readouterr().out
    assert wiki.cmd_wiki([]) == 0
    assert "SBOM  (2 sessions, built" in capsys.readouterr().out


def test_cmd_wiki_rejects_bad_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["x", "--judge", "nope"]) == 2
    assert wiki.cmd_wiki(["x", "--tool", "nope"]) == 2
    assert wiki.cmd_wiki(["x", "--max", "0"]) == 2
    assert wiki.cmd_wiki(["--nope"]) == 2
    assert wiki.cmd_wiki(["--stale", "--discover"]) == 2
    assert wiki.cmd_wiki(["--rebuild", "a topic"]) == 2


def test_sources_the_page_never_cites_are_left_off_the_list(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1, 2, 3]), reply="## 概要\n- x [S3]")
    path = wiki.build("SBOM", str(tmp_path), "step")
    text = open(path, encoding="utf-8").read()
    assert "**S3**" in text and "**S1**" not in text and "**S2**" not in text
    assert 'sources: ["codex id-3"]' in text


def test_a_page_citing_nothing_is_not_written(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]), reply="## 概要\n- no citations")
    assert wiki.build("SBOM", str(tmp_path), "step") is None


# ---------- cross-page links and the related section ----------

def test_a_page_naming_another_page_links_to_it(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    wiki.build("SBOM", str(tmp_path), "step")
    _setup(monkeypatch, tmp_path, _found(judged=[1]),
           reply="## 概要\n- 见 SBOM 那页的流程 [S1]")
    path = wiki.build("SCA", str(tmp_path), "step")
    text = open(path, encoding="utf-8").read()
    assert "[[SBOM]]" in text
    # pages sharing a source session point at each other
    assert wiki.RELATED_HEADING in text and "[[SBOM]] — SBOM · 1 shared session" in text


def test_a_wikilink_span_is_never_wrapped_again(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    wiki.build("SBOM", str(tmp_path), "step")
    _setup(monkeypatch, tmp_path, _found(judged=[1]),
           reply="## 概要\n- 见 [[SBOM]] 与 SBOM [S1]")
    path = wiki.build("SCA", str(tmp_path), "step")
    text = open(path, encoding="utf-8").read()
    # the span kept, the plain word linked, the related section's own link
    assert text.count("[[SBOM]]") == 3


def test_a_page_name_inside_code_is_left_alone(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    wiki.build("SBOM", str(tmp_path), "step")
    _setup(monkeypatch, tmp_path, _found(judged=[1]),
           reply="## 概要\n- `grype sbom:<SBOM>` 与\n```\nSBOM 在块里\n```\n"
                 "外加正文里的 SBOM [S1]")
    path = wiki.build("SCA", str(tmp_path), "step")
    text = open(path, encoding="utf-8").read()
    # the commands stay copyable; only the prose mention becomes a link
    assert "`grype sbom:<SBOM>`" in text
    assert "SBOM 在块里" in text
    assert "正文里的 [[SBOM]]" in text


def test_one_source_reads_as_a_session_not_sessions(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["SBOM"]) == 0
    index = (tmp_path / "_wiki" / note.INDEX_NAME).read_text(encoding="utf-8")
    assert "1 session ·" in index and "1 sessions" not in index
    capsys.readouterr()
    assert wiki.cmd_wiki([]) == 0
    out = capsys.readouterr().out
    assert "(1 session, built" in out and "(1 sessions" not in out


def test_wiki_index_maps_cited_sessions_to_pages(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    wiki.build("SBOM", str(tmp_path), "step")
    assert wiki.wiki_index(str(tmp_path)) == {"id-1": ["SBOM"]}


def test_the_listing_marks_sessions_a_page_cites(monkeypatch, tmp_path, capsys):
    from crossweave import sessions

    record = {"tool": "zcode", "id": "sess_1", "title": "USB 枚举握手排查",
              "cwd": "/w/board", "ts": 1, "path": None}
    monkeypatch.setattr(sessions, "session_turns", lambda r: 3)
    monkeypatch.setattr(sessions, "session_last_message", lambda r: None)
    vault = tmp_path / "vault"
    (vault / "_wiki").mkdir(parents=True)
    (vault / "_wiki" / "SBOM.md").write_text(
        '---\ntitle: "SBOM"\nkind: cw-wiki\nbuilt: 2026-01-01T00:00\n'
        'sources: ["zcode sess_1"]\n---\n# SBOM\n', encoding="utf-8")
    monkeypatch.setenv("CW_VAULT", str(vault))
    rows = [("zcode", "sess_1", "1h ago", "sess_1", "/w/board", "USB 枚举握手排查"),
            ("zcode", "sess_2", "1h ago", "sess_2", "/w/other", "never cited")]
    sessions.render_rows(rows, write_cache=False, sources={"sess_1": record})
    out = capsys.readouterr().out
    assert "USB 枚举握手排查 ⌗" in out
    assert "never cited ⌗" not in out


# ---------- staleness ----------

def test_a_page_a_source_moved_on_since_is_stale(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    path = wiki.build("SBOM", str(tmp_path), "step")
    assert wiki.stale_pages(str(tmp_path)) == {}
    # id-1's session moved on after the build; id-2's did not
    now = datetime.datetime.now().timestamp() + 120
    monkeypatch.setattr(search, "gather_candidates",
                        _gather({"id-1": now, "id-2": 1}))
    assert wiki.stale_pages(str(tmp_path)) == {path: ["codex id-1"]}
    _setup(monkeypatch, tmp_path, _found(judged=[1, 2]))
    wiki.build("SBOM", str(tmp_path), "step")  # rebuild clears it
    monkeypatch.setattr(search, "gather_candidates",
                        _gather({"id-1": 1, "id-2": 1}))
    assert wiki.stale_pages(str(tmp_path)) == {}


def test_cmd_wiki_stale_and_rebuild_touch_only_the_stale_page(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    wiki.build("SBOM", str(tmp_path), "step")
    prompts = _setup(monkeypatch, tmp_path, _found(judged=[2]))
    wiki.build("SCA", str(tmp_path), "step")
    assert len(prompts) == 1
    now = datetime.datetime.now().timestamp() + 120
    monkeypatch.setattr(search, "gather_candidates", _gather({"id-1": now, "id-2": 1}))
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["--stale"]) == 0
    out = capsys.readouterr().out
    assert "SBOM" in out and "SCA" not in out
    assert wiki.cmd_wiki(["--rebuild", "-y"]) == 0
    # only the stale page was rebuilt: the SCA prompt never grew
    assert len(prompts) == 2 and "SBOM" in prompts[1]
    # the platform's own separator: Windows prints _wiki\SBOM.md
    assert "wiki: " + os.path.join("_wiki", "SBOM.md") in capsys.readouterr().out


def test_cmd_wiki_rebuild_with_nothing_stale_says_so(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["--rebuild"]) == 0
    assert "No wiki page is stale" in capsys.readouterr().out


# ---------- --discover ----------

def test_discover_proposes_topics_and_builds_the_new_ones(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _found(judged=[1]))
    wiki.build("SBOM", str(tmp_path), "step")
    prompts = []

    def fake_judge(prompt, j, e, label):
        prompts.append(prompt)
        if len(prompts) == 1:
            return _Proc("1. SCA — 软件组成\n2. SBOM — already has a page\n"), j
        return _Proc("## 概要\n- SCA 的活 [S1]"), j

    monkeypatch.setattr(search, "call_judge_with_fallback", fake_judge)
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["--discover", "-y"]) == 0
    out = capsys.readouterr().out
    assert "SCA" in out and "wiki: " + os.path.join("_wiki", "SCA.md") in out
    # the topic list read the vault's notes, and SBOM was not rebuilt
    assert "画了 SBOM 流程图" in prompts[0]
    assert len(prompts) == 2
    assert (tmp_path / "_wiki" / "SCA.md").exists()


def test_discover_with_no_notes_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["--discover"]) == 1
    assert "no session notes" in capsys.readouterr().err


def test_discover_takes_no_for_an_answer(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, _found(judged=[1]), reply="none")
    monkeypatch.setenv("CW_VAULT", str(tmp_path))
    assert wiki.cmd_wiki(["--discover", "-y"]) == 0
    assert "proposed no new topics" in capsys.readouterr().out
    assert not (tmp_path / "_wiki" / "SBOM.md").exists()
