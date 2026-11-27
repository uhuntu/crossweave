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
- `cw` 把 `-c` 当作 `--continue`。要给 codex 传 `-c key=value` 这类配置覆盖（例如 MCP 服务器配置），必须用 `--`：`cw codex -- -c mcp_servers.x.command="npx"`。

## 工作区注意事项
- `.venv` 和 `.venv-1` 是本地虚拟环境，不要提交或修改。
- 修改安装脚本（`install.sh`、`install.ps1`）时，注意 Windows 上的 CI 冒烟测试。
