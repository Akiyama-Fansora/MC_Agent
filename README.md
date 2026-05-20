# MC_Agent

这是一个本地双 Agent 项目：`MCagent` 负责面向用户问答和本地 RAG，`CrawlerAgent` 负责资料采集、清洗、保存和入库。两个 Agent 都由 LLM 主导，工具只负责客观执行。

当前仓库准备以 Private GitHub 仓库维护。运行时数据、密钥、本地数据库、向量索引、爬虫导出文件和大安装包不会提交到仓库。

## 目录

```text
D:\magic\MC_Agent
├─ chat.py                    # 问答入口
├─ ingest.py                  # 导入入口
├─ config.sample.json         # 配置样例
├─ mcagent\                   # 离线 RAG/Agent 代码
├─ frontend\                  # 本地网页控制台前端静态文件
├─ scripts\                   # AgentTest 协作与导出辅助脚本
├─ tests\smoke_test.py        # 不依赖 Ollama 的烟测
└─ data\
   └─ crawler_exports\        # 本地运行时数据入口，不提交 Git
```

运行时会生成：

```text
data\mcagent.sqlite           # 文档与 chunk
data\vector_index.npz         # NumPy 向量矩阵
data\llm_profiles.json        # 本机模型 URL / 模型名 / API Key 配置，不提交 Git
```

## 安装

