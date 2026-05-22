# MCagent / CrawlerAgent 开发文档

最后更新：2026-05-22

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
- 发现证据不足时，先向用户说明缺口；只有当 MCagent 在工具选择或 planned workflow 中明确选择 `delegate_crawler` 时，才把资料缺口交给 CrawlerAgent。

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
7. 若最终回答中 LLM 判断证据不足，只说明缺口和可补充方向；不能由回答文本扫描或工具层自动把缺口交给 CrawlerAgent。只有本轮工具选择/planned workflow 明确委托时才启动 Crawler。

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

### 16.3 GitHub 当前状态

用户已完成 GitHub CLI 登录，账号为 `Akiyama-Fansora`。GitHub CLI 安装在 `D:\magic\GitHubCLI\bin\gh.exe`。

当前远端仓库：

- 仓库：`Akiyama-Fansora/MC_Agent`
- 地址：`https://github.com/Akiyama-Fansora/MC_Agent`
- 可见性：Private
- 分支：`main`

以后每轮较大修改后，应执行公开检查、提交并推送。公开前仍需人工确认 Git 历史中没有密钥、大数据文件、Cookie、浏览器 profile 或个人隐私资料。

## 17. 本轮公开标准优化：2026-05-19

本轮开始前已重新阅读本文档。用户要求“继续优化整个项目，做到所有公开标准”。本轮目标不是增加新特性，而是让项目更接近可公开维护状态：仓库结构清晰、测试可跑、Agent 行为可验证、文档同步。

### 17.1 代码与仓库变更

1. 前端已纳入主仓库：`frontend/index.html`、`frontend/static/app.js`、`frontend/static/app.css`。后端默认从 `PROJECT_ROOT / "frontend"` 读取前端文件，同时保留 `AGENT_CONSOLE_DIR` 覆盖能力。
2. 增加 `.env.example`，只放空占位，不放真实 key。
3. 增加 `data/README.md` 和 `data/.gitkeep`，说明真实数据库、向量索引、crawler_exports 等只保存在本机，不进入 Git。
4. 扩展 `.gitignore`，忽略 `.env`、密钥文件、logs、runtime、数据库、索引、归档包、crawler_exports 等。
5. 新增 `scripts/public_readiness_check.py`，检查公开仓库必备文件、误提交数据目录、疑似密钥和 README 关键说明。
6. `README.md` 当时补充两 Agent 架构、前端目录、环境变量、Playwright 安装、公开前检查命令和公开仓库标准。后续第 36 章已修正：README 只面向使用者，公开标准不再放入 README。
7. `requirements.txt` 补充 `playwright>=1.40`。

### 17.2 Agent/RAG 修复

1. 修复 CrawlerAgent 直连角色判断：当 active agent 是 `crawler_agent` 且用户直接下采集/保存/补库任务时，LLM 工具选择应进入 `delegate_crawler`，并标记为用户直接委托，而不是误当 MCagent 派单。
2. 修复 Utopia / Utopian Journey 模组清单证据链：
   - 识别“模组列表、模组清单、included mods、mod list、包含模组”等通用表达，不只认“有哪些模组”。
   - 本地 MC百科整合包页面中解析出的“包含模组 (N)”会转换成客观证据块，交给 MCagent LLM 组织最终回答。
   - 证据块说明“上下文节选前 180 个，完整清单仍保留在来源页面”，避免模型误以为本地只保存了前 180 个。
   - 在最终证据选择后再次确保整合包清单证据进入上下文，避免被其他补充证据挤掉。
3. 修复 smoke 脚本的 SSE 读取方式：不再 `response.read()` 等连接关闭，而是按 SSE 块读取，收到 final `response` 即结束。
4. smoke 脚本拆成默认快速 smoke 与可选全量 smoke：
   - 默认快速 smoke 覆盖 status 路由、MCagent 转交 Crawler、CrawlerAgent 直连委托、RAG delta 流式、Utopia 模组清单证据。
   - `MCAGENT_SMOKE_FULL=1` 时再跑长上下文矩阵，适合人工验收，不适合作为每次快速回归。

### 17.3 验证结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\provider_registry.py mcagent\crawler_planner.py scripts\browser_collect_seed.py scripts\public_readiness_check.py scripts\smoke_agent_flows.py
python scripts\check_text_encoding.py
python tests\smoke_test.py
node --check frontend\static\app.js
python scripts\public_readiness_check.py
$env:MCAGENT_TEST_MODEL='cloud:deepseek:deepseek-v4-pro'; python scripts\smoke_agent_flows.py
~~~

快速 Agent smoke 结果：

- `status_routes_to_tool` 通过。
- `progress_routes_to_tool` 通过。
- `mcagent_delegates_utopia_collection` 通过。
- `mcagent_delegates_closing_song_boss_collection` 通过。
- `rag_beginner_guide_has_answer_trace` 通过，确认有 retrieve trace 和 delta 流式事件。
- `crawler_direct_user_delegation` 通过。
- `rag_utopia_mod_list` 通过，确认模型回答引用了 Utopian Journey 模组数量，证据上下文包含 `Immersive Aircraft` 等解析出的模组名。

本轮还确认 `/api/agents` 只返回两个真实 Agent：`MCagent` 和 `Crawler`；“仅检索”继续作为 MCagent 模式，不作为第三 Agent。

### 17.4 剩余公开前事项

1. 公开前仍需检查 Git 历史，确认历史 commit 中没有真实 API key。当前工作树通过 `public_readiness_check.py`，但公开仓库应额外做历史扫描。
2. 全量 `MCAGENT_SMOKE_FULL=1` 依赖云模型速度，适合人工验收，不建议作为默认 CI。
3. 右侧历史批量采集进度仍容易误解为当前运行任务；后续前端应把 finished batch progress 标成“历史批量任务”，并弱化旧 PID/命令字段。
4. Utopia 已能回答模组清单节选，但若用户要完整 423 条，应增加“导出完整清单到文件/表格”的交互，而不是把全部塞进一次聊天回复。

## 18. MCagent 到 CrawlerAgent 的上下文交接：2026-05-19

本轮开始前已重新阅读本文档。用户指出：“让 Crawler 补全你缺的资料”这类委托不是搜索词，也不应该用固定指代词规则处理；正确做法是让 MCagent 保持上下文记忆，并在转交 CrawlerAgent 时完整说明自身或用户转达的需求。

### 18.1 修正原则

1. 不再用“缺的/上述/刚才/这些”等固定词表来决定是否改写委托目标。
2. MCagent 的委托目标不是搜索词，而是给 CrawlerAgent 的自然语言任务目标。
3. 每次 MCagent 委托 CrawlerAgent 时，都生成 `handoff_brief`，包含调用关系、用户原话、转达目标、相关会话背景、已知资料缺口、交付对象和交付要求。
4. `handoff_brief` 由 MCagent LLM 根据用户原话、会话摘要和本轮回答/缺口生成；工具只负责传递这个交接摘要，不替 Agent 决定搜索策略。
5. CrawlerAgent 收到任务后应阅读 `handoff_brief`、`mcagent_gap_summary`、`current_topic`、`missing_evidence` 等上下文，再自行规划搜索词、来源和清洗方式。

### 18.2 代码变更

1. 会话摘要新增 `gaps` 字段，从 MCagent 历史回答中的“缺口/不足/未找到/需要补充”等资料缺口段落中抽取客观缺口句，供下一轮上下文理解使用。
2. 新增 `_build_delegate_handoff_brief()`：让 MCagent LLM 生成完整交接摘要，替代旧的指代词补丁。
3. `delegate_crawler` 分支和 `planned_workflow` 分支都会把 `handoff_brief` 写入 `session_summary`，随任务交给 CrawlerAgent。
4. 修复 planned_workflow 的早退问题：如果本地证据筛选不足，以前会绕过计划式交接并用原句派单；现在也会生成 `handoff_brief` 后再交给 CrawlerAgent。
5. Crawler planner 的上下文读取增加 `handoff_brief`、`mcagent_gap_summary` 和 `gaps`，fallback 规划也优先参考 `current_topic`，减少“补全你缺的资料”这种原句被当成主题。

### 18.3 验证

针对复现场景进行了验证：

1. 历史上一轮用户问“介绍一下乌托邦之旅有哪些玩法”，MCagent 回答里包含玩法教程、版本差异、具体系统介绍等资料缺口。
2. 下一轮用户输入“让Crawler补全你缺的资料”。
3. MCagent 选择 `planned_workflow`，先理解会话缺口，再委托 CrawlerAgent。
4. trace 中出现 `delegate.handoff_brief`，内容明确包含：
   - 用户原话。
   - 当前主题“乌托邦之旅/乌托邦探险之旅”。
   - 玩法教程、任务线、阶段攻略缺口。
   - 3.2/3.5 与 3.0 版本玩法差异缺口。
   - 多维度、交易、烹饪等系统介绍缺口。
   - 交付对象为 MCagent/RAG。
5. 已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py
python scripts\check_text_encoding.py
python tests\smoke_test.py
python scripts\public_readiness_check.py
~~~

## 19. 本轮修复：2026-05-19 编码防护、包体发现与行动确认

本轮开始前已重新阅读本文档，并按用户要求优先处理两个问题：一是不能再出现乱码任务污染 UI；二是两个 Agent 每一步都要先确认下一步，而不是脚本直接推进。

### 19.1 编码与乱码防护

1. 确认项目维护文件本身是 UTF-8：`python scripts\check_text_encoding.py` 通过；额外扫描 Git 跟踪文本也未发现替换字符、长问号串或常见 mojibake。
2. 本轮 UI 中出现的连续问号加 `Utopian Journey` 文本不是源码文件乱码，而是一次通过 PowerShell 发送中文 JSON 时输入已经变成问号，后端按原样保存到了内存 job。
3. `/api/jobs/start-crawler` 已增加输入损坏防护：如果请求内容含替换字符或连续问号，会返回 400，不再创建 Crawler job，避免坏数据进入任务列表、记忆或资料库。
4. 重启服务后，内存中的乱码 stopped job 已清空；后续启动中文 Crawler 任务必须使用 Web UI 或 UTF-8 客户端。

### 19.2 Crawler 新增整合包包体发现工具

新增 `modpack_download` 工具与脚本 `scripts/fetch_modpack_archive_seed.py`：

1. 先搜索 Modrinth modpack 项目并尝试发现 `.mrpack` 文件。
2. 再通过公开搜索页发现 `.mrpack` 或 `.zip` 直链。
3. 若发现可直接公开下载的包体，保存到 `data/manual_research/modpack_archives/<主题>/pack_archive`，后续由 `modpack_internal` 解析 manifest、modlist、任务书、脚本和配置。
4. 若没有公开直链，生成 Markdown/manifest 报告并写明原因，不绕过登录、付费、网盘会员、验证码或私有下载限制。
5. Provider Registry、Crawler 工具清单、Planner schema、行动反思 prompt、执行器和超时配置均已接入 `modpack_download`。

### 19.3 两个 Agent 的下一步确认

MCagent 新增 `_agent_confirm_next_step()`：

1. 工具选择后，MCagent LLM 会确认下一步工具路径是否合理。
2. 执行 `delegate_crawler`、`status`、`local_rag_search`、`final_answer_llm` 前，都会输出 `next_step_confirmed` trace。
3. 这个确认器只确认下一步工具动作，不回答用户问题，不生成最终答案，不替 Crawler 拆搜索词。

CrawlerAgent 已有的行动循环继续保留：

1. 初始计划只提供候选任务队列。
2. 每个工具动作前，CrawlerAgent LLM 读取目标、计划、最近结果和待执行任务，再决定执行哪个 pending task、是否加任务、是否重规划或结束。
3. 本轮乌托邦任务中，CrawlerAgent 没有死跑优先级最高的 `browser_collect`，而是先反思后选择 Playwright 打开 MC百科核心页面，再选择 `modpack_download` 尝试包体发现。

补充修正：

4. Crawler 规划阶段不再因为外层 120 秒计时就切到规则 fallback。只要 LLM 请求仍在进行，就持续显示“正在规划”；只有模型请求自身失败、断链或返回错误时，才进入失败处理。这样避免脚本在模型仍思考时替代 CrawlerAgent 做规划。
5. Crawler planner / reflection 的 JSON 解析失败时，不再立刻进入规则 fallback；会先把模型原始输出交回 LLM 进行 JSON 修复。只有修复仍失败时，才承认本轮模型结构化输出不可用。

### 19.4 当前乌托邦任务观察

新任务 `1779198250425-1` 已用 UTF-8 正常启动：

- 目标：乌托邦探险之旅 / Utopian Journey 整合包。
- 交付对象：MCagent/RAG。
- Crawler 计划包含：Playwright、modpack_download、web_discovery、browser_collect、modpack_internal、Tavily、Firecrawl。
- 第一步行动前反思：先打开 MC百科页面保存完整 HTML，作为核心页面证据。
- 第二步行动前反思：尝试寻找并下载公开 `.mrpack/.zip` 包体，若成功再解析内部资料。

当前包体发现工具已能记录“未找到公开包体直链”的客观原因。下一步需要继续观察新任务 `1779198967433-1` 的 collection_summary，并确认它是否通过 web_discovery / Playwright / Tavily 找到足够的玩法、任务线、版本差异和系统资料。

### 19.5 验证

已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\crawler_planner.py mcagent\provider_registry.py scripts\fetch_mcmod_seed.py scripts\fetch_modpack_archive_seed.py
python scripts\check_text_encoding.py
python scripts\fetch_modpack_archive_seed.py --query "乌托邦探险之旅" --limit 3 --no-download
~~~

注意：服务已重启；当前运行中的 Crawler job 是重启后新建的 UTF-8 正常任务。

## 20. 公开 GitHub 准备审计：2026-05-19

本轮开始前已重新阅读本文档。用户要求总览项目距离公开 GitHub 还差什么，并继续优化。审计范围包括仓库结构、密钥、大文件、运行时数据、README、CI、公开检查脚本、当前 Crawler 任务状态。

### 20.1 审计结论

当前仓库已经具备 Private 维护条件，距离 Public 公开还差一个需要用户决策的事项：

1. `LICENSE` 尚未选择。授权协议属于仓库所有者的法律/开源策略选择，工具和 Agent 不能替用户擅自决定。公开前建议在 MIT、Apache-2.0、GPL 等协议中选择一种并加入仓库。

