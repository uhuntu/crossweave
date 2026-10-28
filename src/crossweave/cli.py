"""cw - unified wrapper for the claude / codex / kimi / step / codebuddy
CLIs, plus the zcode and workbuddy desktop apps' session stores. Normalizes a
handful of common flags across the terminal tools and passes everything else
straight through; zcode has no terminal interface, so `cw zcode` only opens
the app, and workbuddy has no interface at all -- its sessions are listed,
searched and resumed, with nothing to wrap.
"""
import json
import os
import re
import sys

from . import __version__, alias, delete, doctor, machines, note, rename, search, sessions, ui, update

TOOLS = ("claude", "codex", "kimi", "step", "zcode", "codebuddy", "workbuddy")

# USAGE is an f-string built from TOOLS, search.JUDGE_CMD and
# search.DEFAULT_JUDGE for the same reason SUBCOMMANDS exists below: the
# search line hardcoded [--judge claude|codex|kimi] and so advertised a judge
# list that was wrong for the two commits after step joined it, the yolo table
# and the examples named three tools when there were four, and "asks claude"
# kept naming a judge that was no longer the default. Nothing here spells out
# something the code can say.
USAGE = f"""Usage: cw <{'|'.join(TOOLS)}> [common-options] [prompt] [-- extra native args]
       cw sessions [--tool T] [--limit N|all] [--cwd] [--all]
       cw full [--tool T] [--cwd] [--all]
       cw search <topic> [--tool {'|'.join(TOOLS)}] [--judge {'|'.join(search.JUDGE_CMD)}] [--why] [--all]
       cw resume <{'|'.join(TOOLS)}|N> [session-id-or-prefix] [--cwd <dir>]
       cw <N> <{'|'.join(TOOLS)}> [native-options]
       cw <N> delete [-y]      # remove session files, keeping a backup
       cw delete <N> [-y]
       cw <N> rename <title>    # row first, the shape `cw <N> <tool>` reads in
       cw <N> rename             # no title: the judge proposes one, and it is applied
       cw update [tools|all]
       cw stats [--tool T]
       cw doctor [--tool T]
       cw alias add <name>... | list | remove <name>...
       cw machines           # the ssh endpoint: prints this machine's index JSON
       cw rename <N> <title> [--judge T]
       cw rename --auto [<N>... | --trivial] [-y] [--judge T]
       cw rename --trivial              # the sweep -- --auto implied
       cw note <N|session-id>... [--vault DIR] [--judge T]
       cw <N> note              # summarize a session into an Obsidian vault (CW_VAULT)
       cw note --all [--since 7d] [-y]   # every session with no note, or a stale one
       cw note --index          # rebuild each project's _index.md from its notes
       cw context [DIR] [--limit N]   # print that project's newest notes, for a new session
       cw ui                  # pick a session: type to filter, Enter resumes, h/n/r/d hand off/note/rename/delete

Common options (translated per-tool, all optional):
  -p, --print              Non-interactive: print response and exit
  -c, --continue           Continue the most recent session in this directory
  -m, --model <model>      Model to use
  --add-dir <dir>          Additional workspace directory (repeatable)
  -y, --yolo               Auto-approve tool calls (per-tool semantics differ,
                            see notes below)
  --no-context             Don't attach this project's vault notes (see below)

Anything after a literal `--`, or any flag this wrapper doesn't recognize,
is passed through unchanged to the underlying CLI.

The listing (`cw`, `cw sessions`) marks the rows belonging to the current
directory and shows each session's turn count. Color is used only when the
output is a terminal: NO_COLOR, TERM=dumb and CROSSWEAVE_COLOR=never disable
it, CROSSWEAVE_COLOR=always forces it. A bare `cw` lists 15 rows; set
CW_LIMIT (a number or `all`) to change that.

With CW_VAULT set, a fresh interactive `cw claude|codex|step|codebuddy`
starts with this project's newest notes (what `cw context` prints) appended
to the tool's system prompt -- background, no message sent. Not with -p or
-c, nor before a subcommand; --no-context skips it once, CW_CONTEXT=off
for good. kimi has no way to take it.

Per-tool --yolo mapping:
  claude    -> --dangerously-skip-permissions
  codex     -> --approve-for-me   (auto-approve, still sandboxed)
  kimi      -> -y/--yolo
  step      -> --approval-mode auto + --non-interactive-approval allow
  codebuddy -> -y, --dangerously-skip-permissions (HIGH/CRITICAL still ask)
  zcode     -> (desktop app; no flags apply)
  workbuddy -> (desktop app, no CLI; no flags apply)

Examples:
  cw claude -p "summarize this repo"
  cw codex -p -m o3 "fix the failing test"
  cw kimi -c
  cw step -p "summarize this repo"
  cw codebuddy -p "summarize this repo"
  cw zcode            # open the ZCode app on this directory (its sessions
                      #   show in `cw sessions` too)
  cw resume workbuddy 3f57c3fb   # open a WorkBuddy session in its app
  cw claude -- --agent reviewer "look at this diff"
  cw sessions --limit 10
  cw sessions --limit all   # same as `cw full`
  cw full                   # everything, no default 15-row cutoff
  CW_LIMIT=30 cw            # bare `cw` shows 30 rows (export it to keep)
  cw sessions --machines    # other machines' sessions too -- each remote
                            #   row names the machine it lives on; the
                            #   switch is the machines dir itself: sync
                            #   ~/.config/crossweave/machines
  cw sessions --machines 241 149   # or pull fresh over ssh -- targets are
                            #   ssh destinations (config aliases work); the
                            #   remote needs this version's cw first
  cw resume kimi 97946bc7
  cw resume 3         # resume row 3 from the last `cw`/`cw sessions` listing
  cw resume 2 --cwd /path/to/other-project   # can't relocate row 2 in place -- hands off to a fresh session there
  cw 3 codex          # continue row 3 in a new Codex session with context
  cw update           # update crossweave itself
  cw update tools     # update claude, codex, kimi, and step (whichever are
                      #   installed; zcode updates itself)
  cw update all       # both of the above
  cw search "the nfc frequency lock issue"   # asks {search.DEFAULT_JUDGE} (the default judge)
  cw search "katago" --judge codex   # judge with a different tool
  cw search "katago" --why           # each hit's reason, in full
  cw stats            # session counts per tool, oldest/newest, top directories
  cw doctor           # per-tool store health: is each store readable, and
                      #   how many sessions is it actually yielding
  cw alias add xx     # personal command names -- launchers pointing at this
                      #   install; `cw alias add` with no names restores every
                      #   name in the manifest (the fresh-machine move)
  cw rename 12 "USB 枚举握手排查"   # rename row 12 in the store itself --
                            #   zcode/codex/claude/step/kimi today; --auto
                            #   proposes titles for the trivial ones via the judge
  cw 12 rename "USB 枚举握手排查"   # the same command, row first
  CW_VAULT=~/Notes cw note 3   # row 3 as a done/why/open note in the vault,
                            #   one folder per project; again rewrites it
  cw claude "$(cw context) 接着做"   # start a session on top of this project's notes
  cw 327e2ad9-210f-48cd-b    # a session id, not a row number: resumes it
"""

