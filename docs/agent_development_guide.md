# MCagent / CrawlerAgent 开发文档

最后更新：2026-05-19 01:10

这份文档是当前项目的主开发文档。以后修改 MCagent、CrawlerAgent、RAG、SSE、前端交互或采集流程前，必须先读本文档；修改完成后，必须把本次决策、变更、验证结果追加到本文档。旧的 crawler_runbook.md 和历史乱码内容只作为历史参考，不再作为实现依据。

## 1. 核心原则

1. 系统里只有两个真实 Agent：MCagent 和 CrawlerAgent。用户是第三个参与者，但不是 Agent。
2. “仅检索”是 MCagent 的运行模式，不是第三个 Agent。
3. Agent 必须由 LLM 主导。工具函数只能做客观执行：检索、抓取、保存、去重、状态查询、证据抽取、入库、格式转换。
4. 工具不能代替 LLM 做最终回答、主观判断、任务取舍或自然语言组织。
5. 不允许为了当前测试语句写硬编码特例。若行为不对，优先改 Agent 的工具说明、prompt、上下文和验证反馈，而不是加关键词补丁。
6. MCagent 必须接收用户第一手原始输入，再结合当前会话上下文和工具能力决定下一步。
7. CrawlerAgent 是独立爬虫 Agent。用户和 MCagent 都可以委托它；它必须识别调用者、交付对象、数据用途，并自行规划采集。
8. 隐藏思维链不原样展示。界面只展示可观察进度、工具选择、证据来源和简短行动理由。
9. 所有中文源码、prompt、前端文本和文档必须保持 UTF-8。本文档会额外写入 UTF-8 BOM，方便 Windows PowerShell 和编辑器正确识别。

## 2. 三个参与者的关系

### 用户

用户可以直接问 MCagent，也可以切换到 CrawlerAgent 直接下采集任务。用户也可以让 MCagent 转达给 CrawlerAgent。

### MCagent

MCagent 是面向用户的问答 Agent。职责：

- 理解用户原话和当前会话上下文。
- 自己判断下一步是回答、查状态、仅检索，还是委托 CrawlerAgent。
- 普通问答必须先使用本地 RAG 工具找证据，再让 LLM 基于证据组织最终回答。
- 连续追问要回顾当前会话。例如先问“落幕曲新手怎么玩”，再问“有哪些 BOSS”，应理解为“落幕曲有哪些 BOSS”。
- 发现证据不足时，说明缺口，并把资料缺口交给 CrawlerAgent。

MCagent 可用工具：

- local_rag_search：检索本地向量库、全文线索、raw HTML 线索，返回候选证据。
- evidence_select：筛选和排序候选证据。
- status_monitor：读取采集、入库、任务、数据库进度。
- delegate_crawler：把资料缺口或用户转达任务交给 CrawlerAgent。
- final_answer_llm：基于用户问题、会话上下文和证据生成最终回答。

### CrawlerAgent

CrawlerAgent 是独立采集 Agent。职责：

- 理解任务来源：用户直连、用户经 MCagent 转达、MCagent 自主补库。
- 理解交付对象：给用户看，还是给 MCagent/RAG 入库使用。
- 自行规划搜索词、数据源、抓取顺序、清洗格式和验证方式。
- 保存 Markdown、manifest、raw HTML；需要给 RAG 用时，必须让标题、URL、metadata、chunk 都稳定可检索。
- 采集后总结新增了什么、哪些失败、下一轮该怎么补。

CrawlerAgent 可用工具：

- mcmod_search/scrape：MC百科搜索与页面抓取。
- modrinth_search：Modrinth 项目/整合包信息。
- tavily_search/extract：公网搜索和正文提取。
- firecrawl_search/scrape：公网搜索、页面正文、JS 页面辅助抓取。
- jina_search/reader：免费搜索和 URL 转 Markdown。
- web_discovery：公开搜索发现候选 URL。
- playwright_fallback：网页需要 JS、交互或页面结构复杂时兜底。
- raw_html_store：保存原始 HTML，便于后续从原文回查表格、图片和隐藏信息。
- ingest_to_rag：把采集资料清洗入库。

## 3. MCagent 工作流

1. 接收用户原始消息。
2. 读取会话摘要，必要时改写成带上下文的检索问题，但不能覆盖用户第一手意图。
3. MCagent LLM 做工具选择：answer、status、delegate_crawler。
4. 若选择 status，直接走状态工具，不做 RAG。
5. 若选择 answer，MCagent LLM 规划本地 RAG 子查询；工具执行检索并筛证据；最终回答由 LLM 生成。
6. 若选择 delegate_crawler，保留用户的采集目标和身份链，不替 CrawlerAgent 拆搜索词。
7. 若最终回答中 LLM 判断证据不足，才把缺口交给 CrawlerAgent。

