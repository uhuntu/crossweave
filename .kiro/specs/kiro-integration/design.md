# 设计：Kiro 集成到 crossweave

需求见 `requirements.md`。本文档说明实现方式。参照 kimi 的接入方式，因为它同样是"读本地会话存储 + 用 CLI 恢复"。

## 1. 数据来源

- 会话存储：`~/.kiro/sessions/<工作区哈希>/sess_<uuid>/`。
  - `session.json`：`id`、`title`、`createdAt`、`lastModifiedAt`、`workspacePaths`。
  - `messages.jsonl`：每行 `{id, timestamp, payload}`。文本来自 `payload.type` 为 `user` 和 `assistant` 的行。
- 工作目录：取 `session.json` 的 `workspacePaths[0]`。
- 不读取 `data.sqlite3`（`conversations_v2` 为空），不读取 `sessions/cli/*.history`（纯输入历史）。
- 来源不区分：CLI 和 VS Code 扩展写同一存储，列表里统一显示为 `kiro`。

## 2. 新模块 `src/crossweave/kiro.py`

仿照 `kimi.py`，提供以下函数（名称与其他工具保持一致）：

| 函数 | 作用 |
| --- | --- |
| `kiro_light_records(show_all)` | 扫描所有 `session.json`，返回记录列表。记录字段：`tool="kiro"`、`id`、`title`、`cwd`（来自 `workspacePaths[0]`）、`ts`（`lastModifiedAt`）、`path`（会话目录）。 |
| `kiro_title(sdir)` / `kiro_stored_title(sdir)` | 读取标题。标题为 "New Kiro Session" 之类的默认值时，用第一条用户消息作为标题。 |
| `kiro_session_cwd(sid)` | 返回工作目录。 |
| `kiro_resolve(prefix)` | 按 id 前缀解析，与其他工具相同。 |
| `kiro_snippet(sdir, max_messages, max_chars)` | 搜索用的摘要，只取 `user` 和 `assistant` 文本。 |
| `kiro_handoff_messages(sdir)` | 交接用的 `(role, text)` 列表，同样只取 `user` 和 `assistant`。 |

解析要求：遇到损坏的 JSON 行或文件直接跳过，不抛异常（符合项目"容忍脏数据"的约定）。

## 3. 启动参数映射（`cli.build_command`）

| cw 参数 | kiro-cli 参数 | 说明 |
| --- | --- | --- |
| 无 | `kiro-cli chat` | 交互会话 |
| `-p "问题"` | `kiro-cli chat --no-interactive "问题"` | 一次性运行 |
| `-c` | `kiro-cli chat --resume` | 继续本目录最近的会话 |
| `-y` | `--trust-all-tools` | 信任所有工具 |
| `-m 模型` | `--model 模型` | |
| `--add-dir` | 报错 | 与 step、pi 一致 |
| `--ide` | 用 `kiro <目录>` 打开 IDE | 只打开目录，不恢复会话 |

`kiro` 命令（IDE）和 `kiro-cli` 是两个程序，`build_command` 只生成 `kiro-cli` 的命令。`--ide` 在 `cli.py` 里单独处理。

## 4. 恢复与交接

- 恢复：`cw resume kiro <id>` 生成 `kiro-cli chat --resume-id <id>`，工作目录设为 `workspacePaths[0]`。
- 交接到 Kiro：沿用 `sessions.perform_handoff`。先把源会话完整写成导出文件，再启动 Kiro。
  - 启动命令：`kiro-cli chat --trust-tools=fs_read "<短提示>"`（不带 `--no-interactive`，提示词作为第一条问题直接进入交互会话）。
  - 这样只需一步，与 claude、codex 的交接相同。kimi 不能这样做，所以它先跑一次 `kimi -p` 种子会话，再恢复，交接表现因此不同。Kiro 不需要这个两阶段流程。
  - 已验证（普通终端，13:13）：`kiro-cli chat --trust-tools=fs_read "<提示>"` 直接进入交互界面，提示作为第一条消息发出，Kiro 回答后停在输入框。交接只需一步。
  - 短提示指向导出文件路径，与其他工具的提示一致。
  - `--trust-tools=fs_read` 已经验证：无交互运行时可以读取导出文件。
- 从 Kiro 交接出去：`session_handoff_details` 增加 `kiro` 分支，用 `kiro_handoff_messages` 生成导出。
- IDE 会话的交接：IDE 和 CLI 共用同一存储，交接对所有 kiro 会话开放（不做单独拒绝）。`--ide` 只负责打开目录，不参与恢复和交接。

## 5. 需要改动的文件

- `src/crossweave/kiro.py`（新增）。
- `src/crossweave/cli.py`：`build_command` 增加 `kiro` 分支；`--ide` 处理；`TOOLS` 列表加入 `kiro`。
- `src/crossweave/sessions.py`：导入 `kiro` 函数；`TOOLS`、`_all_light_records`、列表、`session_handoff_details`、`_find_record` 各加一个 `kiro` 分支；恢复逻辑（`cmd_resume`）加入 `kiro`。
- `src/crossweave/retrieval.py`：`context_for` 对 kiro 使用 `path`（会话目录）作为存在性依据，与 kimi 类似。
- `src/crossweave/search.py`：必要时加入 `kiro` 的摘要分支。
- `tests/test_kiro.py`（新增）和 `tests/test_cli.py`：覆盖需求中的验收标准。

## 6. 测试计划

- 单元测试使用临时目录伪造 `~/.kiro/sessions` 结构（`session.json` + `messages.jsonl`），不依赖真实数据。
- 覆盖：列表解析、损坏行跳过、标题回退、前缀恢复、`build_command` 各参数映射、交接提示和导出文件内容、`--ide` 报错与生成的命令。
- 集成验证（手动，需要 kiro-cli 登录）：在普通终端里 `cw kiro -p`、`cw resume kiro <id>`、`cw <N> kiro` 交接。

## 7. 待确认的风险

1. 已验证（普通终端，13:18）：`kiro-cli chat --resume-id sess_59bc2bd1-...`（VS Code 扩展产生的会话）能恢复，并显示之前的对话。
2. 已验证：`messages.jsonl` 中 `user` 和 `assistant` 消息的文本在 `payload.content`，类型为字符串。所有会话共 793 条，没有异常。`user` 还有 `documents` 和 `images` 字段，本设计暂不使用。
3. 在 Kiro agent 的 shell 里运行 `kiro-cli chat` 会超时（嵌套环境）。测试脚本里要清除 `KIRO_SESSION_ID`，或只在普通终端里做集成测试。
4. 默认标题（已查 23 个会话）：
   - 9 个会话标题是 "New Session" 或 "New Kiro Session"。其中 7 个没有 `messages.jsonl`，是从未使用的空会话。
   - 2 个有用户消息但标题没有更新（`sess_59bc2bd1`，2 条；`sess_9f6af80f`，1 条）。Kiro 的自动标题并不总是运行。
   - 因此设计如下：没有用户消息的会话不列出（和 claude 的 `claude_is_empty` 做法一致）；标题是默认值或为空时，用第一条用户消息的前 70 个字符作为标题。