已确认通过的项目：

1. 工作区跟踪文件未发现 `sk-`、`tvly-`、`fc-`、GitHub token 或 Bearer token 形式的密钥。
2. Git 历史当前 4 个 commit 未发现上述密钥模式。
3. Git 跟踪文件没有超过 1 MB 的大文件。
4. `data/` 下运行时资料、数据库、向量索引、Crawler 导出、大压缩包仍被 `.gitignore` 排除。
5. `README.md`、`.env.example`、`config.sample.json`、主开发文档和前端目录都已存在。

### 20.2 本轮代码与仓库变更

1. 新增 `.github/workflows/ci.yml`，push 和 pull request 时自动执行：
   - Python 语法检查。
   - UTF-8/乱码检查。
   - 公开准备检查。
   - 基础烟测。
   - 前端 JavaScript 语法检查。
2. 增强 `scripts/public_readiness_check.py`：
   - 将 CI workflow 纳入必备文件。
   - 扫描 Git 历史中的疑似密钥。
   - 检查跟踪文件大小，防止大数据误提交。
   - 对缺少 `LICENSE` 给出 warning，不替用户做授权选择。
3. 更新 `README.md`，说明 CI 已接入，并把 LICENSE 选择列为公开前事项。
4. 修复 GitHub Actions 首轮发现的 Python 3.11 兼容问题：`web_server.py` 中 f-string 表达式不再直接包含反斜杠转义字符串，改为提前计算变量。原则上 CI 使用 Python 3.11，因此本地通过 Python 3.13 不代表公开检查一定通过。

### 20.3 当前 Crawler 任务观察

最近的乌托邦任务 `1779199297176-1` 已结束：

- 状态：succeeded。
- 成功：2。
- 失败：28。
- 主要失败原因：公开 `.mrpack/.zip` 包体未找到，多个公开搜索源空结果或跑偏。
- 可用线索：已复用 MC百科 Utopian Journey 页面等已有证据。

这说明 CrawlerAgent 已能做目标判断、去噪和失败记录，但乌托邦完整内部资料仍没有落幕曲那样的包体级证据。后续若要补齐乌托邦，优先路径应是：

1. 继续从 MC百科页面、教程页、下载页和社区页抓取可公开内容。
2. 如果用户能提供免费可下载的整合包压缩包，再走 `modpack_internal` 解析内部 manifest、任务书、配置和模组清单。
3. 若下载源受网盘会员、登录、验证码限制，CrawlerAgent 只能记录限制和证据，不能绕过。

### 20.4 验证命令

已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\provider_registry.py mcagent\crawler_planner.py scripts\browser_collect_seed.py scripts\fetch_mcmod_seed.py scripts\fetch_modpack_archive_seed.py scripts\public_readiness_check.py scripts\smoke_agent_flows.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
node --check frontend\static\app.js
~~~

GitHub Actions 首轮运行失败在 Python 3.11 语法检查；修复后已在本地重新执行上述命令并通过，随后重新提交触发 CI。

CI 第二轮已通过。GitHub 同时提示 Node 20 action runtime 和 `windows-latest` 即将迁移；为减少公开后的维护噪声，workflow 已固定到 `windows-2025`，并设置 `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`，Node 版本提升到 24。

`public_readiness_check.py` 当前通过，但会提示：

~~~text
LICENSE is missing; choose an open-source license before making the repository public.
~~~

## 21. 网页模型配置与连接测试：2026-05-20

本轮开始前已重新阅读本文档。用户要求在网页里增加设置栏，让用户自己配置 URL、KEY、LLM 名称，支持添加多个模型、测试连接、方便切换，并能分别设置 MCagent 与 CrawlerAgent 使用的 LLM。

### 21.1 设计原则

1. API Key 属于本机运行时配置，不进入 Git。后端保存到 `data/llm_profiles.json`，该目录已被 `.gitignore` 排除。
2. 后端不会把原始 API Key 回显给前端；前端只显示是否已保存 key。需要修改 key 时重新输入。
3. 模型配置是工具配置，不改变 Agent 原则。MCagent 和 CrawlerAgent 仍由各自 LLM 主导，只是 LLM endpoint 可以在 UI 中切换。
4. CrawlerAgent 的 planner、反思和相关性判断默认使用分配给 `crawler_agent` 的 profile；MCagent 的工具选择、行动确认和最终回答使用分配给 `mcagent_rag` 的 profile。

### 21.2 代码变更

1. 新增 `mcagent/llm_profiles.py`：
   - 读取默认 Ollama 配置。
   - 提供默认 Ollama profile 和无 key 的 DeepSeek 模板；模型 API Key 只由用户在设置页填写，不再从 `.env` 或旧 AgentTest `llm.env` 自动搬运。
   - 保存/读取 `data/llm_profiles.json`。
   - 根据 Agent 分配生成 OpenAI-compatible client。
   - 提供连接测试函数。
2. `web_server.py` 新增接口：
   - `GET /api/llm-profiles`
   - `POST /api/llm-profiles`
   - `POST /api/llm-profiles/test`
3. `web_server.py` 的 `_selected_llm_client()` 支持 `profile:<id>` 模型值；聊天请求可传 `model_profile_id`。
4. `crawler_llm_planner.py` 的 Crawler planner 改为读取 CrawlerAgent 分配的 LLM profile，不再只依赖旧 `llm.env`。
5. 前端 `frontend/index.html` / `frontend/static/app.js` / `frontend/static/app.css` 增加“模型设置”：
   - 当前 Agent 模型切换。
   - MCagent/CrawlerAgent 独立分配。
   - 新增、保存、删除 profile。
   - Base URL、模型名、API Key、类型、超时秒配置。
   - 测试连接按钮。
6. `public_readiness_check.py` 把 `mcagent/llm_profiles.py` 纳入公开必备文件。

### 21.3 验证

已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\llm_profiles.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
~~~

已重启本地服务并验证：

1. `GET /api/llm-profiles` 返回 `ollama-default` 和由用户保存的模型 profile。
2. `POST /api/llm-profiles` 能保存 MCagent/CrawlerAgent 分配。
3. `POST /api/llm-profiles/test` 测试 DeepSeek profile 成功，返回 `OK`。

## 22. 模型设置独立页面与 LICENSE 说明（2026-05-20）

本轮开始前已重新阅读本文档。用户确认：`LICENSE` 好像不是必须的，GitHub 仓库可以直接公开；同时用户认为主页面组件过多，希望把 Key、URL、模型名等配置从聊天页挪到专门的设置页面。

### 22.1 设计原则

1. `LICENSE` 不是 GitHub 公开仓库的技术硬性要求；仓库可以没有 LICENSE 直接公开。
2. 但没有 LICENSE 时，法律默认更接近“保留所有权利”，别人没有明确的复制、修改、分发许可。公开检查只给 warning，不阻止发布，也不替用户选择协议。
3. 主聊天页应专注会话、Agent 选择、模型快速切换和状态观察，不再塞入完整 Key/URL 表单。
4. `/settings.html` 承担完整模型管理：新增、保存、删除、测试连接，以及分别分配 MCagent/CrawlerAgent 的 LLM。
5. API Key 仍只保存在本机 `data/llm_profiles.json`；后端不把原始 Key 回显给前端。

### 22.2 代码变更

1. 新增 `frontend/settings.html` 与 `frontend/static/settings.js`，形成独立模型设置页。
2. `frontend/index.html` 的侧栏模型区改为：当前模型下拉框、测试连接按钮、设置页入口。
3. `frontend/static/app.js` 删除主页面对旧设置表单 DOM 的事件依赖；主页面只保存当前 Agent 的模型分配。
4. `frontend/static/app.css` 新增设置页布局、设置卡片、链接按钮样式。
5. `web_server.py` 新增 `/settings` 与 `/settings.html` 路由。
6. `public_readiness_check.py` 把设置页文件纳入必备文件，并把 LICENSE 提示改为 warning：可公开，但缺少复用授权。
7. `README.md` 更新模型设置入口和 LICENSE 公开说明。

### 22.3 验证要求

完成本轮后必须执行：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\llm_profiles.py scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
~~~

### 25.7 本轮实际验证

已执行并通过：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
~~~

`public_readiness_check.py` 当前通过；缺少 `LICENSE` 仍仅作为 warning，不阻止 GitHub 公开。

还要重启本地服务并确认：

1. `http://127.0.0.1:8765/settings.html` 可以打开。
2. `GET /api/llm-profiles` 正常返回 profile 列表。
3. 主聊天页切换当前模型不会依赖已移除的设置表单。

### 22.4 本轮实际验证

已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\llm_profiles.py scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
~~~

已重启 `http://127.0.0.1:8765` 并验证：

1. `GET /settings.html` 返回 200。
2. `GET /api/llm-profiles` 返回 `ollama-default` 与模型配置，且不回显原始 API Key。

公开检查当前通过；缺少 LICENSE 仅作为 warning，不阻止公开仓库。

## 23. 移除自动导入 DeepSeek Key（2026-05-20）

本轮开始前已重新阅读本文档。用户要求：本地保存的 DeepSeek key 不要直接带进项目，后续由用户自己在设置页填写。

### 23.1 原则

1. 模型 API Key 只属于用户在 `/settings.html` 中显式保存的运行时配置。
2. 项目启动时不得从旧 AgentTest `llm.env`、仓库 `.env` 或其他外部文件自动搬运 DeepSeek key。
3. 可以保留无 key 的 DeepSeek 模板，方便用户在设置页填写；默认分配仍使用 Ollama，避免无 key 云模型导致默认失败。
4. `data/llm_profiles.json` 是本机运行时文件，不进入 Git；本轮已清理本机文件中的 DeepSeek key。

### 23.2 代码变更

1. `llm_profiles.py` 删除自动读取 `.env` / AgentTest `llm.env` 生成 `deepseek-env` 的逻辑，改为提供 `deepseek-template` 空 key 模板。
2. `retrieval_planner.py` 改为使用分配给 MCagent 的 LLM profile，不再直接读取 AgentTest `llm.env`。
3. `web_server.py` 的旧 `cloud:deepseek:*` 路径不再读取 AgentTest `llm.env`，只会使用设置页保存过的 `deepseek-template` profile 或空 key 模板。
4. `crawler_llm_planner.py` 移除未使用的 AgentTest env 读取代码。
5. `.env.example` 与 `README.md` 移除 `LLM_API_KEY` 示例，明确模型 API Key 在 `/settings.html` 配置。

### 23.3 验证要求

完成本轮后必须执行：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\retrieval_planner.py mcagent\llm_profiles.py scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
~~~

还要确认：

1. 本机 `data/llm_profiles.json` 不含 `sk-`、`fc-`、`tvly-` 等密钥形态。
2. `GET /api/llm-profiles` 只显示 `key_configured`，不回显原始 key。

### 23.4 本轮实际验证

已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py mcagent\retrieval_planner.py mcagent\llm_profiles.py scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
~~~

已清理本机 `data/llm_profiles.json`，并验证不含 `sk-`、`fc-`、`tvly-`、GitHub token 形态。重启 `http://127.0.0.1:8765` 后，`GET /api/llm-profiles` 返回：

1. `ollama-default`：`key_configured=false`
2. `deepseek-template`：`key_configured=false`

当前 MCagent 与 CrawlerAgent 都默认分配到 `ollama-default`。如果需要 DeepSeek，由用户在 `/settings.html` 中填入 Key 并保存。

## 24. 尊重 MCagent 的“无需工具”判断（2026-05-20）

本轮开始前已重新阅读本文档。用户发现：输入“你好”时，MCagent 已经判断“简单问候，无需其他工具”，并且下一步确认也否决了 `local_rag_search`，但执行层仍继续进入 RAG 检索。这违反了“LLM 主导、工具辅助”的原则。

### 24.1 原则

1. MCagent 不是只能走“本地检索、状态、委托 Crawler”三条路。它也可以在 LLM 判断无需工具时直接自然回复。
2. 工具确认步骤如果返回 `proceed=false`，执行层必须尊重该判断，不能继续强行执行原工具。
3. 这不是给某个测试语句写特例；修复对象是通用执行链路：任何无需工具的问题都应跳过 RAG。
4. `direct_answer` 只表示“本轮无需外部工具”，最终回复仍由 LLM 生成。

### 24.2 代码变更

1. MCagent 工具选择器新增 `direct_answer` 路径，用于问候、闲聊、系统能力说明、解释当前行为等无需工具的问题。
2. `_chat_impl()` 在路由阶段识别 `direct_answer`，直接进入 LLM 回复，不创建 Retriever，不触发 local RAG。
3. RAG 执行前的确认步骤如果返回 `proceed=false` 且建议 `answer/direct_answer/final_answer_llm`，会直接进入 LLM 回复，不再继续检索。
4. 前端进度文案增加 direct 模式提示，避免显示“正在查本地资料库”。

### 24.3 验证

已执行并通过：

~~~powershell
python -m py_compile mcagent\web_server.py scripts\public_readiness_check.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
~~~

补充了一个执行链路级验证：模拟 MCagent 选择 `answer`，但在 `local_rag_search` 前确认 `proceed=false/suggested_tool=answer`；测试确认没有调用 Retriever，直接返回 LLM 回复。

## 25. 对标 Hermes / OpenClaw / Claude Code 后的 Agent Runtime 改造方向（2026-05-20）

本轮开始前已重新阅读本文档。用户要求继续查看 `D:\magic` 下 Hermes、OpenClaw、Claude Code 的源码，分析它们的 Agent 思考逻辑与当前 MCagent / CrawlerAgent 的真实差距，并把结论写入开发文档后开始改进。

### 25.1 源码观察结论

1. 本地 `claude-code-main` 不是 Claude Code 闭源运行时本体，而是公开插件、commands、hooks、skills 示例；可参考其工程组织方式，不能把它当完整 Agent loop 源码。
2. Claude Code 插件示例强调：
   - command 明确声明允许工具、角色假设和执行步骤；
   - 复杂任务会拆成多个子 Agent 并行评审，再用验证子 Agent 过滤假阳性；
   - hook 支持 `PreToolUse`、`PostToolUse`、`Stop`、`SessionStart`，用于安全检查、测试 enforcement、上下文加载和日志审计。
