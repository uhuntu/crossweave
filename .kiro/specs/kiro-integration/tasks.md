# 任务：Kiro 集成到 crossweave

依据：`requirements.md`（需求编号 R1–R7）和 `design.md`。每个任务完成后都要通过 `pytest tests/ -v` 和 `ruff check src/ tests/`，不能破坏现有测试。

## 1. 读取模块 `kiro.py`（R3、R5）

- [x] 1.1 新建 `src/crossweave/kiro.py`，实现 `kiro_light_records(show_all)`：扫描 `~/.kiro/sessions/*/sess_*/session.json`，返回 `tool="kiro"`、`id`、`title`、`cwd`（`workspacePaths[0]`）、`ts`、`path`。
- [x] 1.2 过滤空会话：没有 `messages.jsonl` 或没有 `user` 消息的会话不列出（设计第 7 节第 4 条）。
- [x] 1.3 标题回退：标题为空、"New Session" 或 "New Kiro Session" 时，用第一条用户消息的前 70 个字符。
- [x] 1.4 损坏的 JSON 行和损坏的 `session.json` 直接跳过，不抛异常。
- [x] 1.5 实现 `kiro_resolve(prefix)`（前缀匹配）和 `kiro_session_cwd(sid)`。
- [x] 1.6 实现 `kiro_snippet(sdir, max_messages, max_chars)` 和 `kiro_handoff_messages(sdir)`，只取 `payload.type` 为 `user` 或 `assistant` 的 `payload.content`。
- [x] 1.7 测试 `tests/test_kiro.py`：使用临时目录伪造会话结构，覆盖 1.1–1.6。

## 2. 启动参数 `cli.build_command`（R1）

- [x] 2.1 `TOOLS` 加入 `kiro`。
- [x] 2.2 `build_command("kiro", ...)` 映射：无参数 → `kiro-cli chat`；`-p` → `--no-interactive`；`-c` → `--resume`；`-y` → `--trust-all-tools`；`-m` → `--model`。
- [x] 2.3 `--add-dir` 报错，与 step、pi 一致（R1.6）。
- [x] 2.4 `--ide`：调用 `kiro <目录>` 打开工作区；与 `-p`、`-c`、会话 id 同时出现时报错（R2）。
- [x] 2.5 测试：在 `tests/test_cli.py` 中增加上述映射的用例。

## 3. 会话列表、恢复、查看（R3、R4、R5）

- [x] 3.1 `sessions.py`：导入 kiro 函数，把 `kiro_light_records` 加入 `_all_light_records`、列表源和 `_find_record`。
- [x] 3.2 恢复：`cw resume kiro <id>` 生成 `kiro-cli chat --resume-id <id>`，工作目录设为 `workspacePaths[0]`（R4.1、R4.2）。
- [x] 3.3 `cw show` 按 `user` 和 `assistant` 渲染；`tool_call` 和 `tool_result` 不显示为正文（R5.1、R5.2）。
- [x] 3.4 测试：覆盖列表、恢复命令和查看输出。

## 4. 搜索（R5）

- [x] 4.1 `retrieval.py`：`context_for` 对 `kiro` 使用默认的 `path`（会话目录）作为存在性依据。kiro 的记录里 `path` 就是会话目录，不需要 `id`。
- [x] 4.2 `search.py`：加入 kiro 的摘要分支（如需要）。
- [x] 4.3 测试：`cw search` 能找到 Kiro 会话正文中的关键词（R5.3）。

## 5. 交接（R6）

- [x] 5.1 `session_handoff_details` 增加 `kiro` 分支，使用 `kiro_handoff_messages`。
- [x] 5.2 交接到 Kiro：启动 `kiro-cli chat --trust-tools=fs_read "<短提示>"`，一步进入交互会话（设计第 4 节，已验证）。提示指向导出文件路径。
- [x] 5.3 从 Kiro 交接出去：导出文件包含完整的 `user` 和 `assistant` 文本，不截断（R6.2）。
- [x] 5.4 交接对所有 kiro 会话开放，不做 IDE 会话的单独拒绝（R6.3 已修改，见 requirements.md）。
- [x] 5.5 测试：交接提示内容、导出文件内容、`--trust-tools=fs_read` 出现在命令中。

## 6. 不破坏现有行为（R7）

- [x] 6.1 运行完整测试，现有测试保持通过。已知的失败：无（基线中的 agy 与 tray 问题已修复或已跳过）。
- [x] 6.2 运行 `ruff check src/ tests/`，无新增问题。
- [x] 6.3 确认不写入 `~/.kiro`（R7.3）：读取路径中没有写操作。

## 7. 手动集成验证（需要 kiro-cli 登录，在普通终端中执行）

- [ ] 7.1 `cw kiro -p "..."` 运行一次性会话。
- [ ] 7.2 `cw kiro -c` 继续本目录最近的会话。
- [ ] 7.3 `cw sessions` 列出 CLI 和 VS Code 产生的 Kiro 会话。
- [ ] 7.4 `cw resume kiro <id>` 恢复 VS Code 产生的会话。
- [ ] 7.5 `cw <N> claude` 把 Kiro 会话交接给 claude，并检查导出内容。
- [ ] 7.6 `cw kiro --ide` 用 Kiro IDE 打开当前目录。

## 注意事项

- 在 Kiro agent 的 shell 中运行 `kiro-cli chat` 会超时（嵌套环境，`KIRO_SESSION_ID` 已设置）。集成测试要在普通终端里做，或先清除该变量。
- 每个任务单独提交，提交信息使用项目的 conventional commits 风格。
