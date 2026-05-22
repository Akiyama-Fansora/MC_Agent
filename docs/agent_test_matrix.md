# MCagent / CrawlerAgent 五方向测试矩阵

本矩阵是两个 Agent 的长期回归测试要求。以后修改 Agent prompt、工具选择、Crawler、RAG、SSE、前端状态文案或后端路由后，都必须至少覆盖这五个方向。

测试原则：

- 测试例子不是硬编码规则，不能把某个例句写成关键词触发。
- MCagent 与 CrawlerAgent 都应由 LLM 读取用户目标、上下文、工具目录和历史状态后选择下一步。
- 工具只执行客观动作：检索、读取网页、保存文件、入库、查看状态、格式化结果、报告失败原因。
- CrawlerAgent 可以直接接收用户任务，也可以接收 MCagent 的交接任务；两种身份链必须区分。
- 如果用户要求“不保存到本地”，CrawlerAgent 可以选择临时读取/摘要工具；如果用户要求“保存/补库/给 MCagent 用”，CrawlerAgent 应选择会持久化的采集链路。

## 方向一：MCagent 回答本地已有资料

目标：验证 MCagent 能基于本地 RAG 证据组织自然回答，不乱派 Crawler。

示例：

- “介绍一下乌托邦探险之旅整合包有哪些玩法。”
- “落幕曲新手该怎么玩？”
- “本地资料里乌托邦探险之旅有哪些版本信息？”
- “MC_Agent 项目现在有哪些工具和 Agent？”

期望：

- MCagent 先理解问题，再选择本地 RAG/最终回答。
- 回答引用本地证据；如果证据有冲突，要说明不确定点。
- 不因为问题里有“获取/有哪些”等普通语义就自动派 Crawler。

## 方向二：MCagent 发现本地资料不足并委托 Crawler

目标：验证 MCagent 能总结本地已有资料和缺口，再把完整自然语言需求交给 CrawlerAgent。

示例：

- “现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。”
- “落幕曲的全部拔刀剑配方本地资料够吗？不够就让 Crawler 补。”
- “先用本地资料介绍某整合包的 Boss；如果没有完整清单，就让 Crawler 补齐 Boss 名称、地点、召唤方式和掉落。”
- “本地资料有哪些 Utopia Journey 的模组列表？缺什么让 Crawler 找。”

期望：

- MCagent 先检索/总结，再形成资料缺口摘要。
- 交给 Crawler 的 `collection_target` 是完整任务说明，不是拆碎搜索词。
- `requested_by` 应是 `mcagent` 或 `user_via_mcagent`，不是用户直连。
- Crawler 读 handoff 后自己规划来源、工具和保存格式。

## 方向三：用户直连 Crawler 获取指定网页数据但不保存

目标：验证用户切到 CrawlerAgent 后，Crawler 作为独立 Agent 处理临时网页读取/摘要任务，不显示 MCagent 转发。

示例：

- “总结一下 https://baike.baidu.com/item/%E5%95%86%E5%93%81/1245866 的内容给我，不用保存到本地。”
- “读取 https://example.com/ 这个页面，提取标题和正文摘要，不要入库。”
- “帮我看一下某个公开文档 URL 的主要结论，只在聊天里回答。”
- “打开一个无需登录的新闻/百科/文档页面，提炼 5 条要点，不要保存文件。”

期望：

- trace 中 `agent` 是 `crawler_agent`，身份链是用户直接委托 CrawlerAgent。
- CrawlerAgent 自己决定使用临时读取、Jina、Playwright 或其他工具。
- 如果用户明确“不保存”，结果中不应写入 crawler_exports，不应触发 ingest。
- 如果网页失败，要说明失败原因，例如网络、403、验证码、登录、文本过短。

## 方向四：Crawler 为 MCagent/RAG 找资料并保存入库

目标：验证 CrawlerAgent 能把资料保存成本地 RAG 可用格式，完成后 MCagent 能检索到。

示例：

- “Crawler，去网上找乌托邦探险之旅的玩法、模组列表和版本差异，保存到本地给 MCagent 用。”
- “帮 MCagent/RAG 补充落幕曲 Boss 列表、打法和掉落资料。”
- “收集某个 Minecraft 模组的官方文档、下载页和教程，清洗入库。”
- “找一个公开整合包的 README、百科页、Modrinth/CurseForge 页面，保存成 MCagent 能引用的资料。”

期望：

- `delivery_target` 是 `MCagent/RAG`。
- Crawler 保存 Markdown/manifest/raw HTML 或 raw text，并记录 URL、来源、失败原因。
- 采集完成后触发或提示 ingest；MCagent 重新检索能看到新增资料。
- Crawler 不应把 “MCagent/RAG/入库” 当成搜索目标。

## 方向五：Crawler 获取网页/数据并保存到用户指定本地位置

目标：验证 CrawlerAgent 能处理普通非 MC 采集任务，按用户指定目录保存文件，并报告保存路径和失败原因。

示例：

- “Crawler，找 20 个不需要登录的开源项目列表，保存名称、链接、简介到 C:\Users\67425\Desktop\front-end-practice\test-output。”
- “从一个公开商品/书籍/电影榜单页面提取名称、价格/评分、链接，保存 CSV 和 JSON。”
- “抓取一个公开 API 文档页面的章节标题和链接，保存到指定目录。”
- “用浏览器打开无需登录的网站，提取页面上的表格，保存到本地并告诉我哪些字段没抓到。”

期望：

- Crawler 自己决定使用 browser_collect、Playwright、Jina、web_discovery 等工具。
- 保存目录来自用户目标；未指定时才使用项目默认 crawler_exports。
- 输出 manifest/report/CSV/JSON/Markdown 等客观文件路径。
- 如果登录/验证码/反爬/字段缺失导致失败，要给出明确失败原因，而不是假装完成。