# Everything `cw <x>` accepts that is not a tool. The unknown-tool error names
# this list, and the test asserts it matches what main() actually dispatches,
# so adding a subcommand cannot leave the message stale the way it was when
# `stats` and `step` were both missing from it.
SUBCOMMANDS = ("sessions", "full", "search", "resume", "update", "stats", "doctor", "alias", "machines", "rename", "delete", "note", "context", "ui")


def usage(name="cw"):
    """USAGE, naming the command it was invoked as.

    `cw` is what the project ships and what every message says, but it is not
    the only way in: `cx` is the mark's own name, the ai/aim/aimux/clisweave
    links are the earlier names' trail, and `cw alias add` mints personal
    names -- all of them the same main(). `cx --help` answering "Usage: cw
    ..." reads as though the command does not exist, so the word is swapped
    for the one that was typed. Only the standalone word is touched, never a
    substring of one, and a name that is not a command at all (`__main__.py`
    when the package is run directly) falls back to cw.
    """
    if not name or name == "cw" or "." in name:
        return USAGE
    return re.sub(r"\bcw\b", name, USAGE)


class UsageError(Exception):
    """Bad arguments to `cw <tool> ...`. Caught by main() and reported
    cleanly; kept separate from sys.exit so build_command stays a pure,
    testable function."""


