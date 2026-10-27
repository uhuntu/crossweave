# crossweave

<!-- X#: the crosshatch mark. X is cross; # is a weave of its own — two
     warps, two wefts — the one ASCII character that is literally the
     pattern crossweave names. Kept as a comment: # has no legal standing
     in a package name (PEP 503) or a repo name, and the trailing-sharp
     namespace reads as a language (C#, F#) — which "What this isn't"
     disclaims. A logo, not a name; the h3 heading above has been the
     mark all along. -->

[![test](https://github.com/uhuntu/crossweave/actions/workflows/test.yml/badge.svg)](https://github.com/uhuntu/crossweave/actions/workflows/test.yml)

A tiny, dependency-free wrapper that weaves five AI coding-agent CLIs — [Claude Code](https://claude.com/product/claude-code), [OpenAI Codex CLI](https://github.com/openai/codex), [Kimi CLI](https://www.kimi-cli.com/), [step](https://github.com/stepfun-ai/Step-Code), and CodeBuddy's CLI — behind one set of flags, plus cross-tool session discovery, resume, and handoff — and reads two desktop apps' session stores alongside them, ZCode's and WorkBuddy's, so those sessions show up in the same listing, search, and handoff flow and go back into the app that owns them.

No daemon, no config file, no build step — just a small Python package (`src/crossweave/`) that reads each tool's own on-disk session store directly. Formerly named `clisweave` (and `aimux` before that); the `cw` command was never renamed — `cx` (the mark's own name: X is cross) now ships alongside it.

![crossweave demo: a unified session list across claude/codex/kimi/step/zcode, then an LLM-judged topic search narrowing it down to the one relevant session](assets/demo.gif)

## Install

**Via pip** (the package is named `crossweave` on PyPI; the commands installed are `cw`, `cx`, `crossweave` and `cb` (see [`cb`](#cb-codebuddy), for CodeBuddy), plus the legacy `clisweave`, `ai`, `aim` and `aimux` aliases kept so existing scripts and links keep working, and a legacy `ai-sessions` that is no longer advertised — an active PyPI package of the same name owns that word, and `cw sessions` / `cw resume` is the same surface):

```bash
pip install crossweave
```

*XWeave* is this project's reserved alternate spelling — [`pip install xweave`](https://pypi.org/project/xweave/) installs crossweave (the [`packaging/xweave/`](packaging/xweave) forwarding package holds the name; its version mirrors crossweave's).

**Via curl** (macOS/Linux, or Windows with Git Bash/WSL), one line, no manual clone:

```bash
curl -fsSL https://raw.githubusercontent.com/uhuntu/crossweave/master/install.sh | bash
```

**Via irm** (Windows PowerShell, no Git Bash/WSL/Cygwin needed):

```powershell
irm https://raw.githubusercontent.com/uhuntu/crossweave/master/install.ps1 | iex
```

**Via git**, if you'd rather clone it yourself first:

```bash
git clone https://github.com/uhuntu/crossweave.git
cd crossweave && ./install.sh        # Windows PowerShell: .\install.ps1
```

Whichever of the last three you use, it clones the repo to `~/.local/share/crossweave` first (override with `CROSSWEAVE_REPO_DIR`, or the source with `CROSSWEAVE_REPO_URL` — a fork or a mirror), then wires up `cw`, `cx`, `cb`, `crossweave`, and the `clisweave`/`aim`/`aimux` compatibility aliases in `~/.local/bin` (override with `CROSSWEAVE_BIN_DIR`) — as symlinks on `install.sh`, or native `.cmd` launchers on `install.ps1`. Existing `ai` and `ai-sessions` links keep working, but a fresh install creates neither (`ai` is too generic a name to own, and an active PyPI package owns the `ai-sessions` word; `cw sessions` / `cw resume` is the same surface). To avoid taking over an unrelated command, the standalone installers skip `cw`, `cx` and `cb` with a warning if any already exists. The old `CLISWEAVE_REPO_DIR`/`CLISWEAVE_BIN_DIR` and `AIMUX_REPO_DIR`/`AIMUX_BIN_DIR` variables remain accepted for compatibility. Nothing is copied — the clone stays the source of truth.

Requires `claude`, `codex`, `kimi`, `step` and/or `codebuddy` already installed and on `PATH` (only the ones you actually use need to be present). ZCode needs nothing on `PATH` for its sessions to be *listed* — its SQLite store under `~/.zcode/cli/db` is read directly — but the full experience (`cw zcode`, `cw resume zcode <id>`, handing context *to* zcode) wants the `zcode` CLI on PATH; the desktop app alone still lists sessions and gets the workspace deep link. WorkBuddy needs nothing at all: it puts no executable on `PATH`, and the CLI it bundles runs only inside its own app bundle, so `cw workbuddy` is refused while `cw sessions --tool workbuddy` lists its sessions and `cw resume workbuddy <id>` opens the app that owns one.

> **Windows note:** running the curl one-liner from PowerShell/cmd (rather than Git Bash) can invoke the WSL `bash` launcher by mistake instead of Git's — use `irm` above, or run curl from Git Bash directly. `install.sh` also copes if Git Bash lacks symlink privilege (falls back to a generated launcher instead of a broken copy) or `python3` on `PATH` is the Microsoft Store's no-op stub (probes `python`/`py -3` instead). Files installed by `install.sh` are still extensionless with a shebang line, though, which PowerShell can't execute directly — `install.ps1`'s `.cmd` launchers don't have that problem. If you stick with `install.sh`, call `cw` from Git Bash instead, or add a function to your PowerShell `$PROFILE`:
> ```powershell
> function cw { & "C:\Path\To\python.exe" "$HOME\.local\share\crossweave\bin\cw" @args }
> ```


## Update

```bash
cw update        # update crossweave itself
cw update tools  # update claude, codex, kimi, and step (whichever are installed)
cw update all    # both
```

`cw update` detects how crossweave itself was installed and does the right thing: `git pull --ff-only` for a curl/git install, `pip install --upgrade crossweave` for a pip install.

`cw update tools` runs each CLI's own update command (`claude update`, `codex update`, `kimi update --yes`), skipping any that aren't installed. If one fails, the others still run; the exit code reflects the worst failure. ZCode and WorkBuddy are not attempted — both are desktop apps that update themselves — and CodeBuddy is an npm package you update through your package manager.

Two fallbacks exist for flaky networks, both confirmed live on 2026-09-24:

- **Retries**: `claude` (no fallback mirror; can hit its own internal download deadline) and `kimi` (its update check intermittently hangs at connect time) are retried on failure.
- **Proxy fallback**: set `CROSSWEAVE_UPDATE_PROXY` (e.g. `http://127.0.0.1:7897`) and any failed tool update retries through that proxy. The first attempt is always direct, so this only ever kicks in as a fallback. This exists because some networks sever long TLS transfers mid-flight — codex's ~146MB asset died at ~60MB on every direct attempt, resume ignored, while the same transfer through a working local proxy finished in under a minute. `kimi update` passes `--yes` so its interactive picker can't abort the run.

Equivalent manual commands for updating crossweave itself, if you'd rather:

- **pip**: `pip install --upgrade crossweave`
- **curl**: re-run the same one-liner — it fast-forwards the existing clone before relinking
- **git**: `git -C /path/to/crossweave pull` — the symlinks point straight into the repo, so this alone is enough

## Personal aliases

`cw` is the command the project ships; what your hands type is yours. `cw alias add <name>...` creates launchers in `~/.local/bin` (override with `CROSSWEAVE_BIN_DIR`) pointing at the installation you are running — `cw alias add xx ai` is the whole ritual, and the launchers follow that install through `cw update`. `cw alias list` reads the set back; `cw alias remove` undoes it. The names live in `~/.config/crossweave/aliases.json`, and `cw alias add` with no arguments re-creates every name in it — on a fresh machine: install, copy that one file over (or just remember the names), run the one command. Names have to survive every namespace a command name lives in — bin-dir filenames, PyPI-adjacent words (PEP 503), shell words (`#` starts a comment in all three major shells) — so letters, digits, `.`, `_` and `-` only; the installer's own commands (`cw`, `cx`, `crossweave`, `cb`) are refused, and a file the mechanism did not create is never overwritten. The installer itself never creates or removes personal aliases; this command is their only manager.

## Other machines

A session that happened on the laptop does not exist on the desktop — each tool's store is per-machine, and no amount of local reading can answer "where was that conversation". crossweave's answer is an index, not a service: every machine publishes a small JSON file describing its own sessions, and the files meet in a directory you sync yourself (Syncthing, git, Dropbox — anything that moves files):

```bash
mkdir -p ~/.config/crossweave/machines   # the directory's existence is the switch
# point your sync tool of choice at it, on every machine that runs cw
```

From then on every listing refreshes this machine's index in that directory (metadata only — tool, id, title, directory, timestamp; the transcripts never leave the machine they live on), and `cw sessions --machines` merges the others in: local rows first, then a second section of remote rows, each naming the machine it lives on. A row the local store also holds — mirrored stores do this — stays a plain resumable local row; a truly remote row is listed for discovery, and `cw resume <N>` / the handoff refuse it cleanly, naming the machine, instead of pretending. Without the directory nothing changes: no publish, no merge, no new behavior to learn — and `CROSSWEAVE_MACHINES_DIR` moves the directory, `CROSSWEAVE_MACHINE_NAME` overrides the name a machine publishes under. Indexes older than two weeks drop out of the merge with a note: a machine unused that long is a ghost, not a pointer.

No always-on sync tool? The pull mode needs none: `cw sessions --machines 241 149` — each target is any ssh destination (an alias from `~/.ssh/config` works as-is, jump hosts included), and cw has the remote run `cw machines`, which prints that machine's index JSON on the spot. Nothing is configured, nothing is stored. A target that is unreachable, or still runs a cw older than this feature, costs one stderr line and never takes the listing down — the remote pays one `cw update`, and a password-only host needs key auth (or a live mux master), since the pull runs BatchMode rather than hang at a login prompt. The two transports feed the same merge: a synced directory and ssh targets mix freely in one `--machines`.

## Usage

```bash
cw                          # recent sessions across all seven tools (same as `cw sessions`)
cw sessions --machines      # + other machines' sessions (a synced index dir)
cw sessions --machines 241 149   # ...or pulled fresh over ssh, no setup
cw claude -p "prompt"       # -> claude -p "prompt"
cw codex -p -m o3 "prompt"  # -> codex exec -m o3 "prompt"
cw kimi -c                  # -> kimi -c
cw step -p "summarize"      # -> step -p "summarize"
cw codebuddy -p "summarize" # -> codebuddy -p "summarize"
cw zcode                    # open the ZCode app on this directory

cw sessions --limit 10      # list recent sessions, all tools
cw sessions --limit all     # no cutoff -- same as `cw full`
cw full                     # shorthand for `cw sessions --limit all`
cw sessions --tool codex    # filter to one tool
cw sessions --cwd           # only sessions started in the current directory
cw sessions --all           # include archived sessions and ones a tool started for itself

cw resume kimi 97946bc7     # resume by short id / prefix (resolved against real session ids)
cw resume claude            # no id -> tool's own interactive picker
cw resume 3                 # resume row 3 from the last `cw`/`cw sessions` listing
cw resume zcode <id> "..."  # continue a zcode session headlessly -- its one route in that needs no TUI
cw resume workbuddy <id>    # open the WorkBuddy app on that session (deep link, no CLI needed)
cw 3 codex                  # hand row 3's context to a new Codex session
cw 327e2ad9-210f-48cd-b     # an id where the row number goes -- the same resume, no listing needed

cw search "the nfc frequency lock issue"   # find sessions relevant to a topic
cw search "katago" --tool claude           # restrict the candidates to one tool
cw search "..." --judge kimi               # use a different model to judge relevance
cw search "..." --judge step                # ... or step
cw search "..." --why                      # print each hit's reason in full, on its own line
cw search "..." --all                      # list every hit, not just the strongest 10

cw stats                    # session counts per tool, oldest/newest, top directories
cw stats --tool claude      # stats for one tool only
cw doctor                   # per-tool store health: is each store readable,
                            #   and how many sessions is it actually yielding
cw doctor --tool zcode      # one store only
```

Sessions a tool started for itself are left out of `cw sessions` and `cw search`: Codex's command-approval reviews (`Guardian review` or `codex approval review`, one per command it asks you to approve), step's own `subagent-…` sessions, ZCode's subagent runs (sessions with a `parent_id` in its store), WorkBuddy's subagent runs (an `agent-*.jsonl` under its parent's session directory), and `cw search`'s own judge runs. None of them is a conversation of yours, and a judge session literally contains every candidate's text, which makes it match nearly any topic. They stay reachable by id (`cw resume codex <id>`), and `cw sessions --all` lists them anyway.

Every `cw`/`cw sessions` listing is numbered and cached, so `cw resume <N>` is usually the fastest way in: run `cw`, glance at the row you want, `cw resume 3`. The cache is just the last listing you saw — it's overwritten by the next `cw sessions` call and doesn't try to detect if the underlying sessions changed since.

An id works wherever a row number does, and is the steadier of the two: `cw <id-prefix>` resolves the prefix against every store live, so it does not care what the last listing happened to be, and it survives sessions arriving or moving in the meantime. It is what `cw search` hands you — the ids in its output are the argument to the next command. One match resumes it (with anything after it meaning what it means after a row number: another tool hands the context off, words continue the session); several matches are listed rather than guessed at, because a bare prefix has no tool to narrow it — name one with `cw resume <tool> <prefix>`. A word with no digit, dash or underscore in it is not offered to the stores at all: `cw session` is a typo of `cw sessions` and gets that message, not a list of every kimi session on the box.

### What the listing shows

One table: the row number, the tool, how long ago the session ran, its id, the directory it belongs to, and how many turns it holds. `▸` (or `>` on a console that cannot draw it) marks the rows whose directory is the one you are standing in — the question you usually have when you type `cw` inside a project.

Under each row, dim, is where that session left off — the last thing anyone actually said in it. A title says where a conversation *started* ("fix the nfc lock"); the line under it says where it got to, which is the difference between a table and an answer to "where was I?". A trailing system reminder or a pasted log is skipped for the real last words, and a one-turn session shows nothing under its row rather than repeating its title.

The tool column carries a hue when the output is a terminal, the id/when/turns columns go dim and the header is bold, so the titles are what your eye lands on. Color is skipped when the output is a pipe or a file, when `NO_COLOR` is set, when `TERM` is `dumb`, or with `CROSSWEAVE_COLOR=never`; `CROSSWEAVE_COLOR=always` forces it on (useful for `cw | less -R`).

On a narrow terminal the directory column shrinks first and the turns column is dropped before the title loses its room. Columns are measured in display width rather than code points, so a title in Chinese keeps the table lined up — and a turn count comes from the user messages each tool records (claude's tool results are not turns).

To switch agents, put the target tool after the row number: `cw 3 codex`. Crossweave exports the complete textual conversation to `~/.cache/crossweave/handoffs/`, changes to its original working directory, and starts a new target-tool session with a prompt that asks it to read the export, summarize it, and continue the work. If the named tool already owns that row, the command simply resumes the original session. In listings, a session started this way is titled `(handoff) <topic>` after the session it continues (following a chain of handoffs back to the original), rather than by its own generated "Continue codex session ..." seed.

One exception: kimi cannot open an interactive session with an initial prompt (a bare prompt parses as a subcommand name), so a handoff to it runs the seed as a one-shot `kimi -p` and then automatically resumes the session that run persisted (`kimi -S <id>`), dropping you into the interactive continuation with the summary already in its history. If the seed run fails, nothing is resumed — the error is reported, and you pick up with `kimi -c`.

A zcode row can be a handoff *source* like any other — `cw 3 claude` exports its conversation and continues in Claude Code, which is the way out when a session has grown past what its model can serve. As a *target* it needs the `zcode` CLI on PATH: the CLI seeds one headless `zcode -p` run and resumes the session it persisted (the kimi dance); with only the desktop app installed the command refuses, since the desktop starts no session from a prompt.

WorkBuddy has no such other half: nothing but the app itself starts one of its sessions, so a workbuddy row is a handoff *source* (`cw 3 claude` exports its conversation and continues there — the only way to keep working on one from a terminal) and never a *target* (`cw 3 workbuddy` refuses, because there is no CLI to hand a prompt to).

### How `cw search` works

`cw search` runs two complementary passes:

1. **Exact pre-pass** — a case-insensitive scan of conversation text, tool calls, results, and kimi background-task output logs. It ignores session metadata and injected instructions. Short alphabetic queries such as `cra` match whole words, so they do not match `craft` or `crash`; longer queries retain substring matching. Results print under `exact matches` with zero LLM cost.

2. **Semantic pass** — the LLM judge. Titles alone miss a lot — plenty of sessions are titled "hi" or "(no title)", and the relevant sessions may never use your exact words. So each candidate's tool, cwd, title, and a short content snippet go into one prompt, and an LLM (`step -p` by default — which judge leads is a per-machine setting; `--judge claude`, `--judge codex` or `--judge kimi` pick another) picks out the relevant ones. One batched call, not one call per session — with 100+ sessions, calling an LLM separately for each would be far too slow and far too expensive. That also means it costs one real LLM call (tokens, however your `claude`/`codex`/`kimi`/`step` account bills them) every time you run it, and if a judge runs out of session limit or its login expires mid-search, the remaining judges are tried in order. Results print under `semantic matches`.

The exact pass exists because the semantic pass reasons over small *sampled* snippets, and a term that only appears in unsampled messages, tool calls, or past the snippet scan cap is invisible to the judge — a real `aria2c` search missed 4 sessions that grepping found immediately. The two passes union (a session listed as exact is not repeated under semantic), and both count for `cw resume <N>`.

Search skips Codex approval-review sessions whose prompts quote another agent's history. Those copies otherwise appear as duplicate matches. Row numbers continue across the exact and semantic sections, so each displayed number matches `cw resume <N>`.

The judge reasons about more than just keyword overlap — e.g. searching "katago" correctly pulled in sessions with generic titles like "hi" or "(no title)" that were run inside the `katago` project directory, which plain text search would have missed entirely.

It is also asked to name its strongest hit first, and that order is what gets printed — recency only breaks ties. Ranking by how recently a session ran put the one session actually about the topic below older ones that merely mentioned it. Only the strongest 10 are listed (`--all` shows the rest), since the tail is the weakest of them and a long list is what made a broad search hard to scan.

It must also point at something concrete — a file, command, error, or version — rather than count a session whose only link is the directory it ran in. Asked for that, the same search still returns `Hello` and `Yes go` rows from `android-vts`, but now with the specific commit behind each (`A13 VTS NFC HAL OpenAfterOpen fix`), which is what makes them worth keeping.

It has to say *why* each match counts: the judge answers one line per match (`7: upgrades the firmware from A13`) rather than a bare list of numbers, which makes it commit to a link instead of ticking a box. The reason prints as a WHY column beside the hit, clipped to whatever width the terminal has left over, so ten hits stay ten lines. `--why` prints it in full on its own line instead. That column is what makes a hit with a useless title readable: a row titled `Hello` in an `android-vts` directory is a real A13 VTS fix (`A13 VTS NFC HAL OpenAfterOpen fix committed to the A13 SDK`), which nothing else in the row suggests.

### Normalized flags (`cw <tool> ...`)

| Flag | Meaning | claude | codex | kimi | step | codebuddy | zcode |
|---|---|---|---|---|---|---|---|
| `-p`, `--print` | non-interactive, print and exit | `-p` | `exec` | `-p` | `-p` | `-p` | *(not supported — desktop app; bare `cw zcode` opens it on the current directory, anything else is rejected rather than misinterpreted)* |
| `-c`, `--continue` | continue most recent session in cwd | `--continue` | `exec resume --last` | `-c` | `-c` | `-c` | *(not supported)* |
| `-m`, `--model <model>` | model to use | `--model` | `-m` | `-m` | `--model` | `--model` | *(not supported)* |
| `--add-dir <dir>` | additional workspace directory (repeatable) | `--add-dir` (repeat) | `--add-dir` (repeat) | `--add-dir` (repeat) | *(not supported — step has no per-workspace flag; `--add-dir` is rejected rather than silently dropped)* | `--add-dir d1 d2` (one variadic flag — commander consumes every value after it) | *(not supported)* |
| `-y`, `--yolo` | auto-approve tool calls | `--dangerously-skip-permissions` | `--approve-for-me` (stays sandboxed) | `-y` | `--approval-mode auto` + `--non-interactive-approval allow` | `-y`, `--dangerously-skip-permissions` (HIGH/CRITICAL still ask) | *(not supported)* |

Anything after a literal `--`, or any flag this wrapper doesn't recognize, passes straight through to the underlying CLI unchanged.

WorkBuddy has no column here because there is no CLI to translate flags for: `cw workbuddy …` is refused rather than silently dropped, and the app is reached through `cw resume workbuddy <id>`.

## How session listing works

`cw sessions` reads each tool's native session storage — no shared index, no background process:

- **claude**: `~/.claude/projects/*/*.jsonl`
- **codex**: `~/.codex/sessions/**/*.jsonl`, plus `~/.codex/session_index.jsonl` for auto-generated titles
- **kimi**: `~/.kimi-code/session_index.jsonl` + each session's `state.json` / `agents/main/wire.jsonl`
- **step**: `~/.stepcode/agent/sessions/<encoded-cwd>/<timestamp>_<session-id>.jsonl`, one file per session — or `$STEP_CODING_AGENT_SESSION_DIR` if set. The id and cwd are the `session` record on the file's first line; the encoded directory is only the fallback when that record is missing. A session named with `step --name` or `/name` shows that name when its log holds no prompt to read. Sessions step started for itself (`subagent-…` ids) are left out unless `--all` asks for them.
- **zcode**: `~/.zcode/cli/db/db.sqlite` — a SQLite store (`session`/`message`/`part` tables), read through a read-only connection (`mode=ro`, the contract every read of a live app's store gets), so the app can be running while you list. There is no per-session transcript file: the conversation is `part` rows, the cwd is `session.directory`, and the title is one ZCode's own auto-titler keeps current — used directly, with the first genuine prompt standing in only for a session it never titled. Subagent runs (`parent_id` set) are left out unless `--all` asks for them.
- **codebuddy**: `~/.codebuddy/projects/<slug>/<sessionId>.jsonl` — claude's layout with a codex-rs record chain inside: a `message` record carries a top-level role and typed content blocks (`input_text`/`output_text`), a call and its result are separate `function_call`/`function_call_result` records, and the title is a stored `ai-title` the CLI's auto-titler writes into the file (`custom-title` from `/rename` wins when present). There is no header record — the id and cwd ride on every substantive record — and a resume appends to the same file. Big tool results land outside the transcript, in `<slug>/<sessionId>/tool-results/*.txt`, which the literal scan reads too. CodeBuddy starts no sessions of its own.

  That layout is the documented [session file format](https://pi.dev/docs/latest/session-format) shared by the pi-derived CLIs, so reading these files directly is the sanctioned route rather than a hack — the same reason claude's `projects/*.jsonl` and codex's `sessions/**` are read as they are.

- **workbuddy**: `~/.workbuddy/projects/<slug>/<sessionId>.jsonl` and `~/.workbuddy-ai/projects/<slug>/<sessionId>.jsonl` — the same engine's transcripts, since WorkBuddy is CodeBuddy Code inside an Electron shell. So this store is read by codebuddy's reader, and both editions are read as **one tool** with the URL scheme carried per row: the AI edition registers `workbuddy-ai://` and its handler launches a different executable, so a link built from the wrong store opens an app that has never heard of the session. The shell decorates every user message, though: it prepends a per-turn context block and puts what you typed after it, *inside the same message* (`</system-reminder> <user_query>排查 Cisco VPN 连接报错</user_query`). Read as-is that whole block is the scaffolding it mostly is, and the session would list as having no prompts at all, so the wrapper is unwrapped rather than skipped — and only for a message that *opens* with it, because the engine's compaction recap quotes earlier queries inside itself and would otherwise re-count the session's opening prompt as a turn it never had. Two record kinds are WorkBuddy's own and are looked past rather than counted as conversation — a `session-meta` host line and the `file-history-snapshot` rows its rollback feature writes (they rival the real messages: 113 of them beside 145 messages in one session here). The siblings of a transcript are not transcripts either: `<id>.meta.json` and `<id>.file-rollback.ndjson` are skipped, and a subagent's run is found one level deeper at `<slug>/<sessionId>/subagents/agent-*.jsonl` (left out unless `--all` asks). A pasted screenshot arrives as a reference to the file it was written to (`@image#1:…`, `<image_local_path>…`), which names no topic — the same as Claude's `[Image: …]` paste, so it is neither a turn nor a title. Conversations the app keeps in the cloud store no transcript here, so they are not listed; and because two editions can hold the same project, the cwd comes from the records, never decoded from the `<slug>` directory name.

Titles are best-effort (scanned from the first user message / prompt in each session's log). Claude's cwd is read from the session content itself when available, falling back to a guess decoded from the project-directory name only if that's missing. Resuming a codex thread appends a *new* rollout file instead of extending the one it already had, so a single session id can own several; only the newest is read — for the row's timestamp, title, cwd, snippet, and any handoff. A codex fork or subagent thread has no prompt of its own, so it is named after the thread it forked from (`(fork) …`), the way a handoff is named after the session it continues. kimi names its own sessions too, and that name is used when its log holds no text prompt to read — but only when it is a real name, not the `[image]`/`New Session` placeholder it uses until it has one. A message relayed by a bridge (openclaw) behind its `Conversation info: ⟦openclaw:ctx⟧` header is read as what was said after the header, not as the header itself.

`cw doctor` is the companion for the failure mode all this tolerance hides: every reader treats an unreadable store as an empty one, so when a tool changes its on-disk format, sessions silently vanish from the listing with nothing to say why. `cw doctor` asks each store the questions the readers swallow — is it there, does it open, how many sessions come out, and do the numbers add up (transcript files that resolve to no sessions are reported as broken, not empty). It exits non-zero when a store is broken, so it can run in a shell check.

`kimi -S <id>` refuses to resume a session from a different directory than the one it was created in. `cw resume`/`cw <N>` know each session's original directory already (it's the CWD column), so for all the tools they `cd` there automatically before resuming, rather than leaving you to do it by hand (or, for kimi, surfacing its hard error).

Pass `--cwd <dir>` to send it somewhere else instead, e.g. `cw resume 2 --cwd /path/to/other-project`. All five terminal tools tie a session's transcript to whichever directory it first ran in — claude, codex and kimi quietly keep writing to the *original* directory's log (confirmed by testing `claude --resume` from an unrelated directory: nothing was written under the new one), and step refuses outright, stopping to ask `Fork this session into current directory? [y/N]` (under `-p` there is no TTY to answer it: exit 1, nothing written) — so a session can't actually be relocated in place. If `--cwd` points at a directory other than the one the session already lives in, `cw resume` recognizes that a plain `--resume` there wouldn't accomplish anything (it'd work, but the conversation would still be invisible to that directory's own `/resume` picker) and instead does a handoff: it exports the full transcript and starts a **new**, freshly-seeded session in `<dir>` — same as `cw <N> <other-tool>`, but staying on the same tool. That new session is a real one rooted in `<dir>`, so it shows up in `/resume` there going forward.

ZCode ships both a desktop app and a terminal CLI under the same name, sharing this store. When the CLI is the `zcode` on PATH (told apart by file header — probing either with `--help` boots the desktop GUI), `cw resume zcode <id>` is a real resume (`zcode --resume <id>`, the TUI opening in the session's own directory), bare `cw zcode` opens the CLI's TUI here, and a `--cwd` relocation or `cw <N> zcode` hands off by seeding one headless `zcode -p` run and resuming the session it just persisted — the same two-step kimi needs. An install whose CLI bundles no `@zcode/tui` (one checked: the CLI delegating to the desktop's core) has no TUI at all, so a resume there lands on a hint instead of a node traceback — and the fact is remembered per install, keyed by the `zcode` executable's path, size and mtime so that an upgrade invalidates it by itself. After that the doomed start is skipped and the two ways forward are named outright: the desktop app's task list, or the headless `zcode --resume <id> -p "<next instruction>"` continuation (verified 2026-10-10 — the follow-up turn appends to the same session, context intact). cw takes that route itself when the instruction is on the command line: `cw resume zcode <id> "<next instruction>"`, or `cw resume <N> "..."` by row number, which also gives words after an id a meaning, since a TUI would only reject them. Only on a machine where the desktop app is all there is does the wrapper fall back to the workspace deep link, `zcode://workspace/open?path=…`, which opens the right workspace but no particular session, and says so.

WorkBuddy is the other kind of desktop case: it registers a *session-level* deep link (`workbuddy://chat/<id>`, or `workbuddy-ai://chat/<id>` for the AI edition — the id percent-encoded exactly as the app does it), so `cw resume workbuddy <id>` lands on the conversation itself rather than on the workspace around it. Nothing has to be on `PATH` for that; cw asks the platform what opens a URL — `open` on macOS, `xdg-open` on Linux, and on Windows the executable registered under `HKCU\Software\Classes\<scheme>\shell\open\command`, falling back to the installer's known path, then to `cmd /c start` if the registry answers nothing. The app is launched rather than exec'd, so the terminal stays where it was and just reports which session it opened and where that session lives. Words after the id and `--cwd` both have nowhere to go — a GUI takes no instruction from a terminal, and a session lives in the directory it was started in — so each is refused, naming `cw <N> claude` on its row as the way forward.

## Renaming sessions

A listing is scanned, not read, and the tools themselves name sessions well only when the first message cooperates — a conversation that opened with "hi" or "中文" is indistinguishable from every other one that did. `cw rename` writes a better title through to the store itself, using each tool's own rename surface, so the name is native to the tool and every listing shows it:

```bash
cw rename 12 "USB 枚举握手排查"        # row 12 from the last listing
cw 12 rename "USB 枚举握手排查"        # the same command, row first
cw 12 rename                          # no title: the judge proposes one
cw rename --auto --trivial            # propose titles for every trivial one
cw rename --trivial                   # the same -- --trivial implies --auto
cw rename --auto 3 7 -y               # only these rows; no confirmation
```

The row number may lead or follow the verb — `cw 12 rename "..."` is `cw rename 12 "..."`, matching how `cw 12 codex` already reads. `--auto` has no one row to put first, so the sweep stays verb-first. One reading is given up for it: a bare `rename` after a row number is no longer passed to the tool as a continuation prompt (it used to mean "continue this session and say *rename*"), and `cw resume 3 rename` is the explicit spelling for anyone who meant that.

A row named with no title after it is a request for a suggestion, not a mistake — the row is the part that takes knowing the listing, and the title is the part the judge is better at. `cw 12 rename` asks the judge for one title and applies what it comes back with, printing the change; it is the single-row spelling of what `--auto` does for a batch, and it applies without asking because a batch of sixty is worth a look before anything is written and one row you just pointed at is not. The same two rules hold it back from writing something useless: a session with nothing in it is never asked, and a proposal that restates the title already there is reported rather than written, since a custom rename is what stops the tool's own titler from ever improving the name.

`--auto` and `--trivial` answer different questions — who writes the title, and which sessions — so `cw rename --auto 3 7` names the rows and lets the judge title them, while `cw rename --trivial` on its own is the sweep, with `--auto` implied (it picks neither a title nor a row, so there is nothing else it could be asking for).

Five stores have a surface cw can write: **zcode** (`title_source='custom'`, which its own auto-titler respects), **codex** (a newer `session_index.jsonl` line — the index keeps the most recent name per id), **claude** (a `custom-title` record appended to the transcript — the reader keeps the last one), **step** (a `session_info` record appended to the session file — both of step's readers keep the last one, and its own resume picker shows the name *in place of* the first prompt, which is what makes a session that opened with "中文" nameable) and **kimi** (`title` plus `titleKind='custom'` in the session's `state.json` — the kind every titler path bails on, so the name is permanent; `updatedAt` is left alone so a renamed session does not look freshly live). The others are refused rather than guessed at — codebuddy and workbuddy share one engine, and the only title field it keeps sits in the live app's own database, not in the transcript cw reads. `--auto` extracts each candidate's content, asks the same judge `cw search` uses — one batched call per twenty sessions — and shows a before/after table that waits for a yes; it never proposes for a session that already has a real title, and skips sessions updated in the last five minutes, which are live conversations mid-flight with their own titler. A proposal that merely restates the title already there is shown as kept and not applied: a custom rename is, in every store, what stops the tool's own titler from ever improving the name, so writing "hi" over "hi" would freeze a bad title for good rather than leave it to be retitled. A session holding no words at all — opened and abandoned, a cancelled `/resume`, a launch closed before anything was typed — is never offered to the judge, because there is no topic to name and asking anyway buys an invented title. The sweep counts those on stderr and leaves them out of the table: they can never be renamed, so carrying one would spend a slot of the run's sixty on it and re-offer it on every run after. Naming one explicitly (`cw rename --auto 7`) still reports it rather than dropping it. Renaming a claude or step session restores the file's mtime afterwards, so a months-old conversation does not read as active a minute ago and float to the top of the listing. "A real title" is a question of shape, not length: a row titled with a pasted shell transcript, an `ls -l` listing, a bare URL or a CLI's startup banner says less than one titled "hi", and counts as having no title at all.

## `cb`: CodeBuddy

CodeBuddy ships as a CLI, a VS Code extension and a desktop app, and the three keep their own, unrelated stores. `cb` lists all of them as one table. The CLI client also appears in `cw` as the `codebuddy` tool, where it is a full citizen — resumed, searched, and handed to and from like the rest. What `cb` still uniquely offers is the GUI clients' rows and the `--cluster` view that guesses which of the three clients' unrelated ids are one conversation; it keeps listing the CLI client so that table stays whole (its row numbers are its own cache and never collide with a `cw` listing).

```bash
cb                     # recent sessions from all three clients (same as `cb sessions`)
cb full                # no cutoff
cb --client cli        # one client: cli, vscode or desktop (repeatable)
cb --cwd .             # only sessions started in the current directory
cb --cluster           # rows that are probably one conversation split across clients
cb resume 3            # row 3 of the last `cb` listing
cb resume cli:01a0f6ee # by client and id prefix
cb resume 3 --dry-run  # show what would run, touch nothing
cb 3                   # shorthand for `cb resume 3`
```

| Client | Store | Title | `cb resume` |
|---|---|---|---|
| cli | `~/.codebuddy/projects/<slug>/<sessionId>.jsonl` | the session's AI title, else the first prompt | `codebuddy -r <id>` in the session's directory |
| desktop | `codebuddy-sessions.vscdb` (sqlite) in the app's data directory | stored | launches the app on the session's folder |
| vscode | `tencent-cloud.coding-copilot` extension storage | none stored — the first todo stands in | opens the workspace (`--history` also opens the extension's history panel) |

Only the CLI can really resume. The extension contributes no command that opens one conversation, and the desktop app registers `codebuddy://` but defines no route for a session, so for those two `cb resume` gets you to the right folder and tells you so. A `--to cli` handoff of such a row is refused: with no transcript there is nothing to hand over, only a new empty session.

The three clients mint unrelated ids, so nothing proves two rows are the same conversation. `--cluster` guesses from the directory and the time (`--window`, default 120 minutes) and never puts two rows of one client in the same group. Treat it as a hint.

`cb` numbers rows with its own cache, so a listing from `cw` never becomes `cb resume <N>` or the reverse. The Windows layout is the one checked against a real install; the macOS and Linux locations of the VS Code and desktop stores follow Electron's convention and are unverified.

## Development

```bash
pip install -e ".[test]"
pytest tests/ -v
```

## What this isn't

Not a TUI, not a worktree manager, not a multi-agent orchestrator. If you want any of that, look at [ccmanager](https://github.com/kbwo/ccmanager) (git-worktree-centric session manager, also supports Kimi CLI) or [claude-squad](https://github.com/smtg-ai/claude-squad).

## License

MIT — see [LICENSE](LICENSE).

### Delete a session

Run `cw 7 delete` (or `cw delete 7`) to remove row 7 from the session
files scanned by cw. The number refers to your last displayed list. The
command shows the tool and full session ID and asks for confirmation;
`-y` skips that prompt. Refresh with `cw` to get new row numbers after deletion.

Files are moved to `~/.cache/crossweave/deleted/`, with a `manifest.json`
mapping backup files to their original paths for manual recovery. Codex
removal includes every rollout for the selected session. Kimi removal
includes its session directory. Claude, step, CodeBuddy and WorkBuddy
removal moves the selected transcript. ZCode database sessions and remote
rows are currently unsupported. Tool-owned database and index entries are
not removed, so a tool's own history UI may still show the session.