注意：普通游戏问答里的“如何获取某物品”“怎么合成”“哪里打”不是 Crawler 采集任务，应先走本地 RAG 回答。

## 4. CrawlerAgent 工作流

CrawlerAgent 必须形成循环：

1. 理解：识别调用者、交付对象、主题实体、资料缺口和成功标准。
2. 规划：由 CrawlerAgent LLM 选择搜索词、数据源和采集顺序。
3. 行动前思考：每执行一个工具前，再由 CrawlerAgent LLM 看当前计划、已执行结果、失败原因和待执行任务，决定下一步。
4. 执行：工具按选择结果搜索、抓取、保存 raw HTML、生成 Markdown 和 manifest。
5. 验证：检查记录数、正文长度、主题相关性、重复、空结果、跑偏和错误。
6. 反思/重规划：空结果、跑偏、重复过多时，把失败摘要交给 CrawlerAgent LLM 重新选择方向。
7. 完成：当证据足够时由 CrawlerAgent LLM 决定结束，并总结新增资料、失败项和下一步建议。

## 5. RAG 与证据原则

1. Top K 只是候选获取参数，不是死板边界。
2. 工具可以提供候选和客观信号，但“证据是否足够回答”应由 LLM 结合问题判断。
3. 资料不足时，不能输出候选词垃圾列表。
4. 不能让脚本抽取结果伪装成最终答案。
5. 对整合包问题，优先考虑整合包内部资料、任务书、KubeJS、OpenLoader、modlist、官方页面和高信号攻略。
6. raw HTML 必须尽量保留。部分网页清洗后丢表格、图、折叠块时，可以回原始 HTML 查证。

## 6. SSE 与最终回答

1. /api/chat/stream 必须是真流式：trace 只做阶段提示，LLM 生成正文时发送 delta。
2. 前端收到 delta 后，在同一条 assistant 消息里实时追加文字，不在下面堆标签。
3. 不允许因为模型生成慢就中断并用本地抽取兜底。
4. 只有模型 API 明确失败、连接断开、HTTP 错误、空内容或乱码时，才显示模型失败信息。
5. 即使模型失败，也不能用工具抽取结果代替最终回答；只能告诉用户模型失败并保留证据来源。
6. 不关闭模型 thinking，不通过压缩思考换速度。token 参数可以为 auto，让模型完整思考和输出。

## 7. 前端交互原则

1. 两个 Agent 各有自己的会话列表；切换 Agent 时会话窗也应切换。
2. 可展开栏目必须由用户手动展开/收起，页面轮询和消息刷新不能自动改回收起。
3. Crawler 任务卡片必须让人能直接看懂：目标、状态、当前动作、成功/空结果/跑偏、CrawlerAgent 当前判断、下一步。
4. 调试 trace、证据和 Agent 对话应放进可展开区域；默认不压住最终回答。
5. 进度提示应像自然语言状态，而不是一堆后台字段。

## 8. 编码与文档要求

1. 每次开始改代码前先读本文档。
2. 每次完成后把变更和验证写回本文档。
3. 不再使用 PowerShell here-string 写中文源码或中文文档。
4. 若发现乱码，不继续在乱码旁边追加说明，直接修复或重写干净中文版本。
5. 验证以 Python/Node UTF-8 读取和 scripts/check_text_encoding.py 为准。

## 9. 已完成的重要修复记录

### 9.1 真流式与取消超时兜底

- mcagent/llm.py 增加 OpenAI-compatible stream_chat() / stream_events()。
- /api/chat/stream 在回答阶段发送 delta。
- 正常慢生成不再触发本地证据兜底。
- 前端接收 delta 后实时更新同一条消息。
- max_tokens 支持 auto；后端不关闭模型 thinking。

### 9.2 落幕曲资料深挖

- 找到并解析落幕曲 1.5.1 安装包。
- 抽取了 manifest、modlist、FTB Quests、KubeJS、OpenLoader、TaCZ、SlashBlade named_blades 等内部资料。
- 生成并入库了整合包内部高信号 Markdown 索引。
- 调整检索排序，让整合包内部资料优先于泛 Minecraft Wiki 噪声。

### 9.3 Agent 主导工具选择

- 新增 _agent_tool_decision()，由 MCagent LLM 判断 answer/status/delegate_crawler。
- 普通复合问答不再因为出现“获取”就直接触发 Crawler。
- RAG 检索规划默认由 LLM 生成子查询。
- Crawler 委托时保留采集目标，不由 MCagent 拆成固定搜索词。

### 9.4 CrawlerAgent 行动循环

- 新增 reflect_crawler_progress()。
- CrawlerAgent 每个工具动作前读取当前目标、计划、最近结果、待执行任务，再决定执行、追加、重规划或结束。
- 每次反思写入 plan.agent_reflections，供前端展示。

