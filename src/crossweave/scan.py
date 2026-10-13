"""The literal scan: the byte-and-regex machinery behind `cw search`'s
exact pre-pass.

The scan runs over raw transcript bytes rather than decoded text whenever
the topic lets it: these files are megabytes, decoding is most of the
cost, and an ASCII topic is literal in the raw line whether or not the
rest of it is JSON-escaped. The two cases that force a decode -- a
non-ASCII topic, and a line holding a case-folding character -- are
handled per line, so a 220MB store still scans at byte speed everywhere
it can.

Which record kinds count as conversation content is _literal_texts: what
was said, what a tool call carried, and what it returned -- the same
categories the per-tool snippet readers reach, and deliberately not
session metadata or injected instructions.

Nothing here touches the sessions hub: the orchestration (which files
belong to a session, and which sessions to scan) stays in
sessions.literal_matches, whose namespace is where the tests aim their
patches. This module is reachable as sessions.<name> through the hub's
re-exports.
"""
import json
import re

from . import common
from .codebuddy import codebuddy_record_texts
from .codex import CODEX_BOILERPLATE_PREFIXES, _codex_tool_call_text
from .step import _step_tool_call_text


def _literal_pattern(topic):
    # Short names and acronyms should not match ordinary longer words:
    # "cra" in "craft" was making almost every session an exact match.
    if len(topic) <= 3 and topic.isascii() and topic.isalpha():
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(topic) + r"(?![A-Za-z0-9])", re.I)
    return re.compile(re.escape(topic), re.I)


def _literal_texts(tool, entry):
    """Search conversation content, excluding metadata and injected context."""
    kind = entry.get("type") if isinstance(entry, dict) else None
    if tool == "claude" and kind in ("user", "assistant"):
        content = common.dict_field(entry, "message").get("content")
        if isinstance(content, str):
            return [content]
        if not isinstance(content, list):
            return []
        return [common.block_text(block) for block in content
                if isinstance(block, dict) and block.get("type") in ("text", "thinking")]
    if tool == "codex" and kind == "response_item":
        payload = common.dict_field(entry, "payload")
        ptype = payload.get("type")
        if ptype == "message" and payload.get("role") in ("user", "assistant"):
            texts = common._all_text_blocks(payload.get("content"), ("input_text", "text", "output_text"))
            return [t for t in texts if not t.strip().startswith(CODEX_BOILERPLATE_PREFIXES)]
        if ptype in ("function_call", "custom_tool_call"):
            return [_codex_tool_call_text(payload)]
        if ptype in ("function_call_output", "custom_tool_call_output"):
            return [payload.get("output", "")]
    if tool == "step":
        if kind != "message":
            return []
        message = common.dict_field(entry, "message")
        if message.get("role") not in ("user", "assistant", "toolResult"):
            return []
        texts = []
        for block in common.list_field(message, "content"):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(common.block_text(block))
            elif btype == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str):
                    texts.append(thinking)
            elif btype == "toolCall":
                texts.append(_step_tool_call_text(block))
        return texts
    if tool in ("codebuddy", "workbuddy"):
        # One function covers every record kind the scan wants, because the
        # snippet reader shares it: messages, calls, and call results.
        # WorkBuddy is the same engine's transcript under a different root.
        return codebuddy_record_texts(entry, with_tool_output=True)
    if tool == "kimi":
        if kind == "turn.prompt":
            return common._all_text_blocks(common.list_field(entry, "input"))
        if kind == "context.append_loop_event":
            event = common.dict_field(entry, "event")
            if event.get("type") == "content.part":
                part = common.dict_field(event, "part")
                text = next((v for v in (part.get("text"), part.get("think"))
                             if isinstance(v, str)), "")
                return [text]
            if event.get("type") == "tool.result":
                output = common.dict_field(event, "result").get("output", "")
                return [output if isinstance(output, str) else ""]
    return []


LITERAL_SCAN_LIMIT = 20000