3. Hermes 有更完整的 Agent runtime：
   - `run_conversation()` 是真正的工具循环：模型输出、解析 `tool_calls`、校验、执行、把 tool result 加回 messages，再继续让模型判断下一步；
   - 工具由中央 registry 声明 schema、handler、toolset、availability check；
   - 有 tool-loop guardrail，能识别重复失败、无进展、幂等工具反复调用等；
   - 有 context compression、memory provider、reasoning/provider metadata 归一化。
4. OpenClaw 更偏产品化的本地个人助理：
   - local-first gateway 负责多渠道、多 Agent routing、工具和事件；
   - 把 channel、provider、memory、browser、canvas 等能力做成插件/SDK；
   - 强调安全默认值、sandbox、DM pairing、provider profile、active memory。

### 25.2 当前项目真实差距

1. MCagent / CrawlerAgent 已经不是纯脚本，但仍是“LLM 辅助的固定流水线”，不是完整“LLM 原生工具循环”。
2. `_chat_impl()` 仍承载大量分支：direct / status / delegate / RAG / evidence / final answer；模型只是在几个阶段提供 JSON 决策，执行权仍被后端固定流程主导。
3. CrawlerAgent 已有 LLM planning 和 reflection，但执行器仍按任务队列调用固定脚本；模型不能像 Codex/Hermes 那样在统一 loop 中自由连续调用 browser/search/read/save/reflect。
4. 工具能力散落在 prompt、source alias、脚本名、UI 文案和执行器分支里，缺少统一 `ToolSpec`，导致模型有时并不知道工具边界、输入输出、失败含义。
5. 交接仍偏 job/handoff 字符串，缺少标准 `HandoffContract`：调用者、来源 Agent、目标 Agent、用户原话、任务目标、交付对象、已知上下文、验收标准、失败汇报格式。
6. 记忆仍偏 JSONL 事件摘要，没有形成模型可依赖的长期工作记忆、偏好记忆、任务状态记忆和压缩策略。
7. 失败恢复仍偏经验规则，例如 empty/off-topic/duplicate/replan；应逐步改成结构化失败分类，并把失败观察反馈给 Agent LLM 重新决策。
8. UI trace 已有过程，但仍不是完整“Agent 时间线”：用户看不到清晰的计划、当前假设、工具调用、结果判断、下一步理由和验收状态。

### 25.3 新原则

1. 所有 Agent 都必须以 LLM 为主，工具函数为辅。工具只负责客观观察和执行，不替 LLM 做最终主观判断。
2. 不再为单句测试语句写硬性特例；每次修复都要抽象到通用运行时、工具协议、记忆协议或测试场景。
3. MCagent 与 CrawlerAgent 是两个真实 Agent；`retriever_only` 是模式，不是第三个 Agent。
4. Agent 行动循环的目标形态：
   - `observe`
   - `deliberate`
   - `choose_action`
   - `preflight`
   - `execute_tool`
   - `observe_result`
   - `reflect`
   - `continue_or_finish`
5. CrawlerAgent 必须能服务两类对象：
   - 用户直接委托的数据采集；
   - MCagent/RAG 委托的可入库、可检索、可引用资料采集。
6. 浏览器是 CrawlerAgent 的一等工具，不是最后兜底；当 API 额度、JS 页面、表格、图片、下载页、中文页面抓取不稳定时，应允许 CrawlerAgent 主动选择浏览器路径。

### 25.4 本轮第一批代码改造

1. 新增 `mcagent/agent_runtime.py`，集中定义：
   - `ToolSpec`
   - `AgentRole`
   - `AgentAction`
   - `HandoffContract`
   - `LLM_OWNERSHIP_PRINCIPLES`
   - MCagent route tool catalog
   - CrawlerAgent route tool catalog
   - CrawlerAgent collection tool catalog
2. `web_server.py` 的工具选择 prompt 改为读取统一 Agent Runtime 工具目录，减少散落硬编码。
3. `web_server.py` 的下一步确认 prompt 改为读取统一 Agent Runtime 工具目录，让“确认下一步”从同一份工具能力描述出发。
4. `_fallback_delegate_handoff_brief()` 改为使用 `HandoffContract` 生成通用交接摘要，包含调用关系、用户原话、任务目标、交付对象、上下文、验收标准和失败汇报要求。
5. `crawler_llm_planner.py` 的 CrawlerAgent 规划 prompt 引入 collection tool catalog，让 Crawler 的规划 LLM 明确知道浏览器、包体下载、包体内部解析、MC百科、Modrinth、Tavily、Firecrawl、Jina 等工具的边界。
6. `tests/smoke_test.py` 增加 Agent Runtime 基础断言，保证：
   - MCagent 工具目录包含 direct answer、RAG、委托和状态；
   - CrawlerAgent route 工具目录包含 direct answer、委托和状态；
   - Crawler collection 工具目录包含 browser_collect；
   - handoff contract 能保留用户原话和任务目标。
7. `public_readiness_check.py` 把 `mcagent/agent_runtime.py` 纳入公开必备文件。

### 25.5 下一阶段计划

1. 把 `_chat_impl()` 中的分支继续收敛为 `AgentRuntime.run_turn()`，让 MCagent 真正以统一 loop 执行。
2. 把 Crawler 的 `_run_crawler_job()` 从“任务队列 + 反思”升级为“CrawlerRuntime loop”，每个工具结果都作为 observation 回灌给 CrawlerAgent。
3. 建立 `ToolResult` 结构化结果：`ok/empty/off_topic/duplicate/auth_required/quota_limited/captcha/login_required/network_error/parse_error`。
4. 建立 scenario tests：
   - 问候必须 direct answer，不触发 RAG；
   - “状态”必须走 status；
   - “本地有什么、缺什么、让 Crawler 去找”必须 planned workflow；
   - Crawler 直接用户委托不能伪装成 MCagent 派单；
   - Crawler 遇到空结果/配额/登录限制必须说明失败原因并换策略；
   - RAG 证据不足时不能由工具伪造最终答案。

### 25.6 验证要求

完成本轮后必须执行：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
~~~

## 26. ToolObservation：工具结果结构化回灌（2026-05-20）

本轮开始前已重新阅读本文档。用户反复强调：工具不能替代 Agent LLM 做最终判断，但工具必须把客观结果讲清楚，尤其是 Crawler 失败时要能说清楚到底是空结果、跑偏、登录、验证码、额度、网络、解析还是超时。

### 26.1 改造目标

1. 不再让 CrawlerAgent 面对零散的 `empty_result/off_topic_result/returncode` 自己猜含义。
2. 每次工具执行后生成统一的 `ToolObservation`，作为客观观察回灌给 CrawlerAgent。
3. `ToolObservation` 只描述工具结果，不生成用户最终答案，不替代 LLM 做主观选择。
4. 失败类型必须通用，不针对“落幕曲”“乌托邦”或任何测试句写特例。

### 26.2 新增结构

`mcagent/agent_runtime.py` 新增：

- `TOOL_RESULT_STATUSES`
- `ToolObservation`
- `classify_crawler_tool_result(result)`

当前状态分类包括：

- `ok`
- `empty`
- `off_topic`
- `duplicate_reused`
- `auth_required`
- `quota_limited`
- `captcha_required`
- `login_required`
- `network_error`
- `timeout`
- `parse_error`
- `execution_error`
- `uncertain`
- `blocked`
- `stopped`

每个 observation 包含：

- `tool_name`
- `status`
- `summary`
- `detail`
- `retryable`
- `suggested_next`

### 26.3 接入位置

1. `_crawler_bad_result()` 改为读取 `ToolObservation.bad`，不再散落判断多个布尔字段。
2. `_crawler_failure_summary()` 给 Crawler 反思阶段提供 `observation_status/summary/retryable/suggested_next`。
3. `_crawler_result_summary()` 聚合 `observation_statuses`，让 UI 和状态摘要能显示“为什么失败”。
4. `_run_crawler_job()` 每次工具执行后把 observation 写入 `result["observation"]`。
5. `crawler_llm_planner._compact_result_for_reflection()` 把 observation 传给 CrawlerAgent LLM，用于下一步反思。

### 26.4 设计边界

`ToolObservation` 不允许写用户最终回答，也不允许判断“这个问题该怎么回答”。它只能说：

- 工具执行发生了什么；
- 结果是否客观可用；
- 是否可重试；
- 如果要继续，下一步可以考虑什么类型的路径。

真正是否继续、换源、用浏览器、下载包体、停止并汇报，仍交给 CrawlerAgent LLM 在反思循环里判断。

### 26.5 验证

本轮新增 smoke 断言覆盖：

- timeout 分类；
- provider quota/rate limit 分类；
- empty result 分类；
- records > 0 的 ok 分类。

后续继续做：

1. 把 MCagent 的 `_chat_impl()` 继续拆向统一 `AgentRuntime.run_turn()`。
2. 把 Crawler 的任务队列升级成更完整的 observe/reflect/action loop。
3. 前端把 observation status 展示成直观中文状态，而不是只显示 raw JSON。

### 26.6 前端进度可读性补充

继续按本文档执行后，已把 observation 接到前端：

1. `job.readable` 现在包含 `observation_statuses` 和 `latest_observation`。
2. 会话里的 Crawler 任务卡、右侧当前任务概览、后台任务列表都会显示最近工具结果。
3. 前端新增状态标签，把 `empty/off_topic/quota_limited/login_required/timeout` 等状态翻译成可读中文。
4. 这些标签仍然只展示客观工具观察，不替 Agent LLM 做最终判断。

## 27. 每轮优化必须配套测试方案（2026-05-20）

本轮开始前已重新阅读本文档。用户要求：不要只顾优化，每次优化都要制定完备测试方案并执行。这个要求升级为项目流程，不再只靠口头承诺。

### 27.1 固定测试方案模板

每轮优化开始前，必须写清：

1. **目标行为**：本轮希望改变或保护的 Agent 行为是什么。
2. **风险点**：可能破坏哪些链路，例如路由、RAG、委托、Crawler 规划、SSE、前端显示、密钥安全。
3. **离线测试**：不依赖外网和真实 LLM 的确定性测试，必须能进 CI。
4. **集成测试**：需要本地服务或 SSE 的测试，能本地跑，必要时用环境变量控制长耗时。
5. **人工验收点**：UI、交互、长任务可读性这类自动测试难覆盖的点。
6. **通过标准**：不是“跑了就算”，而是明确断言。

### 27.2 本轮测试计划

目标行为：

- 工具结果结构化 observation 必须稳定；
- Crawler 失败原因必须能被 Agent 和 UI 读懂；
- handoff 不丢用户原话、任务目标和交付对象；
- Agent 工具目录必须暴露直接回答、RAG、状态、委托、浏览器和包体解析能力。

风险点：

- observation 分类误判，导致 Crawler 反思收到错误失败类型；
- UI 显示状态但后端没有提供字段；
- handoff 又退化成只传一句短目标；
- CI 没覆盖 Agent runtime 新文件。

离线测试：

- 新增 `tests/agent_runtime_scenarios.py`；
- 覆盖 `ok/empty/off_topic/uncertain/duplicate_reused/blocked/stopped/timeout/quota_limited/captcha_required/login_required/auth_required/network_error/parse_error/execution_error`；
- 覆盖 `HandoffContract` 保留原始请求、目标、交付对象、验收标准；
- 覆盖工具目录包含 MCagent 和 CrawlerAgent 的关键工具；
- 覆盖 `_job_readable_summary()` 能输出 observation 统计和最近 observation。

集成测试：

- CI 中继续运行 `tests/smoke_test.py`；
- `scripts/smoke_agent_flows.py` 保留为本地 live SSE 场景测试，长耗时矩阵用 `MCAGENT_SMOKE_FULL=1` 控制。

人工验收点：

- 打开 `http://127.0.0.1:8765/`；
- 启动或查看一个 Crawler 任务；
- 确认会话任务卡、右侧当前任务、后台任务列表能显示最近工具结果；
- 确认展开面板不会因为刷新自动收起。

通过标准：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

### 27.3 CI 更新

1. `.github/workflows/ci.yml` 已把 `mcagent/agent_runtime.py` 和 `tests/agent_runtime_scenarios.py` 加入语法检查。
2. CI 的 Smoke test 阶段会同时运行：
   - `python tests\smoke_test.py`
   - `python tests\agent_runtime_scenarios.py`
3. `public_readiness_check.py` 已把 `tests/agent_runtime_scenarios.py` 列为公开必备文件。

## 28. AgentLoopEvent：统一过程事件格式（2026-05-20）

本轮开始前已重新阅读本文档，并先制定测试方案。

### 28.1 本轮测试方案

目标行为：

- 让 MCagent / CrawlerAgent 的过程事件逐步从散落 dict 收敛到统一 `AgentLoopEvent`；
- 保持 SSE 和前端已依赖的 `{time, stage, status, detail}` 字段不破坏；
- 为后续 `AgentRuntime.run_turn()` 做铺垫。

风险点：

- trace 字段名变化导致前端过程详情不显示；
- 时间戳缺失或不是数字；
- 新结构又变成只在某个测试语句上生效的局部补丁。

离线测试：

- `tests/agent_runtime_scenarios.py` 增加 `AgentLoopEvent` 断言：
  - 能生成旧 trace 兼容 dict；
  - `stage/status/detail/time` 字段完整；
  - `time` 为正数。

集成测试：

- 继续运行 smoke、公开检查、编码检查和前端语法检查。

通过标准：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

## 29. AgentToolDecision：统一工具选择结果（2026-05-20）

本轮开始前已重新阅读本文档，并先制定测试方案。

### 29.1 本轮测试方案

目标行为：

- 把 `_agent_tool_decision()` 里的工具别名、fallback、action_plan 归一化迁移到 `agent_runtime.py`；
- 避免 `validate_tool_name()` 对内部 route `answer` 的不稳定处理；
- 为后续完整 `AgentRuntime.run_turn()` 减少 web_server 中的路由散落逻辑。

风险点：

- `local_rag_search` 被误归成 `direct_answer`，导致该 RAG 的问题不查库；
- CrawlerAgent 的普通说明被误判成采集任务，或采集任务被误判成闲聊；
- planned workflow 的 action_plan 丢失。