## 10. 本轮修复：2026-05-18 23:35

本轮开始前已重新阅读本文档和 crawler-stack-helper。

用户指出：我不该只解释，而要直接修改并测试两个 Agent；同时每次必须读开发文档、更新开发文档。

本轮修改：

1. 修复 MCagent 转交 Crawler 的真实目标：之前界面显示的是 LLM 提取后的 collection_target，但后台实际传给 _delegate_crawler_for_missing_data() 的仍可能是上下文化后的旧问题。现在后台真正使用 collection_target 创建 Crawler 任务。
2. Crawler job 的 readable 摘要增加 agent_reflection，前端可显示 CrawlerAgent 当前判断理由。
3. 前端 Crawler 任务卡片改成可展开详情，显示目标、状态、当前来源、当前搜索、当前动作理由、CrawlerAgent 判断、下一步。
4. 修复展开面板自动收起问题：toggle 事件来自已移除 DOM 节点时不再写回状态。
5. 右侧后台任务列表增加目标、当前查询和 Agent 判断摘要，减少“看不懂一堆后台字段”的问题。
6. 重写本文档为干净中文版本，并在验证后写入 UTF-8 BOM。

待验证命令：

~~~powershell
cd D:\magic\MC_Agent
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\retrieval_planner.py mcagent\llm.py
python scripts\check_text_encoding.py
node --check D:\magic\AgentConsole\static\app.js
~~~

必须测试：

1. MCagent：落幕曲新手该怎么玩？有哪些BOSS?拔刀剑有哪些？这些拔刀剑如何做如何获取？女仆有什么用？ 应选择 answer，先 RAG，不直接补库。
2. MCagent：状态 应选择 status，不先 RAG。
3. MCagent：告诉Crawler让他去获取落幕曲的BOSS列表与介绍 应选择 delegate_crawler，转交目标应是“落幕曲的BOSS列表与介绍”，并标记为用户经 MCagent 转达。
4. CrawlerAgent：帮 MCAgent 补充落幕曲女仆资料 应启动 Crawler job，并在 job result/readable 中出现 CrawlerAgent 反思理由。

## 11. 本轮验证结果：2026-05-18 23:50

已执行静态验证：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\retrieval_planner.py mcagent\llm.py
python scripts\check_text_encoding.py
node --check D:\magic\AgentConsole\static\app.js
~~~

结果均通过。服务重启后，/api/status 返回 200。

实际 Agent 行为验证：

1. 复合问答“落幕曲新手该怎么玩？有哪些BOSS?拔刀剑有哪些？这些拔刀剑如何做如何获取？女仆有什么用？”：MCagent LLM 选择 tool=answer，理由为复合游戏内查询，应使用本地知识库回答；没有直接补库。
2. “状态”：MCagent LLM 选择 tool=status，直接返回采集监控摘要，没有先 RAG。
3. “告诉Crawler让他去获取落幕曲的BOSS列表与介绍”：MCagent LLM 选择 tool=delegate_crawler，collection_target 为“获取落幕曲的BOSS列表与介绍”，delivery_target 为 MCagent/RAG，delegation 标记 requested_by=user_via_mcagent。
4. Crawler job readable 中已出现 agent_reflection，例如 CrawlerAgent 判断先通过 MC百科搜索“落幕曲”获取整合包基础信息和可能的 BOSS 线索。
5. 普通 RAG 问题“落幕曲有哪些BOSS呢”：MCagent LLM 选择 tool=answer，并在回答阶段发送 delta，确认是真流式输出。
6. 浏览器打开 http://127.0.0.1:8765 后，Agent 按钮只有 MCagent 和 Crawler 两个；页面快照未出现 undefined。“仅检索”仍作为 MCagent 的模式控件存在，不作为 Agent。

本轮发现并修正的额外问题：

- 工具选择 prompt 之前没有明确“用户经 MCagent 转达给 Crawler 的资料采集，默认交付给 MCagent/RAG”。已补充到 prompt。
- 前端展开面板在消息刷新时可能被旧 DOM 的 toggle 事件写回为关闭。已增加 isConnected 保护。
- Crawler 任务卡片和右侧任务列表现在显示目标、当前查询、当前动作理由和 CrawlerAgent 判断，便于直观看懂状态。

## 12. Crawler 整合包训练与 Utopia 首轮结果：2026-05-19 01:10

本轮开始前已重新阅读本文档和 `data/manual_research/crawler_training/modpack_full_collection_playbook.md`。本轮目标是把“落幕曲完整整合包采集流程”固化为可复用经验，并让 CrawlerAgent 迁移到乌托邦整合包。

### 12.1 代码修复