# The only two characters in all of Unicode whose lowercase contains an ASCII
# letter (enumerated over the whole range, not guessed): U+0130 LATIN CAPITAL
# LETTER I WITH DOT ABOVE and U+212A KELVIN SIGN. A line holding one of them
# can match an ASCII topic -- "k" against a Kelvin sign -- without the topic's
# bytes appearing anywhere in the line, so a cut taken on raw bytes has to let
# that line through to be decoded and judged by the real pattern instead.
#
# Both spellings count: raw UTF-8, and the escaped code point these files use
# for non-ASCII -- which is how a folding character most often appears, and
# which hides it from a test taken on the raw bytes.
CASE_FOLDING_UTF8 = (bytes.fromhex("c4b0"), bytes.fromhex("e284aa"))
CASE_FOLDING_ESCAPED = (bytes.fromhex("5c7532313261"), bytes.fromhex("5c75313330"))


def _holds_case_folder(buf):
    """True if `buf` holds a character whose lowercase is an ASCII letter, in
    either spelling. Four plain byte sequences tested with `in` rather than one
    regex: an alternation over a megabyte of dense CJK costs more than twice
    as much."""
    return (any(seq in buf for seq in CASE_FOLDING_UTF8)
            or any(seq in buf for seq in CASE_FOLDING_ESCAPED))


def _bytes_needle(topic):
    """The topic as a lowercased ASCII byte string, or None for a non-ASCII
    one -- see _file_contains for what a cut on raw bytes has to watch out
    for."""
    return topic.lower().encode("utf-8") if topic.isascii() else None


NEWLINE = 10


def _scan_lines(fh, chunk_size=1 << 20):
    """Yield (raw_line, cut_is_safe) for a binary transcript, undecoded.

    Splitting on b"\\n" and decoding per line rather than decoding the whole
    file: a UTF-8 multi-byte sequence never contains 0x0A (continuation bytes
    are 0x80-0xBF and lead bytes 0xC0 and up), so no character straddles the
    boundary and the two orders agree.

    cut_is_safe is False for any chunk holding a case folder: those lines are
    decoded and parsed rather than cut."""
    carry = b""
    while True:
        chunk = fh.read(chunk_size)
        if not chunk:
            break
        buf = carry + chunk
        safe = not _holds_case_folder(buf)
        lines = buf.split(bytes((NEWLINE,)))
        carry = lines.pop()  # the trailing piece is not a complete line yet
        for line in lines:
            yield line, safe
    if carry:
        yield carry, not _holds_case_folder(carry)


def _file_contains(path, pattern, tool=None, needle=None, limit=LITERAL_SCAN_LIMIT):
    try:
        with open(path, "rb") as fh:
            for i, (raw, safe) in enumerate(_scan_lines(fh)):
                if i > limit:
                    break
                if needle is not None and safe:
                    # An ASCII topic is literal in the line whether or not the
                    # rest of it is JSON-escaped, and a safe line cannot hold a
                    # case variant the cut would miss, so its bytes answer the
                    # question -- at roughly a third of the cost of decoding.
                    if needle not in raw.lower():
                        continue
                # Everything else is parsed rather than cut. A non-ASCII
                # topic, because these files write non-ASCII as escaped code
                # points, so the topic is then not a substring of the raw line
                # even when the line is entirely about it. And an unsafe line,
                # because there the topic can match through a case fold
                # without appearing in the line at all, so no test taken on
                # the line -- bytes or decoded -- can answer.
                if tool is None:  # kimi background task output.log
                    texts = [raw.decode("utf-8", "replace")]
                else:
                    # A mid-write or otherwise unparseable line is skipped, as
                    # everywhere else that reads these files.  That is the only
                    # failure mode left to swallow here: _literal_texts is
                    # type-guarded, so an exception from it would be a real bug
                    # rather than dirty data -- and swallowing real bugs is how
                    # a session silently vanished from search results.
                    try:
                        entry = json.loads(raw)
                    except (ValueError, RecursionError):
                        continue
                    texts = _literal_texts(tool, entry)
                if any(isinstance(t, str) and pattern.search(t) for t in texts):
                    return True
    except OSError:
        pass
    return False
