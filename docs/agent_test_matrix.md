# MCagent / CrawlerAgent Five-Direction Test Matrix

This matrix is a standing regression suite for both agents. Every change to agent prompts, tool routing, Crawler, RAG, SSE, frontend trace text, or backend routes must cover these five directions. 测试例子不是硬编码规则；新增例子不能直接写成关键词分支。

## Principles

- MCagent and CrawlerAgent must let the LLM read the user goal, session context, tool catalog, and prior observations before choosing the next action.
- Tools execute objective operations only: retrieve, fetch, render, search, save, ingest, report status, and explain failures.
- Direct Crawler conversations must remain direct Crawler work, not MCagent handoff text.
- "Do not save" means no `crawler_exports` write and no ingest.
- "Save /补库/给 MCagent 用" means Crawler must persist Markdown/raw HTML/report/manifest and make the result ingestible.

## 方向一：MCagent 回答本地已有资料

Goal: verify MCagent can answer naturally from local RAG evidence without waking Crawler unnecessarily.

Examples:

- “介绍一下乌托邦探险之旅整合包有哪些玩法。”
- “落幕曲新手该怎么玩？”
- “本地资料里乌托邦探险之旅有哪些版本信息？”
- “MC_Agent 项目现在有哪些 Agent 和工具？”

Expected:

- MCagent understands the question and uses local RAG when evidence is needed.
- Final answer cites or reflects the selected evidence and states uncertainty when evidence conflicts.
- Ordinary "怎么获取/有哪些/玩法" game questions do not automatically trigger Crawler.

## 方向二：MCagent 发现本地资料不足并委托 Crawler

Goal: verify MCagent can summarize local knowns and gaps, then only delegate when its planned workflow explicitly chooses Crawler.

Examples:

- “现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。”
- “落幕曲的全部拔刀剑配方本地资料够吗？不够就让 Crawler 补。”
- “先用本地资料介绍某整合包 Boss；如果没有完整清单，就让 Crawler 补齐 Boss 名称、地点、召唤方式和掉落。”
- “本地资料有 Utopia Journey 的模组列表吗？缺什么让 Crawler 找。”

Expected:

- MCagent first retrieves and summarizes local evidence.
- Handoff to Crawler is a complete natural-language task with context, gaps, delivery target, and acceptance criteria.
- `collection_target` is not a broken search keyword.
- `requested_by` is `mcagent` or `user_via_mcagent`, not a fake direct-user task.

## 方向三：用户直连 Crawler 获取指定网页数据但不保存

Goal: verify direct CrawlerAgent requests can read public pages and answer without persistence.

Examples:

- “总结一下 https://baike.baidu.com/item/%E5%95%86%E5%93%81/1245866 的内容给我，不用保存到本地。”
- “读取 https://example.com/ 的标题和正文摘要，不要入库。”
- “帮我看一个公开文档 URL 的主要结论，只在聊天里回答。”
- “打开一个无需登录的新闻/百科/文档页面，提炼 5 条要点，不要保存文件。”

Expected:

- Trace agent is `crawler_agent`; no MCagent handoff wording appears.
- CrawlerAgent may choose `temporary_extract`, `fetch_url`, or browser tools based on observations.
- When the user says not to save, result metadata says `saved_to_local=false`.
- If blocked, Crawler reports the concrete reason: network error, 403, login, captcha, text too short, JS-only page, or parser failure.

## 方向四：Crawler 为 MCagent/RAG 找资料并保存入库

Goal: verify Crawler can gather public data, save it in RAG-friendly form, and make it usable by MCagent.

Examples:

- “Crawler，去网上找乌托邦探险之旅的玩法、模组列表和版本差异，保存到本地给 MCagent 用。”
- “帮 MCagent/RAG 补充落幕曲 Boss 列表、打法和掉落资料。”
- “收集某个 Minecraft 模组的官方文档、下载页和教程，清洗入库。”
- “找一个公开整合包的 README、百科页、Modrinth/CurseForge 页面，保存成 MCagent 能引用的资料。”

Expected:

- `delivery_target` is `MCagent/RAG`.
- Crawler chooses among generic tools such as `web_discovery`, `fetch_url`, `playwright`, `browser_collect`, `modpack_download`, `modpack_internal`, `read_local_file`, `search_local_files`, and `save_artifact`.
- Saved outputs include manifest plus readable evidence with URL/path/source metadata.
- Crawler does not treat "MCagent/RAG/入库" as search target text.

## 方向五：Crawler 获取网页/数据并保存到用户指定本地位置

Goal: verify Crawler can perform non-Minecraft data collection and save results to a user-specified local path.

Examples:

- “Crawler，找 20 个不需要登录的开源项目列表，保存名称、链接、简介到 C:\\Users\\67425\\Desktop\\front-end-practice\\test-output。”
- “从一个公开商品/书籍/电影榜单页面提取名称、价格/评分、链接，保存 CSV 和 JSON。”
- “抓取一个公开 API 文档页面的章节标题和链接，保存到指定目录。”
- “用浏览器打开无需登录的网站，提取页面表格，保存到本地并告诉我哪些字段没抓到。”

Expected:

- Crawler picks tools from the catalog instead of hardcoded website rules.
- The save path comes from the user when provided; otherwise Crawler uses project default export directories.
- Output includes manifest/report/CSV/JSON/Markdown paths.
- Login, captcha, anti-bot, missing fields, or extraction failures are reported honestly rather than marked as completed.