1. `crawler_llm_planner.py` 修复目标抽取：去掉“开始、复跑、采集”等动作词，避免把“开始采集乌托邦（Utopia）”当成主题。
2. `crawler_llm_planner.py` 增强 JSON 解析：优先提取首个平衡 JSON 对象，并容忍尾随逗号，减少 Crawler LLM 输出被 Markdown 或额外文本污染时的失败。
3. `web_server.py` 增加 `modpack_internal` 的客观边界：没有匹配本地安装包时，工具返回“未找到匹配本地包”，由 CrawlerAgent 决定下一步，不再把任意本地 zip 塞给它。
4. `web_server.py` 修复本地包匹配：只用实体词匹配安装包和旁路元数据，不再把“整合包、资料、下载页、任务线”等泛词算作匹配依据。验证结果：`乌托邦 Utopia 整合包` 不匹配落幕曲 zip，`落幕曲 Closing Song 整合包` 能匹配落幕曲 zip。
5. `web_server.py` 修复直接启动 Crawler 任务的来源链：`/api/jobs/start-crawler` 未显式传 agent 时默认视为用户直接委托 CrawlerAgent，不再误标为 MCagent 派单。
6. `web_server.py` 修复 Crawler 行动循环：当 CrawlerAgent 返回 `replan/add_tasks` 时，新任务插到当前执行位置优先运行；如果 CrawlerAgent 说明要重规划但未给出可执行任务，则再调用 Crawler 规划 LLM 把意图落实成工具任务。
7. `AgentConsole/static/app.js` 已支持把 Crawler job 更新写入同一条会话消息，让用户在会话框内看到自然语言进度，而不只看右侧后台字段。

这些修复的边界：工具只做客观校验、排队、执行和状态展示；是否搜索公网、是否下载包体、是否判定噪声，仍由 CrawlerAgent LLM 判断。

### 12.2 落幕曲训练结果

落幕曲训练已确认成功：

- CrawlerAgent 记住“完整整合包优先内部文件”的路线。
- 成功解析落幕曲 1.5.1 本地安装包，抽取 manifest、modlist、FTB Quests、KubeJS、OpenLoader、SlashBlade named_blades 等高信号资料。
- 入库后 documents/chunks 明显增长，MCagent 能基于本地资料回答落幕曲新手路线、Boss、拔刀剑、女仆等问题。
- 已写入可复用手册：`data/manual_research/crawler_training/modpack_full_collection_playbook.md`。

### 12.3 Utopia 首轮训练观察

任务 `1779122483965-1` 在修复 archive 匹配 bug 后被停止，停止时状态：

- 已完成 13 / 21 个采集动作。
- 成功 4，失败 9。
- 有效/可复用线索：
  - MC百科 `[UJ]乌托邦探险之旅 (Utopian Journey)`：`https://www.mcmod.cn/modpack/1337.html`
  - MC百科 `[UCST]理想国：科克肖特 (Utopia:Cockshott)`：`https://www.mcmod.cn/modpack/727.html`
  - Modrinth `Banana!`：CrawlerAgent 判断为 The Utopia server 的组件线索。
- CrawlerAgent 能识别并排除：
  - `TechTopiaUtils` 跑偏。
  - AoA 的“乌托邦盔甲/头盔”等同名物品跑偏。
  - Wikipedia / Project Gutenberg 的普通 “Utopia” 跑偏。
  - 错误喂入的 Closing Song 内部包跑偏。

结论：CrawlerAgent 已学会从落幕曲流程迁移出三件事：先找本地包、无本地包转项目页/下载源、对同名噪声做 LLM 判断。但 Utopia 还没有完成“完整整合包采集”，因为目标存在多实体歧义，且尚未找到可解析的 Utopia 安装包/manifest。

### 12.4 下一轮必须解决

1. Utopia 需要先消歧：`Utopian Journey`、`Utopia:Cockshott`、`The Utopia server` 不是同一个目标。CrawlerAgent 应先列候选并说明差异，再按用户/MCagent 目标继续采集，不应混成一个包。
2. Crawler 规划/反思的 JSON 输出仍偶尔不合法。下一步应给 Crawler LLM 调用增加更稳的 JSON 输出约束或 JSON 修复/重问机制；这属于结构化通信修复，不是代替 Agent 判断。
3. 需要增加“项目页 -> 下载/安装包/manifest 发现 -> 本地保存 -> modpack_internal 解析”的闭环工具能力。现有 Modrinth 能抓 `.mrpack` 内容，但 MC百科/CurseForge/论坛下载页到本地包体的落地链路还不完整。
4. Crawler 进度消息应继续保持自然语言状态，并在“规划中、工具执行中、LLM 反思中、入库中”之间明确区分。

## 13. 通用结构化浏览器采集：2026-05-19 14:10

本轮开始前已重新阅读本文档。用户要求验证 CrawlerAgent 是否像真正 Agent 一样使用工具，而不是固定脚本；测试目标从淘宝改为不需要登录的公开站点。

### 13.1 代码变更

