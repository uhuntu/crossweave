# 需求：将 Kiro 集成到 crossweave（cw）

## 背景

crossweave 目前统一封装 claude、codex、kimi、step、pi、opencode 等工具，并支持跨工具的会话发现、恢复和交接。本功能把 Kiro 加入这个集合，覆盖两种形态：

- **Kiro CLI**（`kiro-cli`，终端程序）：完整支持。
- **Kiro IDE**（`/usr/bin/kiro`，图形界面）：只支持在 cw 里打开工作目录。IDE 里的某个具体会话无法从终端恢复，所以不做会话级的恢复。

## 已确认的事实（本机调查，只读）

- `kiro-cli chat` 的启动参数：
  - `[INPUT]`：第一条问题，对应 cw 的 `-p`，需要配合 `--no-interactive`。
  - `-r` / `--resume`：继续本目录最近的会话，对应 cw 的 `-c`。
  - `--resume-id <SESSION_ID>`：恢复指定会话。
  - `-a` / `--trust-all-tools`：信任所有工具，对应 cw 的 `-y`。
  - `--model <MODEL>`、`--agent <AGENT>`。
- 当前在用的会话存储是 `~/.kiro/sessions/<工作区哈希>/sess_<uuid>/`：
  - `session.json`：包含 `id`、`title`、`createdAt`、`lastModifiedAt`、`workspacePaths`、`agentMode` 等字段。
  - `messages.jsonl`：每行是 `{id, timestamp, payload}`，`payload` 的类型有 `user`、`assistant`、`tool_call`、`tool_result`、`turn_start`、`turn_end`、`usage_summary`、`session_event`、`session_metadata`、`agent_note`。
  - `<工作区哈希>/.index/index.json`：该工作区的会话索引。
  - `~/.kiro/workspace-roots/<工作区哈希>/permissions.yaml`：每个工作区的权限策略，不是路径映射。路径以 `session.json` 的 `workspacePaths` 为准。
- `~/.local/share/kiro-cli/data.sqlite3` 的 `conversations_v2` 表为空，当前看起来没有使用。
- `~/.kiro/sessions/cli/*.history` 是纯文本输入历史，不是完整会话，不作为会话来源。

## 用户故事与验收标准

### R1 启动 Kiro CLI
作为用户，我希望运行 `cw kiro` 就能启动 Kiro CLI，并在当前目录打开会话。

- 1.1 `cw kiro` 启动 `kiro-cli chat`，工作目录为当前目录。
- 1.2 `cw kiro -p "问题"` 运行一次性的非交互会话（`--no-interactive`），输出结束后退出。
- 1.3 `cw kiro -c` 继续本目录最近的会话（`--resume`）。
- 1.4 `cw kiro -y` 信任所有工具（`--trust-all-tools`）。
- 1.5 `-m/--model` 传给 `--model`。
- 1.6 `--add-dir` 不支持时，报出清楚的错误，与 step、pi 的做法一致。

### R2 启动 Kiro IDE
作为用户，我希望 `cw kiro --ide` 用 IDE 打开当前目录。

- 2.1 `cw kiro --ide` 调用 `kiro <目录>`，打开工作区。
- 2.2 IDE 模式不接受会话相关参数（`-c`、`-p`、会话 id），遇到时报错，不静默忽略。

### R3 列出 Kiro 会话
作为用户，我希望 `cw sessions` 列表中能看到 Kiro 的 CLI 会话。

- 3.1 扫描 `~/.kiro/sessions/*/sess_*/session.json`，读取 `id`、`title`、`createdAt`、`lastModifiedAt`、`workspacePaths`。
- 3.2 解析失败的文件跳过，不影响其他会话（遵守项目"容忍脏数据"的约定）。
- 3.3 列表中的工具名为 `kiro`，与其他工具的显示方式一致。

### R4 恢复 Kiro 会话
作为用户，我希望 `cw resume kiro <id>` 能恢复一条 Kiro 会话。

- 4.1 调用 `kiro-cli chat --resume-id <id>`。
- 4.2 会话的工作目录与记录中的 `workspacePaths` 一致。
- 4.3 会话 id 支持前缀匹配，与其他工具相同。

### R5 搜索与查看
作为用户，我希望 `cw show` 和 `cw search` 能读取 Kiro 会话内容。