离线测试：

- `tests/agent_runtime_scenarios.py` 增加 `normalize_agent_tool_decision()` 断言：
  - `local_rag_search` 归一成内部 RAG route `answer`；
  - CrawlerAgent 的 `answer` 归一成 `direct_answer`；
  - 未知工具在 MCagent 下回退为 `answer`；
  - `answer_then_crawler` 归一成 `planned_workflow`，并保留步骤。

集成测试：

- 继续运行 py_compile、编码检查、公开检查、smoke、scenario、前端 syntax。

通过标准：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

### 29.2 代码改造

1. 新增 `AgentToolDecision` 和 `normalize_agent_tool_decision()`。
2. `_agent_tool_decision()` 不再手写别名表和 action_plan 清洗，改用 runtime 层统一归一化。
3. 这仍然不是最终答案决策；它只把 LLM 的工具选择结果规范化，执行和最终回答仍在后续 Agent loop 中处理。

## 30. 修复“证据已足够但答案缺口说明触发 Crawler”（2026-05-20）

本轮开始前已重新阅读本文档，并先制定测试方案。

### 30.1 现象

用户问“介绍一下乌托邦整合包”时，本地 RAG 已经找到多条相关资料，`EvidenceSelector` 也给出 `verdict=ok`，模型开始生成最终回答。但最终回答如果提到“本地资料还缺少某些细节”，旧逻辑会把它误判成“无法回答”，自动启动 Crawler。

这违反两个原则：

1. 工具不能替代 LLM 做最终判断；
2. 证据已足够时，普通资料缺口说明不等于需要自动补库。

### 30.2 本轮测试方案

目标行为：

- 如果 `evidence_report.verdict == "ok"`，最终回答里的普通缺口说明不能自动触发 Crawler；
- 如果没有合格证据且答案明确“本地资料库未找到可靠答案”，仍允许按原有缺资料流程补库；
- 不针对“乌托邦”写特例。

风险点：

- 过度禁止补库，导致真正没有资料时不再触发 Crawler；
- 只修某个测试句，其他整合包/模组介绍仍误触发；
- final answer 阶段再次被工具层覆盖。

离线测试：

- `tests/agent_runtime_scenarios.py` 新增：
  - evidence ok + 答案含“缺少完整任务线/模组列表” => 不自动 delegate；
  - 没有 evidence_report + “本地资料库未找到可靠答案” => 仍可 delegate。

集成测试：

- 继续运行 py_compile、前端 syntax、编码检查、公开检查、smoke、scenario。

通过标准：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

### 30.3 代码改造

1. 新增 `_answer_requires_auto_delegate(answer, evidence_report)`。
2. 当证据报告为 `ok` 时，不再因为答案里出现“缺少/未找到/不完整”等普通 caveat 自动启动 Crawler。
3. 保留无证据场景下的缺资料补库行为。

> 2026-05-20 追加修正：第 30 章的“保留无证据场景下自动补库”和 `_answer_requires_auto_delegate()` 仍然不符合用户最新确认的 Agent 原则，已废弃。后续以第 31 章为准。

## 31. Crawler 委托必须来自 Agent 明确工具选择（2026-05-20）

本轮开始前已重新阅读本文档，并对照本地三个成熟 Agent 项目源码：

- Hermes Agent：`run_agent.py` 以模型返回的 `tool_calls` 为唯一工具执行入口，运行时执行工具后把 `tool` 消息回填给模型；并有 pre/post tool hooks、guardrails、并发工具执行和上下文压缩。
- OpenClaw：运行时把模型流拆成 text/thinking/toolCall/toolResult 等事件，工具执行有 `toolCallId` 生命周期，UI 只展示过程，不替模型决定下一步。
- Claude Code 插件体系：PreToolUse/PostToolUse/Stop hooks 可以拦截、修改、补充上下文，但不会在模型回答后靠文本扫描擅自启动新工具。

### 31.1 新原则

1. Crawler 任务只能由 Agent 的工具选择结果触发：
   - `tool=delegate_crawler`
   - 或 `tool=planned_workflow` 且 action plan 中明确包含 `delegate_crawler`
2. RAG 无结果、证据不足、最终回答提到资料缺口时，后端只能把这些事实返回给 Agent/用户，不能自动创建 Crawler job。
3. 工具层可以做：
   - 检索、排序、证据筛选；
   - 运行 Crawler；
   - 记录 observation；
   - 阻止危险动作；
   - 把工具结果回填给模型。
4. 工具层不可以做：
   - 根据最终回答里的关键词自动派单；
   - 用本地抽取文本代替 LLM 最终回答；
   - 用后台规则替 Agent 判断用户意图。

### 31.2 本轮测试方案

目标行为：

- 删除答案后自动委托 Crawler 的 helper 和调用点；
- 无检索结果时返回“证据不足且未自动委托”，不创建 job；
- 证据筛选失败时，只有 planned workflow 明确委托才创建 job；
- planned workflow 明确委托仍保持可用。

风险点：

- 误删显式委托路径，导致“让 Crawler 去找”不生效；
- 无证据路径漏掉 return，继续往后执行；
- 文档继续误导后续优化；
- 历史乱码再次污染源码。

离线测试：

- `tests/agent_runtime_scenarios.py` 增加源码级原则守卫：
  - `web_server.py` 不允许出现 `_answer_requires_auto_delegate`；
  - 不允许出现 `_answer_indicates_missing_data`；
  - 不允许出现 `answer_marked_missing`；
  - 必须保留 `planned_delegate` 分支；
  - 必须有 `delegated=False` 的证据不足路径。

集成测试：

- 正常 RAG 问答：`介绍一下乌托邦整合包` 不应启动 Crawler；
- 无证据问题：不应启动 Crawler，只说明本地证据不足；
- 显式委托：`让Crawler去收集某主题资料` 应启动 Crawler；
- UI 中 Crawler 任务只来自明确委托，而不是最终回答文字扫描。

通过标准：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

## 32. 每轮优化必须进入测试闭环（2026-05-21）

本轮开始前已重新阅读本文档。用户再次强调：不能只改代码，也不能只跑单一 happy path。每次修改或优化之后，都必须制定完整、多样化的测试方案，执行测试，根据暴露的问题继续修正，再更新文档与测试。

### 32.1 流程原则

1. 修改前先读本文档，确认本轮不违反既有 Agent 原则。
2. 每轮改动必须写清楚：
   - 目标行为；
   - 风险点；
   - 离线单元/源码守卫测试；
   - 集成或接口测试；
   - 前端交互或人工验证点；
   - 失败后的修正策略。
3. 测试失败时不能把失败解释掉，必须回到代码/提示词/文档继续修。
4. 文档不是事后总结，而是下一轮行动约束；任何新原则必须追加到本文档。
5. Agent 行为相关改动尤其要覆盖：
   - 普通问候/闲聊；
   - RAG 问答；
   - 多问题组合；
   - 显式委托 Crawler；
   - 旧上下文与新目标冲突；
   - 模型失败、工具失败、空结果、跑偏结果。

### 32.2 本轮问题

显式委托 Crawler 时，任务本身已经有明确采集目标，但 Crawler 规划器可能优先读取旧会话里的 `current_topic`。例如上一轮聊“乌托邦”，下一轮明确委托收集另一个主题时，旧主题不应覆盖新任务目标。

这不是关键词特例问题，而是 Agent handoff 的上下文优先级问题：

- `collection_target`、`task_goal`、`authoritative_task_goal` 是本轮任务目标；
- `current_topic`、`topics` 是历史背景；
- CrawlerAgent 可以参考历史背景，但不能让历史背景替代本轮目标。

### 32.3 本轮改造

1. Crawler 规划器 `_session_target_hint()` 优先读取 `authoritative_task_goal`、`task_goal`、`collection_target`，最后才读取 `current_topic`。
2. Crawler 规划 prompt 明确说明：本轮 handoff/task goal 是权威采集目标，旧 `current_topic/topics` 只能作为背景记忆。
3. 后台 job 等待规划时显示的主题也按同一优先级选择，避免 UI 上继续显示旧主题。
4. MCagent 委托 Crawler 时，将本轮采集目标写入 `collection_target`、`task_goal`、`authoritative_task_goal`，并把 Crawler 规划用的 `current_topic` 同步为本轮目标。

### 32.4 本轮测试方案

目标行为：

- 显式委托 Crawler 时，新采集目标必须覆盖旧会话主题；
- Crawler fallback 规划、LLM 规划提示、后台 job 主题显示使用同一目标优先级；
- 不新增任何针对“乌托邦”“落幕曲”或某句测试语的硬编码；
- 第 31 章的原则仍成立：Crawler 只能由 Agent 明确工具选择触发。

风险点：

- 过度覆盖 `current_topic`，让 Crawler 失去历史背景；
- fallback 与 LLM planner 行为不一致；
- 显式委托路径修好了，但 planned workflow 路径仍被旧主题污染；
- 文档或源码出现编码乱码。

离线测试：

- `tests/agent_runtime_scenarios.py` 新增 `test_crawler_handoff_target_overrides_old_session_topic()`：
  - 构造旧 `current_topic=介绍一下乌托邦整合包`；
  - 构造新 `collection_target/task_goal=XYZABC 整合包资料`；
  - 验证 Crawler fallback plan 的 topic 和 queries 都包含新目标，不再以旧主题开头。

集成测试：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

人工验证建议：

- 先问一个主题介绍，再显式委托 Crawler 采集另一个虚构主题，确认 UI 和 job 摘要显示的是新主题；
- 再问“你好”，确认不会触发 RAG/Crawler；
- 再问正常 RAG 问题，确认不会因为回答中提到资料缺口而自动补库；
- 再显式说“让 Crawler 去收集……”，确认 Crawler 才启动。

## 33. 修正旧文档与回答提示中的自动补库残留（2026-05-21）

本轮开始前已重新阅读本文档。阅读时发现前文旧章节仍保留“证据不足就交给 Crawler”的表述，这会和第 31 章“Crawler 委托必须来自 Agent 明确工具选择”冲突，也会误导后续开发。

### 33.1 本轮问题

旧文档和 `_build_answer_prompt()` 中存在两类残留：

1. 文档前半段仍保留“证据不足即可自动交给 Crawler”的旧含义。
2. 最终回答 prompt 的工具说明仍暗示“资料不足即可使用委托工具”。

这些句子不是代码里的自动 job 创建逻辑，但会给 Agent/开发者错误暗示：最终回答阶段也可以靠资料不足自动补库。

### 33.2 修正原则

- 资料不足时，最终回答可以说明缺口、建议下一步；
- 后端不能扫描最终回答来启动 Crawler；
- 最终回答 prompt 不能告诉模型“资料不足时应使用工具”，因为该阶段已经不是工具选择阶段；
- Crawler 只能由工具选择/planned workflow 明确委托启动。

### 33.3 本轮测试方案

目标行为：

- 文档前半段与第 31 章保持一致；
- 回答生成 prompt 不再暗示“本地证据不足就使用 delegate_crawler”；
- 现有显式委托、planned workflow 委托仍可用；
- 第 32 章的测试闭环要求继续执行。

风险点：

- 删除旧表述后，Agent 不知道可以建议用户补库；
- 误删显式委托能力说明；
- 文档里仍残留互相矛盾的旧句子；
- 编码或语法检查回退。

离线测试：

- `tests/agent_runtime_scenarios.py` 增加源码/文档守卫：
  - `web_server.py` 不允许保留回答阶段“资料不足就使用委托工具”的旧句；
  - 开发文档不允许保留“证据不足就自动交给 Crawler”的旧句；
  - 开发文档不允许保留“最终回答判断不足后自动移交 Crawler”的旧句。

集成测试：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

## 34. 工具选择失败不能由后端代替 Agent 选路（2026-05-21）

本轮开始前已重新阅读本文档。检查 `_agent_tool_decision()` 时发现：当工具选择 LLM 调用失败时，旧逻辑会把 MCagent fallback 到 `answer`，把 CrawlerAgent fallback 到 `delegate_crawler`。这会造成两个问题：

1. MCagent 没有真正完成工具选择，却可能继续 RAG；
2. CrawlerAgent 没有真正判断任务，却可能直接启动采集 job。

这违反“Agent 必须由 LLM 主导，工具不能替 Agent 做主观判断”的原则。

### 34.1 修正原则

- 工具选择 LLM 失败时，后端不能猜测 answer/RAG/Crawler；
- 失败应显式进入 `router_error`；
- `router_error` 只向用户说明“工具选择模型失败”，不执行检索、不创建 Crawler job；
- `retriever_only/no_llm` 模式仍可按模式定义走本地检索，因为那是用户显式选择的模式，不是 Agent 失败后的猜测。

### 34.2 本轮改造

1. `_agent_tool_decision()` 的异常分支不再设置 `fallback_tool = answer/delegate_crawler`。
2. 异常分支返回 `tool=router_error` 和错误信息。
3. `_chat_impl()` 增加 `router_error` route，直接返回失败说明，并在 trace 中标注 `delegated=false`。
4. 场景测试增加源码守卫，防止以后把 `fallback_tool = "delegate_crawler"` 或 `fallback_tool = "answer"` 加回来。

### 34.3 本轮测试方案

目标行为：

- 工具选择 LLM 异常时，不检索、不派单；
- 用户能看到明确失败原因；
- CrawlerAgent 路由失败时不会创建 job；
- 正常显式委托路径不受影响；
- 第 31、32、33 章的原则继续成立。

风险点：

- 正常工具选择结果被误判为 `router_error`；
- `router_error` 没有被 `_chat_impl()` 处理，落入默认 RAG；
- 仅检索模式被误伤；
- 文档或测试守卫误伤合法文字。

离线测试：

- `tests/agent_runtime_scenarios.py` 增加源码守卫：
  - 不允许出现 `fallback_tool = "delegate_crawler"`；
  - 不允许出现 `fallback_tool = "answer"`；
  - 必须存在 `tool=router_error` 和 `_chat_impl()` 的 `route_intent == "router_error"` 分支。

集成测试：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

## 35. 删除未使用的关键词路由死代码（2026-05-21）

本轮开始前已重新阅读本文档。继续检查路由链路时发现，`web_server.py` 中仍保留三段旧关键词路由函数：

