"""Replay every captured fixture through its tool's readers.

Fixtures are blinded captures of real store files (tests/capture_fixture.py
makes them; nothing here makes them). Each one is a frozen answer to "what
did this format look like the day we accepted it": the manifest holds the
line and record counts at capture time, and the test holds the readers to
those numbers plus a clean pass through the per-record text extraction.
A reader change that starts crashing on, dropping, or miscounting the
accepted shape fails here before it can fail a listing.

The directory is empty until someone runs the capture script by hand and
commits what they have reviewed -- so with no fixtures this file skips,
and that is the designed state, not a gap.
"""

import glob
import json
import os

import pytest

from crossweave import scan, sessions


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixtures():
    out = []
    if os.path.isdir(FIXTURES):
        for tool in sorted(os.listdir(FIXTURES)):
            for path in sorted(glob.glob(os.path.join(FIXTURES, tool, "*.jsonl"))):
                out.append((tool, path))
    return out


@pytest.mark.parametrize("tool,path", _fixtures(), ids=lambda v: v if isinstance(v, str) else "")
def test_fixture_still_reads_exactly_as_captured(tool, path):
    with open(path + ".json", encoding="utf-8") as fh:
        manifest = json.load(fh)

    records = list(sessions.read_jsonl(path))
    assert len(records) == manifest["records"], (
        f"{path}: the capture parsed {manifest['records']} records; "
        "the reader now parses a different number")

    # Every line the capture kept must still survive the per-record text
    # extraction without an exception -- _literal_texts is type-guarded, so
    # a crash here would be a reader bug rather than dirty data.
    for record in records:
        texts = scan._literal_texts(tool, record)
        assert isinstance(texts, list)
        assert all(isinstance(t, str) for t in texts)


def test_the_fixtures_directory_is_wired():
    """Guards the harness against silent rot: a fixtures directory that
    exists but accumulates manifests without transcripts (or vice versa)
    should say so rather than skip everything forever."""
    if not os.path.isdir(FIXTURES):
        pytest.skip("no fixtures captured yet -- see tests/capture_fixture.py")
    manifests = {os.path.basename(m)[:-len(".json")]
                 for m in glob.glob(os.path.join(FIXTURES, "*", "*.json"))}
    transcripts = {os.path.basename(t)
                   for t in glob.glob(os.path.join(FIXTURES, "*", "*.jsonl"))}
    assert manifests == transcripts, \
        "every transcript needs its manifest and vice versa"