1. 新增 `scripts/browser_collect_seed.py`，作为通用浏览器结构化采集工具。它可以打开公开页面，识别列表/卡片/商品项，保存 `items.csv`、`items.json`、`report.md`、`manifest.json`、`raw_page.html` 和截图。
2. CrawlerAgent 工具注册新增 `browser_collect`。Crawler LLM 可以在任务要求“字段、数量、保存目录、列表页结构化采集”时自行选择它。
3. `crawler_llm_planner.py`、`crawler_planner.py`、`provider_registry.py` 和 `web_server.py` 已接入该工具。
4. manifest 记录 `failure_reason`。如果遇到登录、验证、验证码或无可见结构化条目，工具只记录客观失败原因，不绕过限制。
5. 前端消息刷新改为：用户正在上翻展开详情时，不再被自动拉回底部；只有用户本来接近底部或新消息到来时才自动滚动。

### 13.2 验证结果

1. 淘宝测试：CrawlerAgent 选择 `browser_collect`，但页面返回登录/安全验证。工具保存 raw HTML、截图和失败原因，没有尝试绕过。
2. 公开测试站点 `https://webscraper.io/test-sites/e-commerce/allinone/computers/laptops`：CrawlerAgent 选择 `browser_collect`，成功保存 50 条商品记录到用户指定目录，字段包含名称、价格和链接。
3. 静态验证通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\provider_registry.py mcagent\crawler_planner.py
python scripts\check_text_encoding.py
node --check D:\magic\AgentConsole\static\app.js
~~~

### 13.3 原则确认

`browser_collect` 只是工具。它不决定采什么、不决定任务是否成功、不替 CrawlerAgent 总结；CrawlerAgent LLM 负责理解目标、选择工具、解释失败原因和决定下一步。

## 14. MCagent 计划式工作流：2026-05-19 15:10

本轮开始前已重新阅读本文档。用户指出：“本地资料现在有哪些乌托邦的数据、缺什么、让 Crawler 去找”是一个复合任务，不应该被 MCagent 路由成单纯状态查询。用户要求 MCagent 像 Codex 一样先列计划，再按计划做。

### 14.1 设计原则

1. 不写硬编码特例。由 MCagent LLM 识别复合任务，并返回计划。
2. 工具选择器只决定工具流程，不生成最终答案。
3. 对复合任务，MCagent 可返回 `planned_workflow` 和 `action_plan`。
4. `rag_focus` 由 MCagent LLM 生成，用于本地 RAG 检索。它必须去掉“本地资料、缺什么、让 Crawler 找”等元指令，保留真正主题。
5. 当前系统主要服务 Minecraft 资料库；若实体名有泛义且用户没有指定其他领域，`rag_focus` 不能只写裸实体，应带 Minecraft/整合包/模组等领域限定。
6. 计划式委托时，MCagent 先用 RAG 和 LLM 总结本地已有资料与缺口，再把简洁采集目标交给 CrawlerAgent；详细缺口摘要放入 `session_summary.mcagent_gap_summary`，供 CrawlerAgent 阅读后自行规划。

### 14.2 代码变更

1. `_agent_tool_decision()` 新增 `planned_workflow`、`action_plan` 和 `rag_focus`。
2. `_chat_impl()` 在工具选择后会把计划写入 trace：`plan.created` 和 `plan.rag_focus`。
3. RAG 检索使用 `rag_focus` 作为证据检索主题，最终回答仍基于用户原始问题和会话上下文。
4. `planned_workflow` 执行完本地 RAG 回答后，会按计划委托 CrawlerAgent，并把 MCagent 总结的缺口放入 Crawler 的 session_summary，而不是把长回答塞进任务标题。
5. 如果普通回答暴露资料不足，仍可触发 Crawler；但计划式工作流优先走计划式委托，避免旧的“缺口兜底分支”抢走流程。

### 14.3 验证结果

对问题“本地资料现在有哪些乌托邦的数据 缺什么 让Crawler去找”进行验证：

1. MCagent LLM 选择 `planned_workflow`。
2. action_plan 为三步：本地 RAG 检索、总结缺口、委托 CrawlerAgent。
3. `rag_focus` 输出为类似“乌托邦 模组 Minecraft 资料”，不再把“缺什么”当成检索主题。
4. SSE trace 依次出现 `decide.tool_selected`、`plan.created`、`plan.rag_focus`、`retrieve.*`、`answer.thinking`、`delta`、`delegate.planned_workflow`。
5. 最终回答以“执行计划：”开头，并在回答后启动 Crawler 任务。

### 14.4 GitHub 状态

当前 `D:\magic\MC_Agent` 和 `D:\magic\AgentConsole` 都不是 Git 仓库，本机也没有检测到 `gh` CLI。因此现在还不能直接推送到 `https://github.com/Akiyama-Fansora/`。可行路线：

