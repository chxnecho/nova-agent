# NovaAgent

从零构建的自主 AI Agent 系统。不依赖 LangChain 等重型框架,核心循环、工具系统、记忆与多智能体编排全部手写,代码清晰可控。

## 功能总览

| 模块 | 能力 |
|---|---|
| **Agent 内核** | think → act → observe 自主循环;工具调用;失败后自动反思纠错;步数/成本双预算守卫;JSONL trace 全程留痕 |
| **LLM 抽象层** | OpenAI 兼容接口(OpenRouter/OpenAI/vLLM 等);流式 SSE 输出;指数退避重试;token 用量统计 |
| **工具系统** | 注册表 + JSON Schema;内置 11 个工具:`read_file` / `write_file` / `edit_file` / `list_dir`(沙箱)、`run_shell`(超时+黑名单)、`python_repl`(子进程隔离)、`web_fetch`、`remember` / `recall` / `ingest_document` / `search_knowledge` |
| **记忆 & RAG** | SQLite 向量存储 + 特征哈希嵌入(零外部依赖);文档分块索引;跨会话持久化 |
| **多智能体** | Planner 分解任务 → 多个 Executor 依次执行 → Critic 审查,不通过则修订重试(可配轮数),最后综合报告 |
| **Web 界面** | FastAPI + SSE 流式后端;单页聊天 UI 实时显示思考过程和每次工具调用 |
| **CLI** | `run`(自主任务)/ `chat`(多轮交互)/ `team`(团队模式)/ `serve`(启动 Web) |

## 快速开始

```bash
# 方式一:uv(推荐)——一条命令创建 .venv 并安装 server+dev 全部依赖
uv sync
cp .env.example .env              # 填入你的 OPENROUTER_API_KEY

# 方式二:传统 pip
python3 -m venv .venv
.venv/bin/pip install -e ".[server,dev]"
```

> 依赖已锁定在 `uv.lock`(提交到仓库),`uv sync` 可复现完全一致的环境。

配置在 `config/default.yaml`,可用 `NOVA_` 前缀环境变量覆盖(嵌套键用 `__` 连接),
例如 `NOVA_LLM__MODEL=qwen/qwen3-235b-a22b`。

### 使用

```bash
# 单 Agent 自主完成任务
.venv/bin/python -m nova.cli run "在工作区创建 fib.py 并运行验证" --workspace ./myproject

# 多轮对话(会记住上下文)
.venv/bin/python -m nova.cli chat

# Planner/Executor/Critic 团队模式
.venv/bin/python -m nova.cli team "创建一个 Python 模块并写测试验证" --workspace ./myproject

# Web 聊天界面 http://127.0.0.1:8321
.venv/bin/python -m nova.cli serve
```

## 架构

```
nova/
├── config.py             YAML + .env + NOVA_* 环境变量三层配置
├── log.py                结构化日志 + JSONL trace
├── llm/
│   ├── base.py           Message / ToolCall / Usage 数据模型
│   ├── provider.py       OpenAI 兼容 Provider(流式/重试/用量)
│   └── mock.py           离线 MockProvider
├── agent/
│   ├── core.py           ★ 自主循环内核 + 预算守卫 + 反思
│   ├── team.py           Planner/Executor/Critic 编排
│   └── prompts.py        提示词
├── tools/                工具注册表 + 文件/Shell/REPL/Web/记忆 工具
├── memory/
│   ├── embeddings.py     特征哈希嵌入(可替换为 API 嵌入)
│   └── store.py          SQLite 向量库 + 分块器
└── web/
    ├── server.py         FastAPI + SSE 流式接口
    └── static/           单页聊天前端
```

### Agent 循环

```
任务 → [LLM 思考] --有工具调用--> [执行工具] --> 观察结果回填历史 --> [LLM 思考] → …
              └--无工具调用---> 最终答案
失败时注入反思提示,引导换思路而非机械重试
步数上限 / 成本预算 双保险,全程 JSONL trace 可审计
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q     # 50 个单元测试,全部离线运行(MockProvider)
.venv/bin/ruff check .                   # lint
.venv/bin/ruff format --check .          # 格式检查
scripts/smoke_llm.py                     # 真实 API 冒烟测试(消耗少量 token)
```

## 生产部署

```bash
# 1. 专用沙箱工作区(不要指向含 .env / 密钥的目录)
nova serve --host 0.0.0.0 --workspace /srv/nova-sandbox

# 2. 开启 API 访问控制(浏览器会自动弹出令牌输入框)
export NOVA_WEB_TOKEN="$(openssl rand -hex 24)"

# 3. 生产环境建议置于 Nginx/Caddy 反代之后(TLS + 额外访问控制)
```

部署注意事项:

- **认证**:设置 `NOVA_WEB_TOKEN` 后所有 `/api/*` 端点要求 `Authorization: Bearer <token>`;
  未设置时服务只应绑定 `127.0.0.1`(启动时会给出醒目警告)。
- **工作区隔离**:web 模式下 Agent 的文件/Shell/REPL 工具锁定在 `--workspace` 目录内,
  启动时若检测到该目录含 `.env` 会红色告警。
- **资源治理**:空闲会话 1 小时后自动回收,已完成运行的事件缓冲保留 10 分钟,
  会话总数上限 200(超出时淘汰最旧),无需手动清理。
- **已知边界**:`web_fetch` 目前允许抓取任意 URL(含内网地址)。面向公网部署时,
  建议在反向代理层限制出站目标,或在内网环境中禁用该工具(`config/default.yaml`
  中 `tools.web.enabled: false`)。

## 设计取舍说明

- **不用 LangChain**:核心循环仅 ~200 行,行为完全透明可控,便于调试和定制。
- **哈希嵌入兜底**:语义检索质量不及神经嵌入,但零依赖、确定性、离线可用;
  `MemoryStore` 接受任意 embedder,接入 API 嵌入只需实现 `embed()`。
- **安全边界**:文件工具沙箱锁定 workspace;Shell 有危险命令黑名单与超时;
  REPL 在子进程中执行;所有错误以观察结果形式返回给模型而不中断循环。