def build_command(tool, rest):
    """Normalize `rest` (the args after the tool name) into the native
    command to run. Pure function, no I/O — raises UsageError on bad
    input instead of exiting, so it's easy to unit test."""
    if tool not in TOOLS:
        raise UsageError(f"unknown tool '{tool}' (expected one of {', '.join(TOOLS)}, "
                        f"{', '.join(SUBCOMMANDS)})")

    print_ = False
    continue_session = False
    model = None
    add_dirs = []
    yolo = False
    trailing = []

    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-p", "--print"):
            print_ = True
            i += 1
        elif a in ("-c", "--continue"):
            continue_session = True
            i += 1
        elif a in ("-m", "--model"):
            if i + 1 >= len(rest):
                raise UsageError("--model requires a value")
            model = rest[i + 1]
            i += 2
        elif a == "--add-dir":
            if i + 1 >= len(rest):
                raise UsageError("--add-dir requires a value")
            add_dirs.append(rest[i + 1])
            i += 2
        elif a in ("-y", "--yolo"):
            yolo = True
            i += 1
        elif a == "--":
            trailing.extend(rest[i + 1:])
            break
        else:
            trailing.append(a)
            i += 1

    cmd = []
    if tool == "claude":
        cmd = ["claude"]
        if print_:
            cmd.append("-p")
        if continue_session:
            cmd.append("--continue")
        if model:
            cmd += ["--model", model]
        for d in add_dirs:
            cmd += ["--add-dir", d]
        if yolo:
            cmd.append("--dangerously-skip-permissions")
    elif tool == "codex":
        if print_:
            cmd = ["codex", "exec"]
            if continue_session:
                cmd += ["resume", "--last"]
        else:
            cmd = ["codex"]
        if model:
            cmd += ["-m", model]
        for d in add_dirs:
            cmd += ["--add-dir", d]
        if yolo:
            cmd.append("--approve-for-me")
    elif tool == "step":
        cmd = ["step"]
        if print_:
            cmd.append("-p")
        if continue_session:
            cmd.append("-c")
        if model:
            cmd += ["--model", model]
        if add_dirs:
            # step has no per-workspace directory flag of its own; the closest
            # it has is --session-dir, which is where sessions live rather
            # than what to work on.
            raise UsageError("--add-dir is not supported for step")
        if yolo:
            # step's approval modes are confirm/auto/strict, and "auto" is the
            # auto-approve one. With no approval UI (a -p run) approval falls
            # back to --non-interactive-approval, which denies by default, so
            # -y has to cover both.
            cmd += ["--approval-mode", "auto", "--non-interactive-approval", "allow"]
    elif tool == "codebuddy":
        # codebuddy is a claude-code derivative and maps one-to-one, with
        # two differences: its --add-dir is ONE variadic flag (commander
        # consumes every value after it until the next flag, so the dirs go
        # behind a single occurrence), and -p takes the prompt positionally
        # like claude's does.
        cmd = ["codebuddy"]
        if print_:
            cmd.append("-p")
        if continue_session:
            cmd.append("-c")
        if model:
            cmd += ["--model", model]
        if add_dirs:
            cmd.append("--add-dir")
            cmd += add_dirs
        if yolo:
            # -y, --dangerously-skip-permissions: verified against
            # `codebuddy --help` (2.161.4) -- the same flag claude takes.
            cmd.append("--dangerously-skip-permissions")
    elif tool == "zcode":
        # A desktop app plus a terminal CLI answer to the name `zcode`. With
        # the CLI on PATH, `cw zcode` opens its TUI rooted in the directory
        # you are standing in (its /resume picker lists this directory's
        # sessions); with only the desktop app, it goes through the
        # workspace deep link -- the only route that names a directory.
        # Either way no wrapper flag applies.
        if print_ or continue_session or model or add_dirs or yolo or trailing:
            raise UsageError("zcode takes no wrapper flags -- run `zcode` directly")
        if sessions.zcode_cli_on_path():
            cmd = ["zcode"]
        else:
            cmd = ["zcode", sessions.zcode_workspace_link(os.getcwd())]
    elif tool == "workbuddy":
        # A desktop app with no executable on PATH to hand a flag to, and no
        # deep link that names a directory -- its scheme routes to a session
        # or to the app's home, neither of which is "work in this directory".
        # So there is no `cw workbuddy` that could mean anything, and saying
        # what does exist beats an unknown-tool list of seven names.
        raise UsageError(
            "workbuddy is a desktop app with no CLI on PATH -- its sessions are in "
            "`cw sessions --tool workbuddy`, and `cw resume workbuddy <id>` opens one")
    else:  # kimi
        cmd = ["kimi"]
        if print_:
            if not trailing:
                # claude and codex both accept `-p` with the prompt on
                # stdin; kimi's -p does not read stdin at all, so a bare
                # `-p` here can only be a mistake (it used to reach kimi as
                # a value-less option, and in a handoff it collided with the
                # seed's own -p). --model/--add-dir already reject a missing
                # value the same way.
                raise UsageError("-p requires a prompt (kimi's -p does not read stdin)")
            cmd.append("-p")
        if continue_session:
            cmd.append("-c")
        if model:
            cmd += ["-m", model]
        for d in add_dirs:
            cmd += ["--add-dir", d]
        if yolo:
            cmd.append("-y")

    cmd += trailing
    return cmd