- `_is_crawler_status_request()`
- `_is_crawler_start_request()`
- `_mcagent_route_intent()`

这些函数当前已经没有调用点，但它们通过“状态、进度、补库、采集”等词去推断路由，属于早期规则路由思路。即使是死代码，保留它们也会增加后续误接回来的风险。

### 35.1 修正原则

- 未使用的关键词路由代码应删除，而不是留作“备用”；
- 状态、委托、RAG、直接回答仍由 Agent 工具选择 LLM 判断；
- `retriever_only/no_llm` 等显式模式可以保留模式逻辑，但不能复活关键词路由；
- 测试中增加源码守卫，防止旧函数回流。

### 35.2 本轮测试方案

目标行为：

- 删除旧关键词路由死代码；
- 不影响现有 `_agent_tool_decision()` 和工具目录；
- 不影响显式委托、状态、RAG、direct answer 的正常路径；
- 文档继续保持 UTF-8 且章节顺序正确。

风险点：

- 误删仍被使用的函数导致运行时 NameError；
- 测试只查删除，未覆盖语法；
- 后续有人重新加入同名关键词路由；
- 文档插入位置错误。

离线测试：

- `tests/agent_runtime_scenarios.py` 增加源码守卫：
  - 不允许出现 `def _is_crawler_status_request`；
  - 不允许出现 `def _is_crawler_start_request`；
  - 不允许出现 `def _mcagent_route_intent`。

集成测试：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

## 36. README 面向公开使用者，公开标准留在内部检查（2026-05-21）

本轮开始前已重新阅读本文档。用户明确要求：以公开仓库为目标继续优化；如果仓库公开，README 中不应写“GitHub 公开标准”这类内部验收清单。

### 36.1 本轮问题

`README.md` 仍包含两类不适合公开后展示的内容：

1. 开头说明仓库准备以 Private 维护；
2. 末尾写出“GitHub 公开标准”完整清单。

同时 `scripts/public_readiness_check.py` 还把“GitHub 公开标准”作为 README 必须包含的短语。这会强迫 README 保留内部 checklist，和公开使用文档的定位冲突。

### 36.2 修正原则

- README 面向使用者：说明项目、安装、配置、启动、导入、测试和边界；
- 公开验收标准留在开发文档、CI 和 `public_readiness_check.py` 中，不放在 README；
- 检查脚本应验证 README 有安装/启动/测试关键步骤，同时禁止内部公开清单短语出现在 README；
- `.gitignore` 必须覆盖 `.env`、真实 `config.json`、LLM profile、数据库、向量索引、爬虫导出、浏览器登录态和整合包压缩包。

### 36.3 本轮改造

1. README 删除 Private 维护说明，改成“仓库只保存可复现源码、配置样例、测试和文档”。
2. README 的“公开前检查”改为“本地质量检查”。
3. README 删除“GitHub 公开标准”整段。
4. `.gitignore` 增加 `config.json`、`data/llm_profiles.json`、Cookie/storage state、浏览器 profile、`.auth/` 等本地敏感/登录态路径。
5. `public_readiness_check.py` 不再要求 README 包含“GitHub 公开标准”；改为要求“本地质量检查”，并禁止 README 出现内部发布清单短语。
6. `public_readiness_check.py` 增加 gitignore 验证：真实配置、LLM profile、浏览器登录态、整合包 zip 都必须被忽略。

### 36.4 本轮测试方案

目标行为：

- README 公开后可直接面向普通使用者，不出现内部公开 checklist；
- 新机器仍能通过 README 完成安装、配置、启动、导入和测试；
- `public_readiness_check.py` 能阻止 README 重新加入内部公开标准；
- `.gitignore` 对敏感配置、登录态和大包体的排除更完整；
- 现有 Agent 行为测试不受影响。

风险点：

- 删除 README 清单后公开检查误报缺少文档；
- `.gitignore` 规则误伤需要跟踪的样例文件；
- 检查脚本没有覆盖浏览器登录态；
- 文档插入位置错误或 UTF-8 回退。

离线测试：

- `python scripts\public_readiness_check.py` 验证 README required/forbidden phrases 与 gitignore 覆盖；
- `git check-ignore` 间接由公开检查覆盖；
- `python scripts\check_text_encoding.py` 确认 README、文档、脚本无乱码。

集成测试：

~~~powershell
python -m py_compile mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
~~~

## 37. FastAPI 后端第一阶段：框架化 HTTP/SSE 与上下文接口（2026-05-21）

本轮开始前已重新阅读本文档。用户指出当前两个 Agent 的测试效果不符合预期，怀疑后端基础能力不足，要求认真做 FastAPI 后端，并重点考虑上下文、Agent 链路和测试。

### 37.1 本轮定位

现有后端是 `BaseHTTPRequestHandler + ThreadingHTTPServer`，优点是依赖少，但问题也明显：

- 路由、请求体解析、响应格式和 SSE 都散在一个大类里；
- 没有 OpenAPI 文档，不方便调试接口；
- 后续要做上下文、Agent 状态、工具调用记录、任务事件流时，继续堆在旧 handler 里会更难维护；
- 前端依赖的接口已经比较多，直接重写风险大。

因此本轮不删除旧 `web.py`，而是新增并行 FastAPI 后端，先完整复用现有 Agent/RAG/Crawler 业务函数，让框架层和业务层分离。

### 37.2 本轮改造

1. 新增 `mcagent/fastapi_app.py`：
   - `create_app(config)` 构造 FastAPI app；
   - 提供 `/`、`/settings.html`、`/static/*`；
   - 镜像现有 `/api/status`、`/api/jobs`、`/api/models`、`/api/agents`、`/api/llm-profiles`；
   - 镜像 `/api/chat` 与 `/api/chat/stream`，SSE 通过队列把 `_chat_impl()` 的 trace/delta/response/done 事件转成 FastAPI `StreamingResponse`；
   - 镜像 Crawler plan、summary、job start/stop、session get/delete、ingest 等接口；
   - 新增 `/api/session/context`，返回指定 session 的 history、summary、turn_count、last_turn，给前端和测试直接观察会话上下文；
   - 新增 `/api/health`，便于健康检查与部署探活。
2. 新增 `api.py` 作为 FastAPI 后端入口：
   - `python api.py --host 127.0.0.1 --port 8765`
3. 保留旧 `web.py`：
   - 旧标准库后端仍可回退运行；
   - FastAPI 后端先作为推荐入口，不强行切断旧服务。
4. `requirements.txt` 增加 `fastapi` 与 `uvicorn[standard]`。
   - CI 中 FastAPI `TestClient` 依赖 `httpx`，因此同步把 `httpx` 写入 requirements，避免本地已有依赖而 CI 缺依赖。
5. README 改为推荐 `python api.py` 启动，并说明 `/docs` 自动接口文档；旧 `python web.py` 仍作为回退。
6. CI 与公开检查加入 FastAPI 文件和测试。

### 37.3 本轮测试方案

目标行为：

- FastAPI 后端能启动并服务同一套前端；
- 核心 API 返回结构与旧后端兼容；
- SSE 能发出 `response` 和 `done` 事件；
- session/context 基础接口可用；
- 旧后端仍可通过语法检查；
- 不改 Agent 决策逻辑，不把 FastAPI 做成新的脚本路由层。

风险点：

- FastAPI import 导致公开环境缺依赖；
- SSE 队列线程吞异常或不结束；
- 静态文件路径暴露；
- `/api/session`、`/api/jobs/start-crawler` 等 POST 接口行为和旧后端不一致；
- CI 没覆盖 FastAPI，导致公开后才坏。

离线测试：

- `tests/fastapi_backend_scenarios.py`：
  - `GET /api/health`；
  - `GET /api/agents`；
  - `GET /api/status`；
  - `POST /api/session`、`/api/session/context` 与 `/api/session/delete`；
  - `POST /api/chat/stream` 空问题，验证 SSE 形状包含 `response` 与 `done`。

集成测试：

~~~powershell
python -m py_compile api.py mcagent\fastapi_app.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py tests\fastapi_backend_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
python tests\fastapi_backend_scenarios.py
~~~

后续计划：

- 第二阶段再把上下文、Agent 工具事件、job timeline 抽成清晰的 service 层；
- 第三阶段把 FastAPI 设为唯一默认后端，旧 `web.py` 只保留一段迁移期。

## 38. FastAPI 后端第二阶段：会话状态与 SSE 事件服务化（2026-05-22）

本轮开始前已重新阅读本文档。用户允许进行“大修改”，目标是继续打磨后端，让两个 Agent 的上下文、事件流和后续工具链路不再堆在旧 `web_server.py` 的全局变量和重复 SSE 代码里。

### 38.1 本轮定位

上一轮 FastAPI 后端已经能跑，但仍然直接引用旧后端的 `SESSIONS`、`SESSIONS_LOCK` 和手写队列线程。这说明框架层已经换了，状态层还没有真正拆出来。继续在接口里直接摸全局变量，会让后续“Agent 计划、上下文、工具调用、任务时间线”越来越难观察和测试。

本轮只抽离客观状态与事件基础设施，不改变 Agent 的主观判断原则：

- Agent 是否回答、检索、查状态或委托 Crawler，仍由 LLM 工具选择链路判断；
- 新模块只负责保存会话、生成上下文快照、编码 SSE、把后台线程事件送到前端；
- 不新增任何针对测试句子的关键词路由或特例规则；
- 旧 `web.py` 与 FastAPI 共用同一个会话状态服务，避免两个后端上下文不一致。

### 38.2 本轮改造

1. 新增 `mcagent/session_state.py`：
   - `InMemorySessionStore` 管理会话历史、摘要、删除和上下文快照；
   - `SessionContext` 统一返回 `session_id`、`agent`、`history`、`summary`、`turn_count`、`last_turn`；
   - `payload_history()` 专门把前端传入的历史消息转成后端 turn；
   - `merge_limited()` 作为会话摘要去重合并工具。
2. 新增 `mcagent/event_stream.py`：
   - `StreamEvent` 负责 SSE 编码；
   - `ThreadedEventStream` 负责在线程中执行旧的同步 Agent 函数，并把 trace/response/done/error 流式送出。
3. `web_server.py`：
   - 删除直接维护 `SESSIONS`、`SESSION_SUMMARIES`、`SESSIONS_LOCK` 的方式；
   - `_append_session()`、`_session_history()`、`_session_summary()`、`_delete_session()` 改为使用 `SESSION_STORE`；
   - 旧标准库后端也新增 `/api/session/context`，与 FastAPI 的上下文接口保持一致。
4. `fastapi_app.py`：
   - 不再直接读取 `SESSIONS`；
   - `/api/session` 与 `/api/session/context` 统一走 `DEFAULT_SESSION_STORE`；
   - `/api/chat/stream` 使用 `ThreadedEventStream`，去掉重复的 SSE 队列实现。
5. FastAPI 新增 `/api/agents/{agent_id}/tools`：
   - 返回该 Agent 的 route tools、Crawler 的 collection tools 和 prompt 级工具目录；
   - 该接口只暴露环境能力，方便前端和测试观察，不负责替 Agent 选择工具。
6. README、CI、公开检查加入新后端服务模块和测试。

### 38.3 本轮测试方案

目标行为：

- FastAPI 和旧后端都能通过同一个会话状态服务读取上下文；
- `/api/session/context` 能稳定返回可观察的 history 与 summary；
- `/api/agents/{agent_id}/tools` 能稳定展示 Agent 可用工具；
- SSE 事件编码和后台线程流式输出可独立测试；
- Agent 决策链路不因服务化改造改变；
- 公开检查能要求这些后端基础模块存在，避免未来误删。

风险点：

- 从全局变量迁移到 store 后，会话历史丢失或删除失效；
- FastAPI 与旧后端使用不同 store，造成上下文不一致；
- SSE 抽象吞掉异常或漏发 `done`；
- 旧测试只覆盖 FastAPI，不覆盖服务层本身；
- 公开检查没有把新增文件列为必需文件。

离线测试：

- `tests/backend_services_scenarios.py`：
  - `InMemorySessionStore` 的 append/context/delete；
  - `payload_history()` 对前端历史的解析；
  - `merge_limited()` 的去重限制；
  - `ThreadedEventStream` 的 trace/response/done SSE 形状。

集成测试：

~~~powershell
python -m py_compile api.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
node --check frontend\static\app.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
~~~

后续计划：

- 第三阶段抽出 `AgentExecutionService`，把 `_chat_impl()` 的工具选择、确认、执行和回答组织拆成可观察步骤；
- 第四阶段抽出 `CrawlerJobTimelineService`，让后台任务状态、失败原因和当前动作在 UI 中更直观。

## 39. Agent 执行上下文第一阶段：运行态与 Trace 记录器（2026-05-22）

本轮开始前已重新阅读本文档。用户要求继续“大修改”，并特别关心两个 Agent 是否真的像有上下文、有工具、有思考链路的 Agent。本轮开始拆 `_chat_impl()`，但不一次性搬空整个大函数，先抽出稳定的执行上下文、模型解析和 trace 记录器，为后续拆工具执行器做地基。

### 39.1 本轮定位

`_chat_impl()` 当前同时负责：

- 读取 payload；
- 解析 agent、model、temperature、max_tokens；
- 记录 trace 并向 SSE emit；
- 判断空问题/编码损坏；
- 调用工具选择 LLM；
- 执行 direct answer、status、RAG、delegate crawler；
- 组织最终 response 并写会话。

这些职责混在一起，会让后续每次优化 Agent 思考链路都要碰一个超大函数。第 39 阶段先把“运行上下文”和“trace 事件记录”抽出去，让 `_chat_impl()` 以后逐步只保留编排逻辑。

### 39.2 本轮改造

1. 新增 `mcagent/agent_execution.py`：
   - `AgentTraceRecorder`：统一生成 legacy trace shape，并负责向 SSE emit `trace` / `delta`；
   - `AgentExecutionContext`：保存 config、payload、原始问题、当前问题、agent、model、temperature、max_tokens 和 trace；
   - `resolve_agent_model()`：集中处理 model_profile_id、显式 model、Agent profile assignment、默认 Ollama 的优先级；
   - `build_agent_execution_context()`：从 payload 构造本轮 Agent 运行上下文，并记录 `observe/received`。