1. 在本地新建一个统一仓库，包含 MC_Agent 后端与 AgentConsole 前端。
2. 添加远端到用户指定 GitHub 账号下的新仓库。
3. 用户完成 GitHub 登录或提供已配置好的 remote/token 后，再由 Codex 执行提交和推送。

推送前必须先整理 `.env`、API key、数据库、爬虫导出数据和大文件，避免把密钥或几百 MB 数据直接提交到公开仓库。

### 12.5 本轮验证

已执行：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py
python scripts\check_text_encoding.py
node --check D:\magic\AgentConsole\static\app.js
~~~

结果通过。服务已多次重启验证 `/api/jobs/start-crawler`、`/api/crawler/plan`、`/api/jobs/stop` 能工作。Utopia 首轮因发现工具边界问题而主动停止，下一轮应在修复后继续。


## 13. 本轮修复：2026-05-19 02:15

本轮开始前已重新阅读本文档，并阅读 crawler-stack-helper 与 playwright-helper。用户指出 Crawler 仍被过度限制，尤其 Firecrawl 额度不足时不应卡住，应该能主动使用浏览器采集；同时用户明确当前语境中的“乌托邦”指 Minecraft 整合包“乌托邦探险之旅 / Utopian Journey”。

### 13.1 真实状态核对

- 重启前真实 Python 进程只有两个：web_server 与一个 Crawler 子进程。
- 该子进程实际执行的是 `fetch_modrinth_seed.py --query ""`，说明旧任务卡在空查询上。
- 已通过 `/api/jobs/stop` 停止旧任务；重启服务后 `/api/jobs` 为空，右侧 2026-05-17 的 100% 是历史批量采集脚本进度，不是当前 Crawler 任务。

### 13.2 代码修复

1. Crawler 工具执行层新增空查询防护：空查询不是可执行动作，工具层会拒绝运行并把客观失败写回任务结果，交给 CrawlerAgent 反思/重规划，而不是启动无意义的 Modrinth/Jina/Playwright 请求。
2. Playwright 从“兜底”升级为一等浏览器采集工具：`crawler_llm_planner.py`、`crawler_planner.py`、`provider_registry.py` 和前端工具描述均更新为“浏览器搜索/采集/渲染 + raw HTML 保存”。
3. Crawler planner 与 reflection 的 token 上限提高，减少 Crawler LLM 输出半截 JSON 导致 fallback 的概率。
4. 修复 target_hint 抽取：优先使用会话中的 `collection_target`，并能识别“整合包「乌托邦探险之旅 / Utopian Journey」”这类引号目标，避免把目标错误清洗成泛词 Minecraft 或 Utopia。
5. 前端状态面板拆分：当前 Crawler 任务与历史批量采集脚本分开显示；批量进度改名为“批量采集脚本”，避免把旧的 finished 100% 理解成当前任务。
6. 写入 Crawler 记忆：当前会话里的“乌托邦”指“乌托邦探险之旅 / Utopian Journey”，不是普通乌托邦概念、Utopia:Cockshott 或同名服务器。

### 13.3 验证结果

已执行：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\crawler_planner.py mcagent\provider_registry.py
python scripts\check_text_encoding.py
node --check D:\magic\AgentConsole\static\app.js
~~~

结果均通过。

规划验证：使用 UTF-8 请求让 CrawlerAgent 规划“乌托邦探险之旅 / Utopian Journey”完整整合包采集。结果：

- topic/target_hint 正确为“乌托邦探险之旅 / Utopian Journey”。
- 任务包含：modpack_internal、MC百科、Modrinth、Playwright。
- Playwright 任务包括直接渲染 `https://www.mcmod.cn/modpack/1337.html`。

实跑验证：已启动任务 `1779127644117-1`。

- CrawlerAgent 先反思初始任务有泛查询风险，主动重规划为更短、更准的动作。
- 它先执行 Playwright 直接抓取 MC百科 modpack/1337 页面。
- 第一条直接页面与已有 Jina 资料重复，被识别为“可复用重复证据”，没有重复入库。
- 随后 CrawlerAgent 主动追加并执行 Playwright 抓取 `https://www.mcmod.cn/modpack/1337.html?tab=mods`，用于获取渲染后的模组列表。

当前结论：CrawlerAgent 已经比上一轮更接近“LLM 主导 + 浏览器工具辅助”的形态，但仍需继续观察本轮任务完成后的 collection_summary，确认是否真正拿到模组表、下载链接和教程正文。


补充：MCagent 的乌托邦检索同义线索已加入“乌托邦探险之旅 / Utopian Journey”。该代码变更已通过 py_compile 和编码检查；由于当前 Crawler 任务仍在运行，暂不重启服务，避免丢失内存中的任务状态。下次重启后生效。

