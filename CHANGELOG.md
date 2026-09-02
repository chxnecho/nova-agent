# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 的惯例。版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 安全加固 (hardening)
- **SSRF 防护**：`web_fetch` 默认拒绝私网/环回/链路本地/保留/组播地址;手动跟随重定向并对每一跳重新校验;新增 `tools.web.allowed_domains` 域名白名单与 `tools.web.allow_private` 开关。
- **Shell 加固**：黑名单改为空白规范化匹配(堵住 `rm -rf  /` 类绕过),并拒绝向 workspace 之外的绝对路径写入(`workspace_root` 守卫)。
- **API 限流**：为 `/api/*` 增加每 IP 令牌桶限流(`server.rate_limit_per_minute`,默认 300,0 表示关闭)。
- **安全响应头**：所有响应新增 `Content-Security-Policy`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`。
- **XSS 修复**：`app.js` `humanizeError` 的 fallback 现经过 HTML 转义,堵住错误横幅的注入点。

### 功能与健壮性
- **连续失败守卫**：`agent.max_consecutive_errors`(默认 0=关闭),连续工具失败达阈值即停止运行。
- **历史压缩(真正的上下文窗口)**：`memory.context_window_messages` / `memory.summarize_threshold` 现在真正生效——超过阈值时将最旧消息交给 LLM 压缩为摘要并截断。
- **可配置成本单价**：`agent.cost_per_mtok` 覆盖预算守卫默认单价。
- **日志轮转**：文件日志改用 `RotatingFileHandler`(默认 5MB × 5 份),不再无限增长。
- **大文件读取**：`read_file` 不再整文件读入内存,改为流式读取到 50KB 上限。
- **SQLite 连接复用**：`MemoryStore` 复用单条连接 + 锁,性能更好。
- **连接清理**：CLI `run`/`team` 用 `try/finally` 保证 `provider.aclose()`。

### 工程化
- **CI**：新增 GitHub Actions(`.github/workflows/ci.yml`)——Python 3.12/3.13 上跑 `ruff check`、`ruff format --check`、`pytest`。
- **Lint/Format**：引入 `ruff` 配置(`pyproject.toml [tool.ruff]`)及 `pre-commit` 钩子(`.pre-commit-config.yaml`)。
- **Provider 离线测试**：新增 `tests/test_provider.py`(httpx MockTransport),覆盖非流式/工具调用/429 重试/SSE 流式/用量累计。
- 修复 `web/server.py` chat 端点的不可达重复代码。

## [0.1.0] - 2026-08-28

初始版本：NovaAgent v0.1 —— 从零构建的自主 Agent 框架,含 Web 界面。