2. `web_server.py`：
   - `_chat_impl()` 开头改用 `build_agent_execution_context()`；
   - 保留后续工具选择、检索、委托、回答逻辑不变；
   - contextualize 后同步更新 `run.question`，为后续拆执行器做准备；
   - 空问题和编码损坏分支开始使用 `run.response()` 生成带 trace 的响应。
3. 测试与公开检查：
   - 新增 `tests/agent_execution_scenarios.py`；
   - CI、README、`public_readiness_check.py` 都加入新模块和测试。

### 39.3 本轮测试方案

目标行为：

- trace 结构仍保持前端兼容：`stage/status/detail/time`；
- SSE streaming 下 trace 和 delta 仍能被 emit；
- model 解析优先级稳定：profile override > raw model > agent assignment；
- `_chat_impl()` 的现有 Agent 决策路径不因上下文抽离而变化；
- 公开检查能要求 `agent_execution.py` 和测试存在。

风险点：

- 模型解析从 `_chat_impl()` 移走后，profile assignment 结果变化；
- trace recorder 的 shape 和旧前端不兼容；
- streaming 的 delta emit 被漏掉；
- contextualized question 与 run context 不一致；
- 测试只测新模块，不测旧 chat path。

离线测试：

- `tests/agent_execution_scenarios.py`：
  - `AgentTraceRecorder` 的 trace shape 和 emit；
  - `build_agent_execution_context()` 的 payload 解析；
  - `resolve_agent_model()` 的优先级。

集成测试：

~~~powershell
python -m py_compile api.py mcagent\agent_execution.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_execution_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_execution_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
~~~

后续计划：

- 第 40 阶段抽 `AgentToolRouterService`：把工具选择 LLM、确认下一步、planned workflow 判定从 `_chat_impl()` 中拆出来；
- 第 41 阶段抽 `AgentToolExecutor`：把 direct answer、status、RAG、delegate crawler 分支拆成可测试执行器。

## 40. Agent 工具路由第一阶段：路由编排服务化（2026-05-22）

本轮开始前已重新阅读本文档。用户明确授权：每个新阶段只要能优化 Agent，就继续执行，不必再等待逐次确认。本轮继续拆 `_chat_impl()`，目标是把“工具选择、下一步确认、planned workflow 判定”从大函数中抽成可测试服务。

### 40.1 本轮定位

上一轮 `AgentExecutionContext` 解决了运行态和 trace 的基础问题，但 `_chat_impl()` 里仍然直接写着：

- 调用工具选择 LLM；
- 解析 `tool_decision`；
- 记录 `tool_selected`、`plan.created`、`plan.rag_focus`；
- 调用下一步确认 LLM；
- 根据 confirmation suggested_tool 改道；
- 判断 `planned_workflow` 与 `planned_delegate`。

这些都属于 Agent 工具路由编排，不应该散落在 chat 大函数里。本轮先抽编排服务，但保持 LLM prompt 函数仍在旧模块中，通过依赖注入调用，避免一次性大搬迁导致循环依赖。

### 40.2 本轮改造

1. 新增 `mcagent/agent_router.py`：
   - `AgentRouteDecision`：统一保存 `route_intent`、`tool_decision`、`route_confirmation`、`action_plan`、`rag_focus`、`planned_workflow`、`planned_delegate`；
   - `AgentToolRouterService`：负责调用注入的工具选择函数、下一步确认函数、planned workflow 判定函数；
   - 服务会记录现有 trace shape，但不替 LLM 做主观判断。
2. `web_server.py`：
   - `_chat_impl()` 改用 `AgentToolRouterService.route()`；
   - 后续 direct answer、status、RAG、delegate crawler 执行分支保持不变；
   - 工具选择 LLM 和确认 LLM 的 prompt 暂时仍在 `web_server.py`，下一阶段再整体搬迁。
3. 新增 `tests/agent_router_scenarios.py`：
   - 验证 route service 保留 trace；
   - 验证 confirmation 可建议改道到 `direct_answer`；
   - 验证 planned workflow 只标记 `planned_delegate`，不直接执行 Crawler。
4. CI、README、公开检查加入新模块和测试。

### 40.3 本轮测试方案

目标行为：

- 工具路由 trace 仍包含 `decide/tool_selected` 与 `decide/next_step_confirmed`；
- `rag_focus` 和 `action_plan` 仍能被传回 `_chat_impl()` 后续逻辑；
- confirmation suggested_tool 能改道，但仍只允许既有工具集合；
- planned workflow 仍先进入 answer 路径，只有后续分支按计划决定是否委托 Crawler；
- `_chat_impl()` 的行为不因路由编排服务化而改变。

风险点：

- 路由服务没有同步 action_plan/rag_focus，导致 RAG 检索失去主题；
- planned_workflow 判定变化，误触发或漏触发 Crawler；
- trace stage/status 与前端不兼容；
- 依赖注入隐藏了 LLM 调用失败路径；
- 测试只测服务，不测整体 chat path。

离线测试：

- `tests/agent_router_scenarios.py`：
  - 路由决策和确认 trace；
  - suggested_tool 改道；
  - planned workflow + delegate 标记。

集成测试：

~~~powershell
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_router.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_execution_scenarios.py tests\agent_router_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_execution_scenarios.py
python tests\agent_router_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
~~~

后续计划：

- 第 41 阶段把 `_agent_tool_decision()` 与 `_agent_confirm_next_step()` 的 prompt/LLM 调用整体搬进 `agent_router.py`，让 web_server 不再持有路由 prompt；
- 第 42 阶段抽 direct answer/status/delegate/RAG 的工具执行器。

## 41. Agent 工具路由第二阶段：路由 LLM Prompt 迁出 web_server（2026-05-22）

本轮开始前已重新阅读本文档。第 40 阶段只抽了路由编排，但工具选择和下一步确认的 prompt 仍然留在 `web_server.py`。这会让 web 层继续承担 Agent 主观决策提示词，不符合“后端框架与 Agent runtime 分离”的方向。

### 41.1 本轮定位

本轮把路由 LLM 的 prompt 与 JSON 解析整体迁入 `agent_router.py`，让 `web_server.py` 只负责把选择好的路由结果接到后续工具执行分支。注意：这不是用规则替代 LLM，而是把 LLM 工具选择器搬到正确的 Agent runtime 模块。

### 41.2 本轮改造

1. `agent_router.py` 新增：
   - `json_object_from_llm_text()`：统一解析模型返回的 JSON；
   - `LlmAgentToolRouterService`：持有工具选择 prompt、下一步确认 prompt、router_error 失败语义；
   - `decide_tool()`：由当前 Agent LLM 选择 `direct_answer/status/answer/delegate_crawler/planned_workflow/router_error`；
   - `confirm_next_step()`：由当前 Agent LLM 确认下一步工具动作。
2. `web_server.py`：
   - 删除 `_agent_tool_decision()` 与 `_agent_confirm_next_step()`；
   - `_chat_impl()` 改用 `LlmAgentToolRouterService`；
   - delegate/status/retrieve/final_answer 的二次确认也统一调用同一个 router service；
   - handoff brief 仍复用 `json_object_from_llm_text()`，避免重复 JSON 解析函数。
3. `tests/agent_router_scenarios.py`：
   - 增加 fake LLM client，验证路由 prompt 现在由 `agent_router.py` 持有；
   - 验证 fenced JSON 能解析；
   - 验证 router LLM 失败时返回 `router_error`，不会回退成 answer 或 delegate。
4. `tests/agent_runtime_scenarios.py`：
   - 源码守卫更新为同时检查 `web_server.py` 与 `agent_router.py`，确保 router_error 路径仍存在。

### 41.3 本轮测试方案

目标行为：

- web 层不再持有工具选择/下一步确认 prompt；
- router LLM 失败仍不执行任何工具；
- `router_error` 行为保持：不检索、不自动委托 Crawler；
- prompt 迁移后 direct answer/status/RAG/delegate/planned workflow 分支仍可走通；
- JSON 解析不因模型包裹 markdown fence 而失败。

风险点：

- prompt 迁移导致导入循环；
- web_server 中仍有旧路由函数残留；
- 二次确认调用没有使用同一个 router service；
- 源码守卫误判；
- fake client 测试覆盖不了真实服务启动。

离线测试：

- `tests/agent_router_scenarios.py`：
  - route trace；
  - suggested_tool；
  - planned workflow；
  - LLM prompt ownership；
  - router_error fallback。

集成测试：

~~~powershell
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_router.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_execution_scenarios.py tests\agent_router_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_execution_scenarios.py
python tests\agent_router_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
~~~

后续计划：

- 第 42 阶段抽 `AgentToolExecutor`，先拆 direct answer/status/delegate 三个执行分支；
- 第 43 阶段再拆 RAG 检索、证据筛选与 final answer executor。

## 42. Agent 工具执行第一阶段：direct/status/router_error 执行器（2026-05-22）

本轮开始前已重新阅读本文档。第 41 阶段已经把工具路由 prompt 迁出 web 层，但 `_chat_impl()` 里仍然直接执行 direct answer、status 和 router_error 分支。为了继续降低后端耦合，本轮先抽出低风险工具执行器。

### 42.1 本轮定位

这次抽离只处理“Agent 已经明确选中的工具”。它不决定要不要查状态、不决定要不要直接回答、不决定要不要委托 Crawler。工具选择仍由 `LlmAgentToolRouterService` 和当前 Agent LLM 完成。

执行器的职责是客观执行：

- router_error：把工具选择模型失败明确展示给用户，并保证不检索、不委托；
- direct_answer：调用最终回答模型或流式模型，把 delta 和 thinking trace 转发给前端；
- status：调用状态监控工具并返回结果；
- retrieval 被 Agent 二次确认取消且建议直接回答时，复用同一 direct_answer 执行路径。

### 42.2 本轮改造

1. 新增 `mcagent/agent_executor.py`：
   - `AgentToolExecutor` 封装 direct/status/router_error 的客观执行；
   - streaming direct answer 通过 `run.emit_delta` 输出 token，通过 `answer/thinking` trace 展示可观察思考进度；
   - 模型失败只显示“模型调用失败”，不把工具抽取结果伪装成最终回答。
2. `web_server.py`：
   - route_intent 为 `router_error`、`direct_answer`、`status` 时改用执行器；
   - 本地 RAG 检索被 Agent 二次确认取消且建议 direct answer 时，也走执行器，避免旧分支重复调用模型。
3. `README.md`：
   - 发现旧 README 已经乱码，本轮重写为干净 UTF-8 中文版；
   - README 只保留公开用户需要的安装、配置、启动、导入、测试与 Agent 边界，不写内部公开标准清单。
4. `public_readiness_check.py`：
   - 公开检查改为检查正常中文短语“本地质量检查”；
   - 加入 `agent_executor.py` 与对应测试。
5. CI 加入 `agent_executor` 语法检查、场景测试与 `settings.js` 前端语法检查。

### 42.3 本轮测试方案

目标行为：

- direct answer 非流式和流式都能由执行器调用；
- streaming 能发出 delta，并保留 thinking trace；
- router_error 不执行本地检索、不启动 Crawler；
- status 分支仍直接返回监控结果；
- README 与公开检查不再包含乱码短语；
- FastAPI 仍能启动，`/api/health`、`/docs`、`/api/session/context`、`/api/agents/{agent}/tools` 可用。

风险点：

- 执行器抽离后 trace shape 与前端不兼容；
- streaming direct answer 漏掉 delta；
- router_error 被误写成普通 answer 或触发工具；
- README 重写遗漏公开安装步骤；
- 公开检查仍引用旧乱码短语。

离线测试：

- `tests/agent_executor_scenarios.py`：
  - router_error 不执行工具；
  - direct answer 非流式；
  - direct answer 流式 delta + thinking trace；
  - direct answer 模型失败可见；
  - status 返回监控 payload。

集成测试：

~~~powershell
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
python tests\smoke_test.py
python tests\agent_execution_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
~~~

后续计划：

- 第 43 阶段抽 RAG 检索、证据筛选与 final answer 执行器；
- 第 44 阶段抽 Crawler 委托执行器和 job timeline 服务。

## 43. Agent 工具执行第二阶段：RAG 最终回答执行器（2026-05-22）

本轮开始前已重新阅读本文档。第 42 阶段已经抽出 direct/status/router_error 的客观执行器，本轮继续拆 RAG 后半段，把“仅检索回答包装”和“基于已筛选证据调用最终回答模型”也纳入执行器。

### 43.1 本轮定位

本轮不改变检索规划、候选召回、证据筛选和证据是否足够的判断。那些仍属于 RAG 工具链和 Agent/LLM 判断链路。

本轮只抽出已到达 `final_answer_llm` 阶段后的客观执行：

- 仅检索模式：把已生成的 context 包装成“未调用模型”的结果；
- 最终回答模式：在 Agent 已确认可进入 `final_answer_llm` 后调用 grounded answer 模型；
- streaming：继续把 delta 和 thinking trace 通过 run 上下文转发；
- 模型失败：只显示模型失败，不调用 repair 逻辑伪装成正常回答。

### 43.2 本轮改造

1. `mcagent/agent_executor.py`：
   - 增加 `grounded_answer()`；
   - 增加 `retriever_only_answer()`；
   - 失败时跳过 answer repair，确保“模型调用失败”不会被工具修饰成最终答案。
2. `web_server.py`：
   - RAG 后半段改用 `executor.grounded_answer()`；
   - `retriever_only/no_llm` 路径改用 `executor.retriever_only_answer()`；
   - `answer/generating` trace 从 web 分支移动到执行器内部，保持“先确认下一步，再执行模型调用”的顺序。
3. `tests/agent_executor_scenarios.py`：
   - 增加 grounded answer 非流式、流式、失败和仅检索测试；
   - 验证 streaming delta 与 thinking trace；
   - 验证模型失败不会被 repair。

### 43.3 本轮测试方案

目标行为：

- final_answer_llm 的模型调用仍由 LLM 生成最终回答；
- executor 不决定证据够不够，也不决定是否委托 Crawler；
- 仅检索模式明确说明未调用模型；
- streaming grounded answer 能发出 delta；
- 模型失败保持原样可见；
- 旧 direct/status/router_error 测试继续通过。

风险点：