## 14. 本轮修复：2026-05-19 通用 Crawler 与前端滚动

本轮开始前已重新阅读本文档，并阅读 crawler-stack-helper 与 playwright-helper。用户要求 CrawlerAgent 不再只服务 Minecraft，而要像一个偏数据采集方向的 Agent：由 LLM 识别目标、规划工具、执行采集、保存结果，并能处理任意公开数据采集任务。

### 14.1 原则确认

1. CrawlerAgent 仍是 LLM 主导：LLM 决定目标、字段、来源和工具动作。
2. 工具只负责客观执行：浏览器打开页面、保存 CSV/JSON/report/raw HTML/截图、返回状态。
3. 遇到登录、验证码、安全验证或反爬时，工具不得绕过；必须保存证据并向 CrawlerAgent 报告限制。
4. Planner 超时或失败时，fallback 只能保留用户已明确给出的结构化目标和目录，不能退回 Minecraft 专用搜索流程。
5. 前端流式刷新不能强制把用户滚动位置拽到底部；只有用户本来在底部时才自动跟随。

### 14.2 代码修改

1. 新增并接入 `scripts/browser_collect_seed.py`：通用浏览器结构化采集工具，输出 `items.csv`、`items.json`、`report.md`、`manifest.json`、`raw_page.html`、`page.png`。
2. `crawler_llm_planner.py` 已允许 `browser_collect`，并在 schema/prompt 中说明：结构化字段采集、商品/表格/列表、指定保存目录时应优先使用该工具。
3. `crawler_planner.py` 与 `provider_registry.py` 增加 `browser_collect` 工具描述，前端状态页也能显示这个工具。
4. `web_server.py` 接入 `browser_collect` 的执行命令，传递 `output_dir`、`max_items`、`start_url`、`timeout_ms`、`fields` 等参数。
5. 修复 Crawler 委托链路：不再把 `session_summary.collection_target` 覆盖成整句用户请求。用户或 MCagent 给出的真实采集目标会保留给 Planner。
6. 修复结构化采集 fallback：当 Crawler LLM 规划超时/失败且任务显式包含输出目录或字段时，fallback 使用 `browser_collect`，不再误入 MC百科/Modrinth 等 Minecraft 专用工具。
7. 修复浏览器采集站点上下文：如果用户目标中明确包含“淘宝/taobao”，而 LLM 把查询词缩短为“手机”这类商品词，执行层会把站点上下文传给工具，让它打开淘宝搜索页而不是默认 Bing。
8. `AgentConsole/static/app.js` 的 `renderMessages()` 增加近底部判断；用户展开过程详情并向上滚动时，后续 trace/delta 刷新不会自动拉回底部。

### 14.3 验证结果

已执行：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\crawler_planner.py mcagent\provider_registry.py scripts\browser_collect_seed.py
python scripts\check_text_encoding.py
node --check D:\magic\AgentConsole\static\app.js
~~~

结果均通过。服务已重启。

CrawlerAgent 测试任务：

- 用户目标：采集淘宝公开可见商品搜索结果中 50 个商品的名称、价格、链接，保存到 `C:\Users\67425\Desktop\front-end-practice\taobao-comments\1`。
- CrawlerAgent LLM 规划结果：选择 `browser_collect`，字段为 `name/price/url`，保存目录正确，查询词为“手机”。
- 首轮发现问题：工具只拿到短查询词“手机”，没有拿到站点上下文，误走 Bing。已修复。
- 修复后执行结果：工具打开 `https://s.taobao.com/search?q=手机`，保存了 `raw_page.html`、`page.png`、`manifest.json`、`report.md`、`items.csv`、`items.json`。
- 淘宝页面返回登录/安全验证环境；工具按原则没有绕过验证，因此 `items.csv/json` 为空，报告中说明了限制。

### 14.4 下一步

1. Crawler 的规划阶段仍可能在 DeepSeek 上等待较久。需要增加“复用最近成功计划 / 规划阶段可取消 / 规划中自然语言进度更细”的机制。
2. 对需要登录态的浏览器采集，应增加“用户授权浏览器 profile/storageState”的正规路径，由用户登录后 Crawler 复用授权状态，不做绕过。
3. Crawler 任务状态应区分“工具成功保存限制证据”和“采集到目标记录”，避免把受登录限制的合规结果简单显示为失败。

## 15. 本轮修复：2026-05-19 非登录站点采集验证

本轮开始前已重新阅读本文档，并阅读 crawler-stack-helper 与 playwright-helper。用户指出：淘宝需要登录就换不需要登录的网站测试；同时失败时应让 Crawler 能说明失败原因，而不是只显示失败。

### 15.1 代码修改

