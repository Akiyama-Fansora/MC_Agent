# MC_Agent

MC_Agent 是一个面向 Minecraft 资料问答与资料采集的双 Agent 项目。系统里只有两个真实 Agent：

- `MCagent`：面向用户的问答 Agent，负责理解用户问题、读取会话上下文、检索本地 RAG 资料，并由 LLM 组织最终回答。
- `CrawlerAgent`：独立资料采集 Agent，负责理解采集目标、规划来源和工具、保存 Markdown/raw HTML/manifest，并把资料清洗成 MCagent 可检索的格式。

`仅检索` 是 MCagent 的运行模式，不是第三个 Agent。

## 主要能力

- 本地 RAG：读取 `data/crawler_exports` 中的 Markdown、HTML 清洗结果与结构化资料，导入 SQLite 与向量索引。
- MCagent：支持普通问答、上下文追问、状态查询、计划式工作流和 Crawler 委托。
- CrawlerAgent：支持 MC百科、Modrinth、Tavily、Firecrawl、Jina Reader/Search、Playwright/browser_collect 等来源和兜底工具。
- FastAPI 后端：提供网页 UI、SSE 流式回答、会话上下文接口、任务状态接口和 `/docs` 自动接口文档。
- 设置页：在 `/settings.html` 中配置多个 OpenAI-compatible/Ollama 模型档案，分别指定 MCagent 与 CrawlerAgent 使用的 LLM。

## 目录结构

```text
MC_Agent/
  api.py                         FastAPI 推荐入口
  web.py                         旧标准库 HTTP 后端，保留作回退
  chat.py                        命令行问答入口
  ingest.py                      本地资料导入与索引构建
  config.sample.json             配置模板
  .env.example                   环境变量模板
  frontend/                      内置网页前端
  mcagent/                       Agent、RAG、Crawler、后端服务代码
  scripts/                       采集、检查和维护脚本
  tests/                         本地场景测试
  docs/agent_development_guide.md 开发文档
```

## 安装

建议使用 Python 3.11+。

```powershell
cd D:\magic\MC_Agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果需要 Playwright 浏览器采集或前端验收：

```powershell
playwright install chromium
```

## 配置

复制配置模板：

```powershell
copy config.sample.json config.json
copy .env.example .env
```

真实 API key、Cookie、浏览器登录态和本地模型地址只放在本机 `.env`、`config.json` 或网页设置页中，不要提交到 Git。

也可以启动后打开设置页维护模型：

```text
http://127.0.0.1:8765/settings.html
```

设置页支持：

- 添加多个 LLM Profile。
- 配置 Base URL、API Key、模型名、temperature、max_tokens。
- 测试连接。
- 分别指定 MCagent 和 CrawlerAgent 的默认模型。

## 导入资料

把采集到的 Markdown/raw HTML/manifest 放到 `data/crawler_exports` 后运行：

```powershell
cd D:\magic\MC_Agent
python ingest.py
```

索引和数据库是可再生运行时产物，可以删除后重新导入。

## 启动网页

推荐启动 FastAPI 后端：

```powershell
cd D:\magic\MC_Agent
python api.py --host 127.0.0.1 --port 8765
```

打开：

```text
http://127.0.0.1:8765
```

自动接口文档：

```text
http://127.0.0.1:8765/docs
```

如需回退到旧标准库后端：

```powershell
cd D:\magic\MC_Agent
python web.py --host 127.0.0.1 --port 8765
```

## 命令行问答

```powershell
cd D:\magic\MC_Agent
python chat.py "落幕曲新手该怎么玩？"
```

只查看检索结果、不调用模型：

```powershell
python chat.py "拔刀剑怎么玩？" --no-llm --show-context
```

## Agent 边界

- Agent 必须由 LLM 主导。工具函数只能做客观执行：检索、抓取、保存、去重、状态查询、证据抽取、入库和格式转换。
- 工具不能替代 LLM 做最终回答、主观判断、任务取舍或自然语言组织。
- MCagent 接收用户第一手原始输入，再结合会话上下文和工具能力决定下一步。
- CrawlerAgent 是独立爬虫 Agent。用户和 MCagent 都可以委托它；它必须识别调用者、交付对象、数据用途，并自行规划采集。
- `/api/chat/stream` 使用 SSE 真流式输出。模型生成慢时不能用本地抽取结果替代最终回答；只有模型连接或 API 明确失败时才显示失败信息。

## 本地质量检查

```powershell
cd D:\magic\MC_Agent
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\crawler_delegation_service.py mcagent\crawler_reflection_service.py mcagent\evidence_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\job_view_service.py mcagent\rag_service.py mcagent\session_state.py mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\provider_registry.py mcagent\crawler_planner.py scripts\browser_collect_seed.py scripts\public_readiness_check.py tests\crawler_delegation_service_scenarios.py tests\crawler_reflection_service_scenarios.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\job_view_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\crawler_delegation_service_scenarios.py
python tests\crawler_reflection_service_scenarios.py
python tests\agent_execution_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\evidence_service_scenarios.py
python tests\job_view_service_scenarios.py
python tests\rag_service_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
```

仓库包含 GitHub Actions：`.github/workflows/ci.yml`。每次 push / pull request 会自动执行 Python 语法检查、UTF-8/乱码检查、公开准备检查、基础烟测和前端语法检查。
