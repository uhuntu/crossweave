"""Reader for pi's session store (earendil-works/pi, the `pi` coding agent).

step is a pi derivative, and the two write the same session files: one
JSONL per session under ~/.pi/agent/sessions/--<encoded-cwd>--/, named
`<timestamp>_<id>.jsonl`, opening with a `session` header that carries the
id and cwd, then `message` entries (user / assistant / toolResult, with
text, thinking and toolCall blocks) and `session_info` records holding a
name set with `pi --name`, /name or `cw rename`. So every reader here is
step's, pointed at pi's directory -- see step.tree_store_records.

The store moves with $PI_CODING_AGENT_SESSION_DIR, or with
$PI_CODING_AGENT_DIR (the whole config directory, sessions under it).
settings.json's `sessionDir` can move it too; that one is not followed.
"""
import os

from . import common, step


def _default_sessions_dir():
    explicit = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if explicit:
        return explicit
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(common.HOME, ".pi", "agent")
    return os.path.join(agent_dir, "sessions")


PI_SESSIONS = _default_sessions_dir()


def pi_light_records(show_all=False):
    return step.tree_store_records(PI_SESSIONS, "pi", show_all)


def pi_resolve(prefix):
    return sorted({r["id"] for r in pi_light_records(show_all=True) if r["id"].startswith(prefix)})


def pi_session_cwd(sid):
    for r in pi_light_records(show_all=True):
        if r["id"] == sid:
            return r["cwd"]
    return None