1. 重写 `scripts/browser_collect_seed.py` 的通用抽取逻辑：
   - 保留淘宝/Tmall/1688 专门链接识别。
   - 新增通用商品卡片识别：`.thumbnail`、`.card`、`.product`、`article`、`li` 等常见结构。
   - 识别标题链接、价格文本、详情链接，输出统一的 `name/price/url/source`。
   - `manifest.json` 增加 `failure_reason`，`report.md` 增加 Reason 区域。
2. `web_server.py` 的 `_crawler_manifest_stats()` 读取并透传 `status/note/failure_reason`。
3. `_crawler_result_summary()` 会把 `failure_reason` 写入 `next_actions`，让 Crawler 状态能说明失败原因。
4. 修复站点上下文判断：只有明确出现“淘宝”或独立单词 `taobao` 时，才给查询词补淘宝上下文，避免把路径名 `taobao-comments` 误判成淘宝任务。

### 15.2 验证结果

已执行：

~~~powershell
python -m py_compile scripts\browser_collect_seed.py mcagent\web_server.py
python scripts\check_text_encoding.py
~~~

结果通过。

直测公开练习电商站：

- URL：`https://webscraper.io/test-sites/e-commerce/allinone/computers/laptops`
- 输出目录：`C:\Users\67425\Desktop\front-end-practice\taobao-comments\demo-webscraper`
- 结果：成功采集 50 条商品记录，生成 `items.csv`、`items.json`、`report.md`、`manifest.json`、`raw_page.html`、`page.png`。

通过 CrawlerAgent 后台任务验证：

- 任务 ID：`1779167126975-1`
- CrawlerAgent LLM 规划结果：选择 `browser_collect`，直接打开目标 URL，字段为 `name/price/url`。
- 输出目录：`C:\Users\67425\Desktop\front-end-practice\taobao-comments\webscraper-crawler-test`
- 结果：成功采集 50 条商品记录。
- 样例：`Asus VivoBook / 295.99 / https://webscraper.io/test-sites/e-commerce/allinone/product/60`

结论：CrawlerAgent 已能完成非 Minecraft 的通用公开网页结构化采集；淘宝失败是目标站点登录/安全验证限制，不是通用采集链路失效。

### 15.3 下一步

1. 优化 Crawler 任务结果展示：当 `status=blocked_or_login_required` 且已保存 raw HTML/截图时，UI 应显示“受目标站限制，已保存证据”，而不是普通失败。
2. 规划阶段仍偏慢，应增加“已知结构化采集目标快速路径”：先由 LLM 判断是否需要完整规划；如果目标已经包含 URL、字段和输出目录，可以直接生成单个 `browser_collect` 动作。
3. 继续避免工具替代主观判断：快速路径也必须来自 Agent 的工具选择判断，而不是针对固定语句的关键词触发。

## 16. GitHub 仓库准备：2026-05-19

本轮开始前已重新阅读本文档。用户决定仓库名使用 `MC_Agent`，初期保持 Private，并要求说明何时可以公开。

### 16.1 仓库策略

1. 仓库先以 `D:\magic\MC_Agent` 为本地 Git 根目录。
2. `D:\magic\AgentConsole` 的前端静态文件复制到 `D:\magic\MC_Agent\frontend`，让 GitHub 仓库同时包含后端和前端。
3. 不把 `D:\magic` 整个目录变成 Git 仓库，避免误提交无关项目。
4. 不提交 `.env`、API key、Cookie、浏览器 profile、数据库、向量索引、crawler_exports、logs、runtime、整合包 zip 和其他大文件。
5. 远端仓库建议命名为 `Akiyama-Fansora/MC_Agent`，初始可见性为 Private。

### 16.2 公开标准

满足以下条件后再考虑从 Private 改为 Public：

1. 密钥和隐私数据全部确认不会进入 Git 历史。
2. README 能让新机器完成安装、配置、启动、导入和测试。
3. `config.sample.json` 足够完整，真实配置只保存在本地。
4. MCagent 的普通 RAG 问答、计划式工作流、状态查询、Crawler 委托测试通过。
5. CrawlerAgent 的公开网页采集、失败原因解释、RAG 入库测试通过。
6. 前端无明显乱码、undefined、自动滚动抢焦点等体验问题。
7. 开发文档清楚说明两个 Agent 的职责、工具边界、RAG/SSE/Crawler 工作流。

### 16.3 用户需要做什么

当前本机有 `git`，但没有 `gh` CLI，也没有可用的 GitHub remote。用户有两种选择：

1. 在 GitHub 网页手动创建空仓库 `MC_Agent`，保持 Private，不勾选 README/.gitignore/License，然后把远端地址发给 Codex。
2. 安装 GitHub CLI 并登录：`winget install --id GitHub.cli`，然后 `gh auth login`。登录后 Codex 可以用 `gh repo create` 创建远端仓库。

在远端准备好之前，Codex 只能完成本地仓库初始化、提交和 remote 预配置，不能直接推送。
