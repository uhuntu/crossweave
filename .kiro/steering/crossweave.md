# crossweave 项目说明

## 项目概况
- 纯 Python 包（当前版本 0.4.0，要求 Python >= 3.9），源码在 `src/crossweave/`。
- 统一封装多个 AI 编程 CLI（Claude Code、Codex、Kimi、step、pi、opencode 等），并跨工具发现、恢复、交接会话。
- 直接读取各工具的本地会话存储，没有守护进程，没有构建步骤。
- 命令入口：`cw`、`cx`、`crossweave`（`pyproject.toml` 的 `[project.scripts]`）。

## 常用命令
- 安装开发依赖：`pip install -e ".[test]"`
- 运行测试：`pytest tests/ -v`
- 代码检查：`ruff check src/ tests/`
- 包的构建检查见 `.github/workflows/test.yml`。

## 代码约定
- ruff 只启用 F811 和 E9，其余规则有意关闭（见 `pyproject.toml` 注释），不要为了通过 lint 而改动这些设计。
- 行宽上限 100。
- 部分读取器（如 JSONL 读取、会话目录扫描）有意吞掉解析异常，以容忍脏数据。修改它们时保留这一行为。
- 旧名称（`clisweave`、`aimux`、`ai`、`aim`、`ai-sessions`）为兼容保留，不要删除对应的入口。
- `CodeBuddy` 和 `WorkBuddy` 支持目前已禁用。
- `cw` 会把未识别的参数原样传给底层工具（透传）。`cw claude --mcp-config <文件>` 可以直接用。
- `cw` 把 `-c` 当作 `--continue`，但 codex 下 `-c <dotted.key>=<value>` 会原样交给 codex（见 `cli._is_codex_override`）。例如 `cw codex -c mcp_servers.x.command="npx"`。其他形式的 `-c` 仍然是 `--continue`。

## 工作区注意事项
- `.venv` 和 `.venv-1` 是本地虚拟环境，不要提交或修改。
- 修改安装脚本（`install.sh`、`install.ps1`）时，注意 Windows 上的 CI 冒烟测试。
## Kiro 集成
- 代码：`src/crossweave/kiro.py`（读取）；接入点在 `cli.py`（启动参数、`--ide`）、`sessions.py`（列表、恢复、交接、查看）、`search.py` 与 `scan.py`（搜索）。
- 会话存储：`~/.kiro/sessions/<工作区哈希>/sess_<id>/`，含 `session.json` 和 `messages.jsonl`。Kiro CLI 与 VS Code 的 Kiro 扩展共用此存储，所以两者的会话都出现在 `cw sessions --tool kiro`。
- `cw kiro -p` 的工具调用需要 `-y`，否则被拒绝，但回答仍会输出。
- 交接到 Kiro 用 `kiro-cli chat --trust-tools=fs_read "<提示>"`，一步进入交互会话，提示里指向导出文件。
- 设计与任务见 `.kiro/specs/kiro-integration/`。