# How each tool takes background it should know without being asked
# anything: appended to its system prompt, so starting a session sends no
# message and costs no turn. kimi has only --agent-file, which replaces the
# whole agent, and zcode/workbuddy take no flags -- none of them get it.
CONTEXT_FLAGS = {
    "claude": lambda text: ["--append-system-prompt", text],
    "codebuddy": lambda text: ["--append-system-prompt", text],
    "step": lambda text: ["--append-system-prompt", text],
    # -c values are TOML; a JSON string is a valid TOML basic string
    "codex": lambda text: ["-c", "developer_instructions=" + json.dumps(text, ensure_ascii=False)],
}
# Flags after which a launch is not a fresh interactive session: a script's
# one-shot run should not change with the vault, and a continued session
# already holds what it knew.
_NO_CONTEXT_FLAGS = ("-p", "--print", "-c", "--continue")
# The user's own system-prompt flags: theirs win, ours stay out.
_OWN_PROMPT_FLAGS = ("--system-prompt", "--append-system-prompt", "developer_instructions")
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z-]*$")


def strip_no_context(rest):
    """(rest without --no-context, whether it was there). Only the wrapper's
    own part counts: after `--` everything belongs to the tool."""
    out, off = [], False
    for i, a in enumerate(rest):
        if a == "--":
            out += rest[i:]
            break
        if a == "--no-context":
            off = True
        else:
            out.append(a)
    return out, off


def wants_context(tool, rest):
    """Whether `cw <tool> <rest>` is a fresh interactive session that the
    project's notes should be attached to."""
    if tool not in CONTEXT_FLAGS:
        return False
    if os.environ.get("CW_CONTEXT", "").strip().lower() in ("0", "off", "no", "false"):
        return False
    own = rest[:rest.index("--")] if "--" in rest else rest
    if any(a in _NO_CONTEXT_FLAGS for a in own):
        return False
    if any(flag in a for a in rest for flag in _OWN_PROMPT_FLAGS):
        return False
    # a bare lowercase word first is a subcommand (`cw claude doctor`,
    # `cw codex login`), not a prompt -- the flags would only get in its way
    positional = [a for a in own if not a.startswith("-")]
    if positional and positional[0] == own[0] and _SUBCOMMAND_RE.match(own[0]):
        return False
    return True


def attach_context(tool, cmd):
    """cmd with the project's notes as background, plus one stderr line
    saying so; cmd unchanged when there are none."""
    got = note.project_context(os.getcwd())
    if got is None:
        return cmd
    project, count, text = got
    print(f"cw: {count} note{'s' if count != 1 else ''} from {project} attached as "
          "background (--no-context skips, CW_CONTEXT=off for good)", file=sys.stderr)
    return [cmd[0], *CONTEXT_FLAGS[tool](text), *cmd[1:]]


DEFAULT_LIST_LIMIT = "15"


def default_list_limit():
    """How many rows a bare `cw` shows: CW_LIMIT (a number or `all`) when it
    is set to something usable, else 15."""
    raw = os.environ.get("CW_LIMIT", "").strip()
    if not raw:
        return DEFAULT_LIST_LIMIT
    if raw.lower() == "all" or (raw.isdigit() and int(raw) > 0):
        return raw.lower()
    print(f"cw: ignoring CW_LIMIT={raw!r} (expected a positive number or 'all')", file=sys.stderr)
    return DEFAULT_LIST_LIMIT