- 5.1 `cw show` 按 `messages.jsonl` 中的 `user` 与 `assistant` 消息渲染对话。
- 5.2 `tool_call` 和 `tool_result` 不作为正文显示，仅在搜索中可选匹配。
- 5.3 `cw search` 能在 Kiro 会话的正文中找到关键词。

### R6 跨工具交接
作为用户，我希望能把其他工具的会话交接给 Kiro，或把 Kiro 会话交接出去。

- 6.1 交接到 Kiro：把源会话的完整对话写成 Markdown 导出文件（`~/.cache/crossweave/handoffs/<工具>-<id>.md`），用 `kiro-cli chat` 启动新会话，第一条输入是一段短提示，指向该导出文件。不截断对话内容，与其他工具的做法一致。
- 6.1a 启动交接会话时，要允许 Kiro 读取导出文件（`--trust-tools=fs_read` 或等价设置）。否则无交互运行时读取可能被拒绝。
  - 状态：已验证。在用户自己的终端里运行 `kiro-cli chat --no-interactive --trust-tools=fs_read "..."`，工具调用 `Reading export.md` 完成，输出了文件内容。在 Kiro agent 的 shell 里运行时会超时，原因可能是嵌套环境（继承了 `KIRO_SESSION_ID`），没有进一步排查。交接命令应在普通终端环境中执行，或者先清除该变量再测试。
- 6.2 从 Kiro 交接出去：读取 `messages.jsonl` 的 `user` 与 `assistant` 消息，生成交接文本。
- 6.3 IDE 会话不支持交接，报错说明原因。

### R7 不破坏现有行为
- 7.1 不改变 claude、codex、kimi、step、pi、opencode 等现有工具的行为。
- 7.2 现有测试保持通过，新增测试覆盖上面的每条验收标准。
- 7.3 只读取 Kiro 的数据，不写入或修改 `~/.kiro`。

## 待确认的问题

1. VS Code 里的 Kiro 扩展（`kiro.kiro-for-vscode` 1.5.44）和 CLI 共用 `~/.kiro/sessions` 存储。依据是扩展代码引用了同一路径，以及 11:32 前后在同一工作区哈希目录下出现了一个有用户消息和助手回复的新会话 `sess_59bc2bd1-...`。用户消息时间（11:32:02）与用户发送时间吻合，且同一时段只有这一个新会话，因此判断为扩展产生。cw 只需要扫描这一个存储，不需要区分来源。
  - 状态：
    - 已验证：CLI 会话 `sess_26ab4f27-...` 写入 `~/.kiro/sessions/d0df2a33e0aa81e5/`。
    - 已验证：VS Code 扩展的会话列表显示 crossweave 工作区的会话，其中活动会话的时间戳与存储中 `sess_59bc2bd1-...` 的 `lastModifiedAt` 一致，说明扩展读取同一存储。
    - 已验证：在 `/home/hunt/work/crossweave` 运行 `kiro-cli chat` 产生的会话 `sess_349e3fbb-...` 写入同一存储。
    - 已验证：CLI 会话 `sess_9e416659-...`（消息 "Hello from cli"，工作区 crossweave）在 VS Code 的 Kiro 面板中打开，显示 CLI 的消息和回复。CLI 与扩展双向共用同一存储。
    - 注：此前在列表中看不到 "Getting Started With Kiro" 的问题未查明。会话能被打开，说明数据可读。
    - 已验证：独立的 Kiro IDE 窗口（`/usr/bin/kiro`）中，标签页同时打开了 CLI 会话（`sess_9e416659-...`）、VS Code 扩展产生的会话（`sess_59bc2bd1-...`）。三端（CLI、VS Code 扩展、Kiro IDE）读写同一存储。
    - 已验证：VS Code 扩展写入同一存储。会话 `sess_59bc2bd1-...`（VS Code 面板中的 "New Kiro Session"）的消息中包含在 VS Code 里发送的 "test from vscode"。
    - 结论：CLI 与扩展双向共用同一存储，此前"根据时间推断"的部分已由消息内容证实。
2. 交接时的上下文长度：已决定不截断，写导出文件，第一条输入只是指向文件的短提示（见 6.1）。
3. `cw` 的旧名称（`ai`、`aim` 等）是否需要为 Kiro 提供？按项目约定，新工具不需要。