- trace 顺序改变导致前端展示异常；
- 模型失败被 repair 函数改写；
- no_llm 路径误调用模型；
- grounded stream 漏掉 thinking trace；
- RAG 最终回答路径与 planned delegate 后续逻辑耦合过紧。

离线测试：

- `tests/agent_executor_scenarios.py`：direct/status/router_error/grounded/no_llm 全覆盖。
- 完整集成测试沿用第 42 阶段命令，并重点观察 `tests/fastapi_backend_scenarios.py` 与 `tests/smoke_test.py`。

后续计划：

- 第 44 阶段抽检索规划与候选召回服务；
- 第 45 阶段抽证据筛选服务；
- 第 46 阶段抽 Crawler 委托执行器与 job timeline 服务。

## 44. RAG 检索服务第一阶段：规划与召回执行服务化（2026-05-22）

本轮开始前已重新阅读本文档，尤其确认了“Agent 必须由 LLM 主导、工具只做客观执行”和“每次修改都要有完整测试方案”的原则。本轮没有为某个测试句添加硬编码，而是继续沿第十七、十八两个大方向拆分后端职责。

### 44.1 本轮目标

把 `web_server.py` 中混在主聊天流程里的 RAG 检索准备、检索规划调用、候选召回、raw HTML 补充和去重执行迁入独立服务。这样 MCagent 的工具选择仍由 Agent/LLM 与路由服务负责，而 RAG 服务只在“已经决定要检索”之后执行客观检索步骤。

### 44.2 代码变更

1. 新增 `mcagent/rag_service.py`：
   - `RagRetrievalPreparation`：保存 evidence question、rough_k、final_k；
   - `RagRetrievalResult`：保存 retrieval plan、实际检索问题、候选结果和初始去重结果；
   - `RagRetrievalService`：执行本地 RAG 检索、可选检索规划、raw HTML 候选补充和基础去重。
2. 修改 `mcagent/web_server.py`：
   - `_chat_impl()` 不再直接 new `Retriever`、调用 `plan_retrieval()`、拼检索问题和补 raw HTML；
   - `_chat_impl()` 只负责在 MCagent 已确认下一步是 `local_rag_search` 后调用 `RagRetrievalService`；
   - 证据是否足够、是否交给 Crawler、最终回答怎么组织仍留在后续 Agent/证据流程中，不由 RAG 服务决定。
3. 新增 `tests/rag_service_scenarios.py`：
   - 测试 `rag_focus` 优先级和自适应候选数；
   - 测试计划型检索会发出 planning/planned/searching/done trace，并用组合后的检索问题调用 retriever；
   - 测试 MCagent 候选过少时只触发 raw HTML 补充，不生成证据结论；
   - 测试非 MCagent/无计划检索不会调用 planner 或补充器。
4. 更新 `.github/workflows/ci.yml`、`README.md`、`scripts/public_readiness_check.py`，把新服务和新测试纳入公开仓库质量检查。

### 44.3 边界说明

`RagRetrievalService` 不是 Agent。它不知道用户最终要不要回答，也不知道证据够不够，更不会自动委托 Crawler。它只执行已经被 MCagent 选择的工具动作，并把可观察 trace 和候选结果交回主流程。

### 44.4 本轮测试方案与已通过结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\rag_service.py mcagent\web_server.py tests\rag_service_scenarios.py
python tests\rag_service_scenarios.py
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\rag_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\rag_service_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
~~~

随后继续执行并通过：

~~~powershell
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/docs=200，/api/session/context=200，/api/agents/mcagent_rag/tools=200
~~~

`scripts\public_readiness_check.py` 仍提示缺少 LICENSE。这不是公开 GitHub 的硬性阻断项；当前策略仍是无 LICENSE 时默认保留所有权利，等仓库所有者决定复用协议后再添加。

### 44.5 下一步

继续拆 `EvidenceWorkflowService`：把证据选择、同主题补充、modpack manifest 补充、raw HTML 二次补充等客观证据工作迁出 `web_server.py`，但保持最终“证据是否能回答”和“是否需要 Crawler”的判断由 Agent/LLM 链路控制。

## 45. EvidenceWorkflowService 第一阶段：证据筛选与上下文补充服务化（2026-05-22）

本轮继续第十七、十八两个方向。开始前再次阅读本文档，确认本阶段只抽“证据工具执行”，不把 Crawler 委托或最终回答决策放进工具服务。

### 45.1 本轮目标

把 `web_server.py` 里一长段证据处理逻辑迁出为 `EvidenceWorkflowService`。这段逻辑包括候选证据筛选、父主题优先、整合包 manifest 补充、本地 manifest 补充、项目关键词补充、raw HTML 补充、modlist 上下文补充和主题 fallback。它们都属于客观证据加工，不应混在聊天编排函数里。

### 45.2 代码变更

1. 新增 `mcagent/evidence_service.py`：
   - `EvidenceWorkflowResult`：返回 selected 和 EvidenceReport；
   - `EvidenceWorkflowService`：执行证据筛选与客观补充；
   - 服务只返回证据结果，不生成最终回答，不委托 Crawler。
2. 修改 `mcagent/web_server.py`：
   - RAG 候选召回后，调用 `EvidenceWorkflowService.select()` 获取 selected/report；
   - 证据不足后的用户说明、planned delegate、handoff brief、Crawler job 创建仍留在主 Agent 编排层。
3. 新增 `tests/evidence_service_scenarios.py`：
   - 验证 selector 调用、trace 顺序和补充函数顺序；
   - 验证 modpack manifest 可把客观证据报告升级为 ok；
   - 验证 fallback theme 可恢复稀疏选择；
   - 验证服务只返回 evidence result，不触碰 Crawler 委托或最终回答。
4. 更新 `.github/workflows/ci.yml`、`README.md`、`scripts/public_readiness_check.py`，纳入新服务和新测试。

### 45.3 边界说明

`EvidenceWorkflowService` 仍不是 Agent。它可以根据客观补充结果调整 `EvidenceReport`，但它不能决定“现在该不该通知 Crawler”，也不能组织用户最终回答。这个边界继续由 MCagent 的工具选择、planned workflow 和最终回答 LLM 控制。

### 45.4 本轮测试方案与结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\evidence_service.py mcagent\web_server.py tests\evidence_service_scenarios.py
python tests\evidence_service_scenarios.py
python tests\rag_service_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\evidence_service.py mcagent\rag_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/docs=200，/api/session/context=200，/api/agents/mcagent_rag/tools=200
~~~

公开检查继续只提示 LICENSE 缺失；这仍是已知非阻断警告。

### 45.5 下一步

继续拆 Crawler 委托执行器和 job timeline 服务，让 MCagent 与 CrawlerAgent 的交接内容、用户经 MCagent 转达、MCagent 自主补库、用户直连 Crawler 的几种关系在后端结构上更清楚。

## 46. CrawlerDelegationService 第一阶段：MCagent 到 CrawlerAgent 交接包服务化（2026-05-22）

本轮继续遵守“Agent 决策，工具执行”的边界。开始前已重新阅读本文档。本阶段只抽取委托交接包的构建逻辑，不让服务决定是否委托，也不让服务替 CrawlerAgent 规划搜索词或数据源。

### 46.1 本轮目标

之前 `web_server.py` 里有两段重复的 Crawler 委托准备逻辑：一种发生在证据不足且 planned delegate 已明确选择时，另一种发生在最终回答后仍需要同步补库时。这两段都要保留调用关系、交付对象、原始用户请求、MCagent 缺口摘要和 handoff brief。把它们抽成服务后，MCagent/CrawlerAgent 的关系更清楚，也方便后续做 job timeline。

### 46.2 代码变更

1. 新增 `mcagent/crawler_delegation_service.py`：
   - `CrawlerDelegationPlan`：保存 collection_question、requested_by、delivery_target、handoff_brief、planner_summary、delegate_payload；
   - `CrawlerDelegationService.prepare()`：在 MCagent 已经决定委托后，构建交给 CrawlerAgent 的上下文包。
2. 修改 `mcagent/web_server.py`：
   - 证据不足 planned delegate 分支改用 `CrawlerDelegationService`；
   - 最终回答后的 planned delegate 分支也改用同一个服务；
   - `_delegate_crawler_for_missing_data()` 仍负责真正创建任务，CrawlerAgent 仍自行规划采集。
3. 新增 `tests/crawler_delegation_service_scenarios.py`：
   - 验证用户经 MCagent 转达时，requested_by、handoff_from、delivery_target、原始用户请求、gap summary 都被保留；
   - 验证用户直连 Crawler 时，delivery target 可由任务目标推断；
   - 验证服务不创建 job、不调用搜索、不替 Crawler 规划关键词。
4. 更新 `.github/workflows/ci.yml`、`README.md`、`scripts/public_readiness_check.py`。

### 46.3 边界说明

`CrawlerDelegationService` 只回答“已经决定委托之后，应该把什么上下文交给 CrawlerAgent”。它不回答“是否需要委托”，也不回答“CrawlerAgent 应该搜什么”。真正的采集路径仍由 CrawlerAgent 的 LLM 在任务执行时根据 handoff brief、历史结果和工具观察循环决定。

### 46.4 本轮测试方案与结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\crawler_delegation_service.py mcagent\web_server.py tests\crawler_delegation_service_scenarios.py
python tests\crawler_delegation_service_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\evidence_service_scenarios.py
python tests\rag_service_scenarios.py
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\crawler_delegation_service.py mcagent\evidence_service.py mcagent\rag_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\crawler_delegation_service_scenarios.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/docs=200，/api/session/context=200，/api/agents/mcagent_rag/tools=200
~~~

公开检查仍只有 LICENSE 非阻断警告。

### 46.5 下一步

继续把 Crawler job timeline/status card 从 `web_server.py` 拆出来，目标是让 UI 看到的“当前动作、成功/空结果/跑偏、CrawlerAgent 判断、下一步”来自统一的任务视图服务，而不是多个地方各自拼字段。

## 47. JobReadableViewService 第一阶段：Crawler 任务视图服务化（2026-05-22）

本轮开始前已重新阅读本文档。本阶段继续拆后端结构，但不改变 Agent 决策逻辑：Crawler 任务是否存在、Crawler 下一步怎么采集，仍由 MCagent/CrawlerAgent 的 LLM 链路决定；本服务只把已有 job/result/plan/tasks 转换成人能读懂的状态视图。

### 47.1 本轮目标

此前 `_job_readable_summary()` 在 `web_server.py` 内部直接拼任务状态、观察计数、当前动作、下一步说明。前端又二次推断状态，导致 UI 有时看起来像一堆后台字段。本轮把任务可读视图抽成服务，并给前端增加直接可用的字段。

### 47.2 代码变更

1. 新增 `mcagent/job_view_service.py`：
   - `JobReadableViewService.build()` 从 job dict 构造统一 readable view；
   - 输出 `headline`、`status_label`、`progress_percent`、`progress_text`、`health_text`、`next_action` 等字段；
   - 仍保留 observation_statuses、latest_observation、agent_reflection 等原字段，保证前端兼容。
2. 修改 `mcagent/web_server.py`：
   - `_job_readable_summary()` 改为调用 `JobReadableViewService`；
   - 原 API 响应结构不变，`job.readable` 继续存在。
3. 修改 `frontend/static/app.js`：
   - Crawler 任务卡片优先使用后端给出的 `headline/status_label/progress_text/health_text`；
   - 右侧 active crawler overview 和后台任务列表也显示新的健康说明；
   - 前端不再重复猜测主要状态含义，只做展示。
4. 新增 `tests/job_view_service_scenarios.py`：
   - 验证运行中任务会显示当前动作、进度、观察状态和额度不足健康说明；
   - 验证等待规划任务用自然语言表达，而不是空字段堆叠。
5. 更新 `.github/workflows/ci.yml`、`README.md`、`scripts/public_readiness_check.py`。

### 47.3 边界说明

`JobReadableViewService` 是 UI/API 视图服务，不是 Agent。它不会决定下一步采集策略，也不会修正 CrawlerAgent 的判断。它只把 Crawler 已产生的 plan/tasks/observations 变成稳定、可解释的展示结构。

### 47.4 本轮测试方案与结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\job_view_service.py mcagent\web_server.py tests\job_view_service_scenarios.py
python tests\job_view_service_scenarios.py
python tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\crawler_delegation_service.py mcagent\evidence_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\job_view_service.py mcagent\rag_service.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\crawler_delegation_service_scenarios.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\job_view_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\crawler_delegation_service_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\evidence_service_scenarios.py
python tests\job_view_service_scenarios.py
python tests\rag_service_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\settings.js
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/api/jobs=200，/api/session/context=200，/api/agents/mcagent_rag/tools=200
~~~

公开检查仍只有 LICENSE 非阻断警告。`frontend/static/app.js` 出现 Git 的 CRLF/LF 工作区提示，不影响语法和测试；后续若需要统一换行，可单独做格式化提交。

### 47.5 下一步

继续做 Crawler job timeline 第二阶段：把 job readable view 扩展为时间线数组，让 UI 能显示“规划、执行、观察、反思、重规划、入库”的顺序事件，而不是只看当前任务快照。

## 48. Crawler Job Timeline：任务过程事件化（2026-05-22）

本轮开始前已重新阅读本文档。第 47 阶段已经把 job readable view 服务化，本轮继续在这个服务中增加 timeline，但仍然只展示事实过程，不替 CrawlerAgent 判断下一步采集策略。

### 48.1 本轮目标

让 UI 能看到 Crawler 任务从规划到执行的顺序过程，而不是只看到当前任务快照。timeline 事件包括：

- 规划：CrawlerAgent 是否已经产生计划，以及计划动作数；
- 采集动作：每个 planned task 的来源、查询、理由、工具观察状态；
- 反思：CrawlerAgent 的 agent_reflections；
- 重规划：replan_count；
- 入库：ingest_background、ingest、ingest_error。

### 48.2 代码变更

1. `mcagent/job_view_service.py`：
   - 新增 `timeline` 字段；
   - 新增 `_timeline()` 方法，将 plan/tasks/observations/reflections/ingest 状态整理为事件数组；
   - 事件保留 `type/label/status/title/text`，方便前端统一展示。
2. `frontend/static/app.js`：
   - 新增 `renderJobTimeline()`；
   - Crawler 任务详情卡片显示最近 10 条 timeline；
   - timeline 与已有 health_text、next_action 共存，不替代 Agent 的判断。
