"""Turn a real session file into a shape-preserving, blinded fixture.

    python tests/capture_fixture.py <tool> <transcript.jsonl> [more.jsonl ...]

Writes tests/fixtures/<tool>/<basename> plus a `<basename>.json` manifest
recording what the readers yielded at capture time. The harness
(test_format_fixtures.py) replays every fixture through its tool's
readers and holds them to those numbers, so a reader regression -- or a
hand-edit of the reader's assumptions -- shows up as a fixture failure
rather than a silently different listing.

The blinding walks each parsed record and replaces every string value
whose key is not structural (type, role, sequence, ids, timestamps) with
same-length runs of "x": the byte shape survives, the content does not.
That is a mechanical best effort, not a guarantee -- READ THE DIFF
BEFORE COMMITTING. A fixture that still carries a recognizable cwd, a
title, or a sentence of anyone's conversation does not belong in the
repository, and zcode is not capturable here at all (its store is
SQLite, and `cw doctor --tool zcode` is the drift check that format
gets).

Not collected by pytest (the name does not start with test_); nothing in
the suite runs this for you, on purpose.
"""
import json
import os
import sys

TOOLS = ("claude", "codex", "kimi", "step", "codebuddy", "workbuddy")

# Keys whose string values are structure rather than content. Everything
# else a string rides on is blinded -- biased toward over-blinding, since
# a fixture with its `type`s destroyed is merely useless, while one that
# kept a cwd or a message body is a leak.
STRUCTURAL_KEYS = {
    "type", "role", "sequence", "id", "sessionId", "parent_id",
    "timestamp", "time_created", "time_updated", "createdAt", "updatedAt",
    "titleKind", "isCustomTitle", "archived",
}


def blind(value, key=None):
    if isinstance(value, dict):
        return {k: blind(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [blind(v, key) for v in value]
    if isinstance(value, str) and key not in STRUCTURAL_KEYS:
        return "x" * len(value)
    return value


def main(argv):
    if len(argv) < 3 or argv[1] not in TOOLS:
        print(__doc__)
        return 2
    tool, paths = argv[1], argv[2:]
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "fixtures", tool)
    os.makedirs(out_dir, exist_ok=True)

    for path in paths:
        if not os.path.isfile(path):
            print(f"skip (not a file): {path}")
            continue
        records = 0
        lines = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    lines.append(line)  # keep dirty lines: tolerance is part of the contract
                    continue
                records += 1
                lines.append(json.dumps(blind(record), ensure_ascii=False))
        name = os.path.basename(path)
        out_path = os.path.join(out_dir, name)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
        manifest = {"tool": tool, "lines": len(lines), "records": records}
        with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        print(f"{out_path}  ({records} records of {len(lines)} lines)")
        print(f"  review before committing: {out_path} and its .json manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