```powershell
cd D:\magic\MC_Agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

如果你不想建虚拟环境，也可以直接用当前 Python。核心 RAG 只需要 NumPy；CrawlerAgent 的浏览器采集工具需要 Playwright 和 Chromium。

## 配置

复制配置样例：

```powershell
copy config.sample.json config.json
copy .env.example .env
```

默认模型与接口：

```text
model: qwen3-4b-agent-16k:latest
base_url: http://localhost:11434/v1
```

也可以用环境变量覆盖：

```powershell
$env:MCAGENT_OLLAMA_BASE_URL="http://localhost:11434/v1"
$env:MCAGENT_OLLAMA_MODEL="qwen3-4b-agent-16k:latest"
```

`.env` 只放本地密钥，不提交 Git。常用项：

```dotenv
LLM_API_KEY=
TAVILY_API_KEY=
FIRECRAWL_API_KEY=
FIRECRAWL_API_URL=
```

## 导入本地资料

先让另一个 worker 或你自己把导出的 `.md` / `.json` / `.html` / `.txt` 文件放入：

```text
D:\magic\MC_Agent\data\crawler_exports
```

然后执行：

```powershell
cd D:\magic\MC_Agent
python ingest.py
```

导入流程会：

1. 递归读取 `data\crawler_exports`。
2. 清洗 Markdown / JSON / HTML / TXT。
3. 切分 chunk，写入 SQLite。
4. 用本地确定性 Hashing char n-gram embedding 生成向量。
5. 写入 `data\vector_index.npz`。

导入器会跳过 WAF/503 阻断页、`qa_usable=false`、manifest、failure lesson 等采集审计文件，避免把采集失败证据当作可问答知识。

这个 embedding 不依赖联网模型，也不依赖 chromadb / sentence-transformers。以后要替换真实 embedding，可以在 `mcagent/embeddings.py` 增加新的 provider，并在 `config.json` 中切换。

## 和 AgentTest 爬虫 Agent 协作

如果要继续让 `D:\magic\AgentTest` 自己练习采集，可以从本项目启动一个有边界的种子任务：

```powershell
cd D:\magic\MC_Agent
python scripts\run_agenttest_mcmod_seed.py --page-limit 5
```

如果 AgentTest 只生成了 run 目录，没有自己导出到 `data\crawler_exports`，可以把某次 run 的 MCMod 相关证据整理出来：

```powershell
python scripts\export_agenttest_run.py "D:\magic\AgentTest\runs\某个run目录"
python ingest.py
```

注意：`D:\magic\AgentTest` 已加了一个轻量域名护栏。用户任务里明确出现 `mcmod.cn` 时，HTTP/browser 工具会阻止跑到其他公网根域，避免旧 SpiderDemo 训练逻辑把采集任务带偏。

## 问答

确保 Ollama 已经有模型并启动 OpenAI 兼容接口：

```powershell
ollama pull qwen3-4b-agent-16k:latest
ollama serve
```

然后提问：

```powershell
cd D:\magic\MC_Agent
python chat.py "工业时代2 的橡胶怎么获得？"
```

或者只看检索结果、不调用模型：

```powershell
python chat.py "工业时代2 的橡胶怎么获得？" --no-llm --show-context
```

答案会基于本地检索上下文生成，并在末尾列出来源。若本地资料库没有相关内容，会明确说明没有找到。

## 本地网页

启动本地 HTTP 控制台：

```powershell
cd D:\magic\MC_Agent
python web.py --host 127.0.0.1 --port 8765
```

然后打开：

```text
http://127.0.0.1:8765
```

网页支持：

- 多会话标签，多开浏览器窗口时按 session_id 隔离。
- 切换 `MCagent` 和 `Crawler` 两个真实 Agent；`仅检索` 是 MCagent 的模式，不是第三个 Agent。
- 主界面只保留当前模型选择、连接测试和设置入口；完整模型管理在 `/settings.html`。
- 在模型设置页维护多组 OpenAI-compatible / Ollama 配置，分别指定 MCagent 和 CrawlerAgent 使用的 LLM，并可测试连接。
- 流式显示 MCagent 思考状态、工具计划、LLM delta 输出和 Crawler 进度。
- 切换模型、温度、是否仅检索。
- 查看本地索引统计、crawler_exports 状态、最近 AgentTest run。
- 后台重新导入本地采集资料。
- 让 CrawlerAgent 自主规划多源采集、保存 raw HTML/Markdown/manifest，并按 MCagent/RAG 可读格式入库。

默认前端静态文件来自仓库内的 `frontend/`。如果你想使用外部前端目录，可以设置：

```powershell
$env:AGENT_CONSOLE_DIR="D:\magic\AgentConsole"
```

## 烟测

烟测使用临时目录创建一份小样本文档，不读取也不写入 `data\crawler_exports`，不需要 Ollama：

```powershell
cd D:\magic\MC_Agent
python tests\smoke_test.py
```

看到 `SMOKE TEST PASSED` 即代表导入、索引、检索和无 LLM 回答路径可运行。

公开前检查：

```powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\provider_registry.py mcagent\crawler_planner.py scripts\browser_collect_seed.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
```

仓库已提供 GitHub Actions：`.github/workflows/ci.yml`。每次 push / pull request 会自动执行 Python 语法检查、UTF-8/乱码检查、公开准备检查、基础烟测和前端语法检查。

## 边界约定

- Agent 必须由 LLM 主导。工具函数只能做检索、抓取、保存、状态查询、入库等客观执行。
- 不覆盖 `data\crawler_exports` 中已有文件。
- 本项目的导入器读取本地采集资料并写入可再生索引。
- SQLite 与 `.npz` 索引是可再生运行时产物，可以删除后重新导入。

## GitHub 公开标准

仓库先保持 Private。满足以下条件后再考虑公开：

1. `.env`、API key、Cookie、浏览器登录态、数据库、向量索引、爬虫导出数据和整合包安装包都被确认排除。
2. `README.md` 能让新机器完成安装、配置、启动、导入和测试。
3. `config.sample.json` 足够完整，真实配置只放本地。
4. MCagent 的基础问答、计划式工作流、状态查询和 Crawler 委托测试通过。
5. CrawlerAgent 的公开网页采集、失败原因解释、RAG 入库流程测试通过。
6. 前端无明显乱码、undefined、自动滚动抢焦点等问题。
7. 至少有一份开发文档说明 Agent 职责、工具边界、RAG/SSE/Crawler 流程。
8. `LICENSE` 不是 GitHub 公开仓库的硬性要求；没有 LICENSE 时，法律默认更接近“保留所有权利”，别人不能明确复用。若希望别人可复用或二次开发，再由仓库所有者选择 MIT、Apache-2.0 等协议。