def main():
    # Before anything prints: a title the console's code page cannot encode
    # must not abort the command (see harden_console_output).
    sessions.harden_console_output()

    argv = sys.argv[1:]

    if argv and argv[0] in ("-h", "--help"):
        print(usage(os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""))
        return
    if argv and argv[0] in ("-v", "--version"):
        print(f"crossweave {__version__}")
        return

    if not argv:
        sessions.cmd_list(["--limit", default_list_limit()])
        return

    tool, rest = argv[0], argv[1:]

    if tool == "sessions":
        sessions.cmd_list(rest)
        return
    if tool == "full":
        # `cw full` is shorthand for `cw sessions --limit all`.
        sessions.cmd_list(["--limit", "all", *rest])
        return
    if tool == "resume":
        sessions.cmd_resume(rest)
        return
    if sessions.ROW_NUMBER_RE.match(tool):
        # `cw <N>` is shorthand for `cw resume <N>`.
        if rest and rest[0] == "delete":
            sys.exit(delete.cmd_delete([tool, *rest[1:]]))
        if rest and rest[0] == "rename":
            # `cw 3 rename "title"` -- the row first, the shape `cw 3 codex`
            # already reads in, so pointing at a listing row and saying what
            # to do with it is one grammar rather than two. The word has to be
            # lifted out here: cmd_rename takes the row as its first
            # positional, so leaving it in place would fold it into the title.
            #
            # It is not handed to the tool as a continuation prompt the way
            # every other second word after a row number is -- that reading is
            # what this costs, and it was near-worthless anyway (a session
            # continued with the single word "rename" as its instruction).
            # `cw resume 3 rename` still spells it out for anyone who meant it.
            sys.exit(rename.cmd_rename([tool, *rest[1:]]))
        if rest and rest[0] == "note":
            # `cw 3 note`, row first -- lifted out for the reason rename is
            sys.exit(note.cmd_note([tool, *rest[1:]]))
        sessions.cmd_resume(argv)
        return
    if tool == "update":
        update.cmd_update(rest)
        return
    if tool == "search":
        search.cmd_search(rest)
        return
    if tool == "stats":
        sessions.cmd_stats(rest)
        return
    if tool == "doctor":
        sys.exit(doctor.cmd_doctor(rest))
    if tool == "alias":
        sys.exit(alias.cmd_alias(rest))
    if tool == "machines":
        # The ssh endpoint of the pull mode: `ssh <host> cw machines` runs
        # this and ships the JSON back to the caller's `--machines`.
        sys.exit(machines.cmd_machines(rest))
    if tool == "delete":
        sys.exit(delete.cmd_delete(rest))
    if tool == "rename":
        sys.exit(rename.cmd_rename(rest))
    if tool == "note":
        sys.exit(note.cmd_note(rest))
    if tool == "context":
        sys.exit(note.cmd_context(rest))
    if tool == "ui":
        sys.exit(ui.cmd_ui(rest))

    # A session id, not a tool. `cw search` prints ids, and the command after
    # finding a session is pointing cw straight at it -- which used to fall
    # through to build_command and answer "unknown tool '327e2ad9-…'", a list
    # of tools for a thing that is not a tool, with no mention that ids go
    # through resume. One hit resumes it; several are listed, because a bare
    # prefix has no tool to narrow it; none falls through to the tool error
    # below, so a mistyped command still gets the list of real ones.
    #
    # Two things keep this off the paths that are not about an id. A tool name
    # never reaches it -- those are build_command's, and asking six stores
    # whether any id starts with "claude" would tax every `cw claude ...` for
    # an answer that is always no. And an argument with no digit, dash or
    # underscore in it is a word, not an id: every id in every store is hex
    # or carries a sess_/session_ prefix, while `cw session` is a typo of
    # `cw sessions` and deserves that message rather than a list of every
    # kimi session on the box.
    if tool not in TOOLS and re.search(r"[0-9_-]", tool):
        hits = sessions.resolve_any_id(tool)
        if len(hits) == 1:
            sessions.cmd_resume([hits[0][0], hits[0][1], *rest])
            return
        if len(hits) > 1:
            print(f"cw: ambiguous id '{tool}' -- {len(hits)} sessions start with it:",
                  file=sys.stderr)
            for t, sid in hits[:10]:
                print(f"  {t} {sid}", file=sys.stderr)
            if len(hits) > 10:
                print(f"  ... and {len(hits) - 10} more; narrow it with "
                      f"`cw resume <tool> {tool}`", file=sys.stderr)
            sys.exit(1)

    rest, no_context = strip_no_context(rest)
    try:
        cmd = build_command(tool, rest)
    except UsageError as e:
        print(f"cw: {e}", file=sys.stderr)
        sys.exit(1)

    if not no_context and wants_context(tool, rest):
        cmd = attach_context(tool, cmd)
    sessions.exec_or_die(cmd)


if __name__ == "__main__":
    main()