3. `frontend/static/app.css`：
   - 新增 `.job-timeline`、`.job-timeline-row`、`.job-timeline-label` 样式。
4. `tests/job_view_service_scenarios.py`：
   - 增加 timeline 断言，覆盖 plan/task/reflection/replan/ingest。

### 48.3 边界说明

timeline 是观察层。它反映 CrawlerAgent 与工具已经发生的事情，不决定任务是否成功，不决定下一步搜索什么，也不自动触发补库。它的目的只是让用户更直观看到“现在到底发生了什么”。

### 48.4 本轮测试方案与结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\job_view_service.py mcagent\web_server.py tests\job_view_service_scenarios.py
python tests\job_view_service_scenarios.py
python tests\agent_runtime_scenarios.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\crawler_delegation_service.py mcagent\evidence_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\job_view_service.py mcagent\rag_service.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\crawler_delegation_service_scenarios.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\job_view_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\crawler_delegation_service_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\evidence_service_scenarios.py
python tests\rag_service_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/api/jobs=200，/api/session/context=200，/api/agents/mcagent_rag/tools=200
~~~

公开检查仍只有 LICENSE 非阻断警告。`frontend/static/app.js` 仍有 Git 的 CRLF/LF 工作区提示；不影响测试，后续可单独统一换行。

### 48.5 下一步

继续优化 CrawlerAgent 的任务执行可观测性：把 reflection、tool observation、失败原因和 replan 输入输出进一步统一成结构化对象，方便后续让 CrawlerAgent 复盘“为什么空结果/跑偏/失败，以及下一步怎么改”。

## 49. CrawlerReflectionSnapshotService：反思输入结构化（2026-05-22）

本轮开始前已重新阅读本文档。第 48 阶段让用户能看到 timeline，本轮面向 CrawlerAgent 自身：把 reflection 阶段喂给 LLM 的最近结果、待执行任务、观察状态和压力信号整理成一个结构化 snapshot。它仍然只提供事实输入，不决定 CrawlerAgent 的下一步。

### 49.1 本轮目标

让 `reflect_crawler_progress()` 不再自己散拼 recent_results/pending_tasks/plan，而是使用统一服务生成：

- compact plan；
- recent tool results；
- pending task list；
- observation_statuses；
- retryable_recent_results；
- pressure，例如 quota_limited、poor_yield、no_pending、has_usable_evidence。

这些信息进入 CrawlerAgent 的 prompt，帮助它判断是否继续执行、换来源、重规划或结束。

### 49.2 代码变更

1. 新增 `mcagent/crawler_reflection_service.py`：
   - `CrawlerReflectionSnapshotService.build()`；
   - `compact_plan()`、`compact_pending()`、`compact_result()`；
   - `_pressure()` 给出客观压力信号。
2. 修改 `mcagent/crawler_llm_planner.py`：
   - `reflect_crawler_progress()` 改为使用 snapshot；
   - prompt 中新增 `loop_snapshot`，包含 observation_statuses、retryable_recent_results、pressure。
3. 新增 `tests/crawler_reflection_service_scenarios.py`：
   - 覆盖 quota_limited pressure；
   - 覆盖空结果/跑偏过多时的 poor_yield pressure；
   - 验证 pending task、recent result、观察计数结构稳定。
4. 更新 `.github/workflows/ci.yml`、`README.md`、`scripts/public_readiness_check.py`。

### 49.3 边界说明

`CrawlerReflectionSnapshotService` 是 CrawlerAgent 的观察整理器，不是决策器。它不会选择工具、不会新增任务、不会判断采集是否完成；这些仍由 `reflect_crawler_progress()` 调用的 CrawlerAgent LLM 根据 snapshot 做决定。

### 49.4 本轮测试方案与结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\crawler_reflection_service.py mcagent\crawler_llm_planner.py tests\crawler_reflection_service_scenarios.py
python tests\crawler_reflection_service_scenarios.py
python tests\agent_runtime_scenarios.py
python tests\job_view_service_scenarios.py
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\crawler_delegation_service.py mcagent\crawler_reflection_service.py mcagent\evidence_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\job_view_service.py mcagent\rag_service.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\crawler_delegation_service_scenarios.py tests\crawler_reflection_service_scenarios.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\job_view_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\crawler_delegation_service_scenarios.py
python tests\crawler_reflection_service_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\evidence_service_scenarios.py
python tests\rag_service_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/api/jobs=200，/api/session/context=200，/api/agents/crawler_agent/tools=200
~~~

第一次 `py_compile` 遇到 Windows `__pycache__` 写入权限抖动，清理 `mcagent\__pycache__` 后重跑通过。公开检查仍只有 LICENSE 非阻断警告。`mcagent/crawler_llm_planner.py` 出现 Git 的 CRLF/LF 工作区提示，不影响测试。

### 49.5 下一步

继续优化 CrawlerAgent 的复盘闭环：把 reflection 返回结果也规范成更明确的 action contract，避免 LLM 返回“想重规划但没给任务”时由执行器补救过多。

## 50. CrawlerReflectionDecisionService：反思输出契约化（2026-05-22）

本轮开始前已重新阅读本文档。第 49 阶段已经把反思输入整理成 snapshot，本轮继续处理反思输出：CrawlerAgent LLM 仍然决定下一步行动，但返回给执行器的内容必须被整理成可检查的 action contract。这样可以把“LLM 的决定”和“工具能否执行”分开，避免工具层悄悄替 Agent 做主观决策。

### 50.1 本轮目标

让 `reflect_crawler_progress()` 返回的结果包含稳定 contract：

- action：`execute_pending`、`add_tasks`、`replan`、`finish`；
- selected_index：只在执行 pending task 时使用；
- tasks：只在 `add_tasks` 或 `replan` 时作为 LLM 给出的可执行任务；
- reason / done_summary：保留 CrawlerAgent 的理由和完成说明；
- contract：记录是否有效、有哪些结构问题、是否需要再次询问 Crawler planning LLM 把意图转成可执行工具任务。

### 50.2 代码变更

1. 新增 `mcagent/crawler_reflection_decision_service.py`：
   - `CrawlerReflectionDecisionService.normalize()`；
   - 只校验和规范 LLM 的反思输出；
   - 不选择来源、不生成搜索词、不判断是否采集足够。
2. 修改 `mcagent/crawler_llm_planner.py`：
   - `reflect_crawler_progress()` 解析 LLM JSON 后，把 action、selected_index、tasks 交给 decision service 生成统一 contract；
   - 保留 CrawlerAgent 原始 action，不把“想 replan 但没给 tasks”偷偷改成别的行为。
   - reflection LLM 连接失败时，也返回带 `reflection_llm_error` 的 contract，避免前端和执行器面对两种数据形状。
3. 修改 `mcagent/web_server.py`：
   - `agent_reflections` 中记录 `contract`；
   - 只有当 contract 明确 `requires_llm_task_materialization` 时，执行器才再次调用 Crawler planning LLM 生成可执行任务；
   - 这一步是“把 CrawlerAgent 的 replan 意图交回 Crawler LLM 落实”，不是工具层自己编任务。
4. 新增 `tests/crawler_reflection_decision_scenarios.py`：
   - 覆盖 selected_index 越界；
   - 覆盖 replan/add_tasks 没有可执行 tasks 时的契约问题；
   - 覆盖 finish 自动使用 reason 作为 done_summary；
   - 覆盖无效 action 不会把 tasks 误当作工具层决定。
5. 更新 `.github/workflows/ci.yml`、`README.md`、`scripts/public_readiness_check.py`。

### 50.3 边界说明

`CrawlerReflectionDecisionService` 不是新的 Agent，也不是 planner。它只回答：“CrawlerAgent 刚才给出的决定能否被执行？如果不能，是缺 selected_index、缺 tasks，还是 action 不合法？”真正的下一步仍来自 CrawlerAgent LLM；执行器最多把 contract issue 交回 Crawler planning LLM 请求补全可执行任务。

### 50.4 本轮测试方案与结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\crawler_reflection_decision_service.py mcagent\crawler_llm_planner.py mcagent\web_server.py tests\crawler_reflection_decision_scenarios.py
python tests\crawler_reflection_decision_scenarios.py
python tests\crawler_reflection_service_scenarios.py
python tests\job_view_service_scenarios.py
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\crawler_delegation_service.py mcagent\crawler_reflection_decision_service.py mcagent\crawler_reflection_service.py mcagent\evidence_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\job_view_service.py mcagent\rag_service.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\crawler_delegation_service_scenarios.py tests\crawler_reflection_decision_scenarios.py tests\crawler_reflection_service_scenarios.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\job_view_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\crawler_delegation_service_scenarios.py
python tests\crawler_reflection_decision_scenarios.py
python tests\crawler_reflection_service_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\evidence_service_scenarios.py
python tests\rag_service_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/api/jobs=200，POST /api/session/context=200，/api/agents/crawler_agent/tools=200
~~~

公开检查仍只有 LICENSE 非阻断警告。`mcagent/crawler_llm_planner.py` 仍有 Git 的 CRLF/LF 工作区提示，不影响测试。

### 50.5 下一步

继续把 `_run_crawler_job()` 的循环拆成更明确的 CrawlerRuntime 服务：把“反思、执行、观察、再次反思”的状态迁移出 `web_server.py`，让后端路由只负责启动 job 和推送状态，减少固定流水线对 CrawlerAgent 的影响。

## 51. CrawlerRuntimeStepService：反思动作应用服务化（2026-05-22）

本轮开始前已重新阅读本文档，并在第 50 阶段基础上继续拆 `_run_crawler_job()`。本阶段只抽取“把 CrawlerAgent 已经选好的 reflection action 应用到任务队列”的逻辑，不改变 CrawlerAgent 如何思考，也不让服务替 CrawlerAgent 选择来源或查询词。

### 51.1 本轮目标

把以下队列操作从 `web_server.py` 中移出：

- 记录 `agent_reflections` 条目；
- 将 `add_tasks` / `replan` 返回的新任务插到当前 index 前；
- 去重，避免重复执行同一 source/query；
- 根据 `selected_index` 调换下一步要执行的 pending task；
- 根据 `finish` action 写入 finish reason。

这些都是“执行 CrawlerAgent 已选 action”的机械动作，不属于主观决策。

### 51.2 代码变更

1. 新增 `mcagent/crawler_runtime_step_service.py`：
   - `CrawlerRuntimeStepService.reflection_entry()`；
   - `CrawlerRuntimeStepService.apply_action()`；
   - `task_identity()` 和 `_insert_unique()` 仅用于客观去重和队列插入。
2. 修改 `mcagent/web_server.py`：
   - `_run_crawler_job()` 创建 `runtime_step`；
   - reflection entry 构建改由服务负责；
   - replan/add_tasks 插入、finish、selected_index swap 改由服务执行；
   - materialization 仍只在 CrawlerAgent contract 明确缺少 executable tasks 时触发，并交回 Crawler planning LLM。
3. 新增 `tests/crawler_runtime_step_service_scenarios.py`：
   - 验证 reflection entry 保留 contract；
   - 验证 replan 新任务插入当前 index；
   - 验证重复任务不会插入；
   - 验证 selected_index 会调换 pending task；
   - 验证 finish reason 正确返回。
4. 更新 `.github/workflows/ci.yml`、`README.md`、`scripts/public_readiness_check.py`。

### 51.3 边界说明

`CrawlerRuntimeStepService` 不是 planner，也不读取网页，不调用 LLM。它只把 CrawlerAgent LLM 已经输出的 reflection decision 作用到本轮任务队列上。是否重规划、是否结束、是否选择浏览器，仍由 CrawlerAgent 在 reflection 阶段决定。

### 51.4 本轮测试方案与结果

已执行并通过：

~~~powershell
python -m py_compile mcagent\crawler_runtime_step_service.py mcagent\web_server.py tests\crawler_runtime_step_service_scenarios.py
python tests\crawler_runtime_step_service_scenarios.py
python tests\crawler_reflection_decision_scenarios.py
python tests\job_view_service_scenarios.py
python -m py_compile api.py mcagent\agent_execution.py mcagent\agent_executor.py mcagent\agent_router.py mcagent\crawler_delegation_service.py mcagent\crawler_reflection_decision_service.py mcagent\crawler_reflection_service.py mcagent\crawler_runtime_step_service.py mcagent\evidence_service.py mcagent\event_stream.py mcagent\fastapi_app.py mcagent\job_view_service.py mcagent\rag_service.py mcagent\session_state.py mcagent\agent_runtime.py mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\public_readiness_check.py tests\crawler_delegation_service_scenarios.py tests\crawler_reflection_decision_scenarios.py tests\crawler_reflection_service_scenarios.py tests\crawler_runtime_step_service_scenarios.py tests\agent_execution_scenarios.py tests\agent_executor_scenarios.py tests\agent_router_scenarios.py tests\evidence_service_scenarios.py tests\job_view_service_scenarios.py tests\rag_service_scenarios.py tests\agent_runtime_scenarios.py tests\backend_services_scenarios.py tests\fastapi_backend_scenarios.py
python tests\crawler_delegation_service_scenarios.py
python tests\crawler_reflection_decision_scenarios.py
python tests\crawler_reflection_service_scenarios.py
python tests\crawler_runtime_step_service_scenarios.py
python tests\agent_executor_scenarios.py
python tests\agent_router_scenarios.py
python tests\evidence_service_scenarios.py
python tests\rag_service_scenarios.py
python tests\backend_services_scenarios.py
python tests\fastapi_backend_scenarios.py
python tests\smoke_test.py
python scripts\check_text_encoding.py
python scripts\public_readiness_check.py
node --check frontend\static\app.js
node --check frontend\static\settings.js
git diff --check
python api.py --host 127.0.0.1 --port 8766
# 探针结果：/api/health=200，/api/jobs=200，/api/agents/crawler_agent/tools=200，POST /api/session/context=200
~~~

公开检查仍只有 LICENSE 非阻断警告。

### 51.5 下一步

继续拆 `_run_crawler_job()` 中的工具执行结果归档逻辑：把 task_payload 构造、空查询拒绝、manifest_stats、topic_validation、success/failure 计数更新整理成服务，进一步缩小 `web_server.py` 的责任。
