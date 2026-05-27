# MCagent / Crawler 开发运行文档

本文档是 MCagent 与 CrawlerAgent 的长期开发约定。修改检索、爬虫、Agent prompt、数据清洗、状态 UI、派单逻辑之前，先读本文档；修改完成后，必须同步更新本文档。

最后更新：2026-05-17

## 1. Agent 边界

系统里只有两个真正的 Agent：

- `MCagent`：面向用户问答的 Agent。它负责理解当前会话、检索本地 RAG、基于证据回答、判断资料缺口。
- `Crawler`：独立爬虫/研究 Agent。它可以接收 MCagent 的资料缺口，也可以直接接收用户的采集任务。它负责判断怎么搜、搜哪些源、怎么抓、怎么清洗、怎么保存、怎么总结。

`仅检索` 不是 Agent，只是 MCagent 下面的调试/运行模式：只返回本地检索结果，不调用回答模型。

## 1.1 LLM 主导原则

任何 Agent 都必须是 **LLM 为主，工具函数为辅**。

工具函数、脚本、规则、检索器、爬虫 provider 只能负责：

- 提供候选资料、候选主题、候选链接。
- 执行明确动作，例如搜索、抓网页、保存 HTML、清洗 Markdown、去重、统计 manifest。
- 做客观校验，例如 return code、records 数、URL 是否重复、正文是否为空。

工具不能替代 LLM 做主观决策，例如：

- 哪些主题重要。
- 哪些候选是噪声。
- 下一轮应该搜什么。
- 用户真正想要的是完整整合包、某个物品、教程还是表格。
- 当前资料是否足以回答。

正确流程是：

1. 工具产出候选和证据。
2. Crawler/MCagent 的 LLM 读取候选、上下文和目标。
3. LLM 判断取舍、拆分任务、选择来源、决定是否继续。
4. 工具再执行 LLM 决定的具体动作。

例子：`discover_topic_seeds.py` 只能从已有资料里抽出“聚晶、百胜竞技场、塔罗牌、女仆、诡厄巫法”等候选词；它不能自行决定这些都要抓。必须由 Crawler LLM 判断哪些候选与目标相关、哪些是噪声、哪些应该进入下一轮采集。

## 2. 调用者与交付对象

不要只根据“谁发起任务”决定输出格式。要区分：

- 调用者：`用户`、`MCagent`、未来其他 Agent。
- 交付对象：`用户可读`、`MCagent RAG`、`原始归档`、`表格/报告`、`两者都要`。

例子：

- 用户说：“Crawler，获取乌托邦完整数据，给 MCagent 用。”调用者是用户，交付对象是 MCagent RAG。
- MCagent 说：“我缺少落幕曲新手路线证据。”调用者是 MCagent，交付对象通常是 MCagent RAG。
- 用户说：“Crawler，把这些网页原始 HTML 下载下来并给我总结。”调用者是用户，交付对象是用户可读 + 原始归档。

当交付对象是 MCagent/RAG 时，Crawler 保存数据时必须优先满足 RAG 可用：

- Markdown 必须有稳定标题和来源 URL。
- `manifest.json` 必须记录 query、records、skipped、errors、来源 URL、输出路径。
- 抓取器支持 raw HTML 时必须保存 raw HTML 路径。
- metadata 要包含 source、project/entity、query、fetched_at、内容 hash/fingerprint 等可过滤字段。
- 切分后仍要能引用，表格/列表上下文不能被切碎到无法回答。

## 3. MCagent 行为规则

MCagent 应该这样工作：

1. 先理解当前会话，尤其是“这些 / 它们 / 上述 / 刚才”这类追问。
2. 先检索本地 RAG。
3. 把用户问题和筛选后的证据交给回答模型。
4. 只基于证据回答。证据不完整就说明缺口。
5. 证据不足时，把资料缺口交给 Crawler，内容包括：
   - 目标实体
   - 已知上下文
   - 缺少哪些证据
   - 交付对象
   - 成功标准
6. Crawler 入库后，MCagent 必须重新检索本地 RAG，再判断是否可以回答。

MCagent 不允许为了显得完整而编造资料中没有的内容。

## 4. Crawler 行为循环

Crawler 是独立 Agent，应该形成完整循环：

1. 理解：识别调用者、交付对象、目标实体、缺失证据、限制条件。
2. 规划：定义覆盖目标、短查询词、数据源、成功标准。
3. 试采：优先跑高可信、高收益的小任务。
4. 验证：检查 `records`、`skipped`、`errors`、正文长度、标题/URL/正文是否命中主题。
5. 重试/扩展：如果空结果、失败或跑偏，换查询词、换来源，必要时用 raw HTML 或 Playwright 兜底。
6. 保存：保留 Markdown、manifest、来源 URL、raw HTML。
7. 入库：只有产生有效新 records 且主题匹配时才入库。
8. 总结：说明收集了什么、哪些失败、下一轮应该怎么试。

重要规则：return code 为 0 但 `records == 0` 是空结果，不是成功采集，不能触发 ingest。

另一个重要规则：`records > 0` 也不一定成功。必须检查主题相关性。比如采集 `落幕曲 Closing Song` 时，抓到天文学 twilight、Kafka、音乐网页等都属于跑偏，必须拒绝或隔离，不能入库。

中途重规划规则：

- Crawler 任务执行不是一次性固定列表，而是队列式执行。
- 连续 3 个结果为空、跑偏或命令失败，且还没有有效成功时，调用 Crawler LLM 做中途重规划。
- 重规划时要把最近失败摘要、已尝试的 source/query、交付对象、成功标准交给 Crawler LLM。
- 新任务必须去重，不能重复已经尝试过的 source/query。
- 默认最多重规划 2 次，任务总数默认封顶 32，避免无限扩散。
- `empty_result` 与 `off_topic_result` 都计入失败统计，不能显示成“成功 1，失败 0”这种误导状态。

## 5. 完整整合包资料采集标准

当用户要求“获取某整合包完整数据”时，Crawler 应尽量覆盖：

- 基本信息：名称、别名、作者、版本、加载器、MC 版本。
- 官方/下载/社区链接：MC百科、Modrinth、CurseForge、GitHub、Bilibili、QQ群等。
- 整合包内容：模组列表、依赖、可选资源包、配置说明。
- 新手路线：开局步骤、任务书/FTB 任务、早期资源、难度提醒。
- 核心系统：魔法、科技、战斗、维度、经济、特殊机制。
- 关键物品与获取路线。
- 配方、表格、图片；必要时保留 raw HTML 供后续查表。
- Boss 与进度门槛。
- 视频/教程索引；没有字幕/正文时只能引用标题和简介，不能假装看过视频内容。
- 已知问题、版本注意事项、安装/开服说明。

面向 MCagent/RAG 时，优先选择少量高质量页面 + raw HTML + 干净 Markdown，不要大量重复浅页面。

## 6. 数据源策略

| 数据源 | 使用场景 |
| --- | --- |
| `mcmod` | 中文 MC 模组、整合包、教程、评论、表格、MC百科页面。 |
| `modrinth` | 项目元数据、整合包 contents、版本、链接。 |
| `followup` | 从项目元数据发现 Source/Wiki/README/docs 后继续抓。 |
| `web_discovery` | 公开网页搜索与正文抽取，适合教程和资料页发现。 |
| `fetch_url` | 本地 HTTP 读取指定公开 URL，保存正文、raw HTML 和 manifest，无需第三方 API key。 |
| `browser_collect` | 浏览器结构化采集列表/商品/表格，输出 XLSX/CSV/JSON/report/manifest。 |
| `playwright` | JS 页面、复杂表格、渲染页面、raw HTML 和截图兜底。 |
| `mediawiki` | 原版 Minecraft 机制。 |
| `ftbwiki/createwiki` | 已知大型模组生态的专门 Wiki。 |

升级策略：

- `mcmod` 搜索为空：换短中文名、英文别名，用 web_discovery 找 MC百科直达 URL。
- `modrinth` 元数据太浅：用 `followup` 跟进 Source/Wiki/README，再用网页源补教程。
- `web_discovery/playwright` 报错或空：换 local URL fetch 和 web_discovery。
- 正文抽取漏表格/图片：查 raw HTML，必要时 Playwright。
- 多来源重复：保留不同文章/页面，删除完全相同 URL 或内容指纹重复项。

## 7. 查询规划规则

Crawler 规划器不能把整段用户任务当搜索词。

好的查询词：

- `落幕曲 Closing Song`
- `落幕曲 新手攻略`
- `落幕曲 拔刀剑 梦想一心`
- `落幕曲 至纯之血`
- `Utopia modpack mod list`

坏的查询词：

- `让 Crawler 继续获取落幕曲 Closing Song 整合包的完整资料，这批数据要给 MCagent RAG 入库使用...`

当任务里出现 `MCagent`、`RAG`、`入库`、`切分`、`能看懂` 时，这些通常是交付要求，不是搜索目标。

## 8. 状态与进度 UI

UI 应该让进度可观察：

- Agent 列表只显示 `MCagent` 和 `Crawler`。
- `仅检索` 保留为模式开关。
- Crawler job 显示当前任务序号、source、query、reason、status。
- Crawler result 包含 planned_tasks 和 loop 状态。
- 空结果显示 `empty_result` 与 manifest_stats。
- 跑偏结果显示 `off_topic_result` 与 topic_validation。
- 中途重规划时显示 `replan` 阶段；最终 result 包含 `replan_count`，plan 内记录 `replans`。
- Crawler 完成后必须给出 `collection_summary`，说明本轮新增、重复、空结果、错误、跑偏和下一步建议。
- `/api/crawler/summary` 可查看最近 manifest 汇总，便于不重跑任务也能判断数据有没有用。
- 状态接口不能每次做昂贵全量扫描。

## 9. 当前已知问题

- 已发现历史乱码会影响会话摘要、追问实体抽取、证据行抽取和答案元信息剥离。后续任何源码/文档改动必须先通过乱码检测脚本。
- Crawler LLM 有时返回空或非法 JSON，target-aware fallback 必须保持可靠。
- Crawler LLM 规划偶尔超时或返回非 JSON。当前 fallback 能继续执行，但后续要增强 prompt/JSON 修复与可观察错误。
- 部分托管搜索源对中文小众主题会返回 0 records 或跑偏结果，需要更强主题校验与短查询重试。
- 本轮发现 local URL fetch 对 `落幕曲 Closing Song MC百科` 抓到 Kafka/Wikipedia 跑偏页面，必须清理并防止入库。
- 停止 Crawler job 目前只设置 stop 标记，还不能中断正在运行的子进程；后续要补真正的子进程取消。

## 10. 近期变更记录

2026-05-17：

- Agent 列表改为两个真实 Agent：`MCagent` 与 `Crawler`。
- `仅检索` 恢复为 MCagent 的运行模式，不再作为 Agent。
- Crawler prompt 明确：Crawler 是独立 Agent；调用者和交付对象分开判断。
- Crawler plan schema 增加 `delivery_target` 与 `cleaning_policy`。
- LLM 规划失败时增加 target-aware fallback，避免整句搜索。
- 空结果不再触发 ingest，标记为 `empty_result`。
- 启动落幕曲继续补库任务：`1779011197830-1`。
- 发现跑偏入库问题：local URL fetch 产出 Kafka/Wikipedia 噪声记录。下一步必须做主题相关性校验和清理。
- 已清理 Kafka/Wikipedia 跑偏记录，重建 FTS 与向量索引。
- Crawler 入库前增加主题相关性校验：有 records 但不命中目标主题时标记 `off_topic_result`，不触发 ingest。
- 验证任务 `1779013057758-1`：8 个任务全部无有效新增；followup 的 2 条 records 被判定跑偏且未入库。
- Crawler 执行循环改为队列式，支持连续空/跑偏/失败后的中途重规划。
- 新增失败摘要、任务去重、重规划记录：`_crawler_failure_summary`、`_crawler_task_identity`、`_replan_crawler_tasks`。
- 修正失败统计：`empty_result` 与 `off_topic_result` 计入 `failure_count`。
- 验证任务 `1779013754416-1`：Closing Song 小任务中 MC百科 1 条主题匹配并入库，Modrinth/legacy hosted search/legacy hosted crawler 为空；验证了 query/reason 记录与空结果标记。该任务有成功结果，所以未触发中途重规划，符合规则。
- 新增 Crawler 采集总结器：读取 job task results 与 manifest，汇总 records、skipped、errors、重复跳过、低相关跳过、raw HTML 数量、可用记录样例和下一步建议。
- Crawler job 最终 result 增加 `collection_summary`；新增 `/api/crawler/summary` GET/POST，用于查看最近导出 manifest 的汇总。
- 修正 source alias：`modrinth_agent` 归一为 `modrinth`。
- 验证任务 `1779014597725-1`：不存在查询返回失败，但 result 正确包含 `collection_summary`、空结果任务和下一步建议。
- 强化 MC百科抓取：`fetch_mcmod_seed.py` 的查询拆分改为通用短词拆分，过滤“玩法/攻略/获取/合成/入库/RAG”等非实体词。
- MC百科抓取增加 Bing `site:mcmod.cn` 外部发现兜底，只接受 `class/item/post/modpack/course` 等 MC百科内容 URL，并继续保存 Markdown 与 raw HTML。
- 验证查询 `落幕曲 梦想一心 获取步骤`：搜索短词包含 `落幕曲 梦想一心`、`梦想一心` 等；成功发现并保存 `【攻略】如何从0开始快速制作梦想一心`，同时记录 `external_search_results`。
- 修正 Crawler 派单链路：`_delegate_crawler_for_missing_data()` 现在会合并显式 `session_summary`，不再覆盖用户/MCagent 提供的 `coverage_goals`、`known_context`、`delivery_target`。
- 修正 Crawler fallback 规划：LLM JSON 失败时也会读取 `session_summary.coverage_goals`，生成覆盖型短查询；完整整合包采集会优先安排 MC百科教程/资料页。
- 修正任务截断策略：coverage query 会以高优先级插入，避免 LLM 原始计划只围绕 `Closing Song`、Boss 或 TACZ，把梦想一心/至纯之血/嬗变台等关键主题挤掉。
- 启动落幕曲覆盖型采集任务 `1779016713707-1`：计划已确认包含 `落幕曲 新手路线`、`落幕曲 FTB任务`、`落幕曲 拔刀剑 获取步骤`、`落幕曲 梦想一心 获取步骤`、`落幕曲 至纯之血 获取` 等 16 个任务。
- 用户指出：完整采集不能只围绕历史对话里的纯洁之血、拔刀剑、梦想一心等已知词；Crawler 必须从整合包本身和已有资料中发现未知主题。
- 新增 `discover_topic_seeds.py` 作为候选发现工具：它只负责从本地已有资料中抽候选主题/seed query，不负责判断哪些重要。
- Crawler 对 topic discovery 的后续扩展改为 LLM 审核：候选主题必须交给 Crawler LLM 判断取舍后再生成采集任务；规则 fallback 只能在 LLM 失败时兜底。
- 修正 Crawler 直连 source：用户明确指定 `source=topic_discovery` 或其他 provider 时，后端不再强行覆盖为 planner。
- 验证任务 `1779017794348-1`：`source=topic_discovery` 已按“主题种子发现”运行，不再被错误改成 MC百科搜索。

## 11. 下一步开发任务

1. 给 Crawler job 停止逻辑补真正的子进程中断。
2. Crawler LLM 审核 topic discovery 候选时，job 状态要显示 `reviewing_candidates`，避免看起来卡住。
3. 在 RAG 回答路径中支持选中 Markdown 不够时回查 raw HTML。
4. 让 Crawler LLM 基于 `collection_summary` 生成更自然的中文任务报告。
5. 保持源码、文档、prompt、前端文本全部为 UTF-8；发现乱码时先修源文件，再继续功能开发。
6. 等落幕曲发现式采集跑完并用 `collection_summary` 验证后，再决定继续补落幕曲还是启动乌托邦整合包完整采集。

2026-05-17 补充记录：

- Crawler 任务结果需要区分“已抓取但暂缓入库”和“已抓取并已入库”。如果 records 需要后续复判，应标记为 `ingest_deferred`，不要直接写入 FTS/向量索引。
- Crawler job 状态页需要展示 `collection_summary`，并把 `loop.ingest` 的状态区分为 running、done、failed。
- `topic_discovery` 是候选发现阶段，不是最终证据。它应输出候选数量、候选来源、交给 Crawler LLM 审核的摘要，以及 LLM 选择/拒绝理由。
- 对 `落幕曲 / Closing Song` 这类中英混合主题，标题、别名、正文和 URL 都应参与判断。不能只按一个中文名或英文名做硬匹配。
- 任务 `1779021152280-1` 暴露的问题：topic discovery 能发现 TACZ 等候选，但后续需要 Crawler LLM 把候选扩展为多源采集任务，而不是停留在候选摘要。
- 任务 `1779021601488-1` 暴露的问题：job 结束后应明确是否完成 ingest；fallback 任务应继承 `session_summary.last_result`，继续围绕 MCagent 缺口，例如 Boss、TACZ、FTB 任务、装备路线、打法和地点。

修复原则：

- Crawler 的主观判断必须由 LLM 完成，工具只提供 source、query、正文、URL、候选和统计。
- 如果资料要给 MCagent/RAG 使用，Crawler 需要保存 Markdown、manifest、raw HTML 路径、来源 URL、清洗策略和可切分正文。
- 每次 ingest 后必须更新 `crawler_exports` 汇总和数据库统计，状态页能看出新增、重复、跳过、失败。
- fallback 任务必须读取 `session_summary`、MCagent 缺口、用户交付要求和 Crawler LLM 的上一轮判断，再选择 legacy hosted search、legacy hosted crawler、local URL fetch、web_discovery、Playwright、MC百科或 Modrinth 等工具。
- `_select_diverse_tasks()` 只能做客观多样性选择，不能替代 Crawler LLM 判断哪些主题重要。
- fallback priority 只能作为执行队列排序提示，不能把某个来源或父主题共现变成硬门控。
- 后续需要验证：Boss 清单、Boss 地点、Boss 打法、掉落、GitHub README、MC百科教程、B站标题索引等资料能否入库并被 MCagent RAG 使用。

## 12. 组件宽词采集原则（2026-05-17）

用户已经明确指出：完整整合包资料采集不能把所有查询都强行写成“整合包名 + 组件名”。这是硬规则，后续开发必须遵守。

正确做法：

- 如果 Crawler/MCagent 已经从可靠上下文确认某个组件、系统、模组、Boss、物品属于目标整合包，就允许单独搜索该组件词。
- 例如目标是“落幕曲 Closing Song”，已知组件包括 `TACZ`、`FTB Quests`、`SlashBlade`、塔罗牌、女仆、亚波伦、诅咒饰品、黑魔法等，那么 Crawler 可以直接搜索 `TACZ`、`FTB Quests`、`塔罗牌`、`亚波伦 Boss`，不要求查询词里必须带“落幕曲”。
- 组件页、模组页、教程页本身不一定会写父整合包名。不能因为页面没有同时出现“落幕曲/Closing Song”就直接判定跑偏。
- 是否纳入目标整合包资料，由 Crawler LLM 基于任务目标、已知组件列表、来源、标题、正文、URL、采集理由综合判断。
- 工具函数只能做候选抽取、关键词命中、Minecraft 上下文检查、URL/正文统计等客观工作；不能代替 LLM 做“这个组件是否属于整合包资料”的主观决定。
- 当 LLM 相关性判断失败时，结果应标记为 `uncertain_result`，用于后续复判或换源补抓；不能退回到“缺少父整合包名就 off_topic”的硬门控。
- 真正的跑偏例子是天文学 twilight、Kafka、普通音乐网页、与 Minecraft/Mod/组件无关的网页。这类可以拒绝或隔离。

开发检查点：

1. Planner 生成任务时，允许同时存在父主题查询和组件宽词查询。
2. `topic_validation` 应区分 `direct`、`component`、`noise`、`uncertain`、`llm_judge_error_uncertain`。
3. 规则层的组件词命中只能写入 `component_candidates`，作为给 Crawler LLM 看的客观提示；不能把它当成最终通过理由，更不能替代 LLM 判断。
4. 面向 MCagent RAG 的资料仍需保存 Markdown、manifest、source URL、metadata、raw_html 路径和可切分上下文。

## 13. RAG 关系检索与答案修复原则（2026-05-17）

本节记录一次重要修复：用户问“X 里的 Y 是什么/怎么用”时，系统不能只拿 `X Y` 硬共现检索，也不能只因为组件资料标题不含父主题 `X` 就判定资料不足。

正确行为：

- MCagent 检索规划要识别父主题和组件主题，例如“落幕曲里的塔罗牌”应拆成 `落幕曲`、`塔罗牌`、`落幕曲 塔罗牌`。
- 检索排序时，当前问题的核心锚点应优先取组件主题 `Y`。组件解释题必须让标题或正文命中 `Y`，避免被只提到父主题 `X` 的泛资料压住。
- 允许组件资料作为回答证据。只要会话摘要、检索计划或父主题资料能支持“Y 是 X 的相关组件/系统”，模型可以先解释 `Y` 本身，再说明与 `X` 的关联强弱。
- 证据筛选器的“标题匹配”不能只匹配整句问题。对于“X 里的 Y”“X 中的 Y”“X 的 Y”这类问题，标题命中 `Y` 也应视为有效组件证据。
- topic_discovery 资料适合给 Crawler 做后续种子，不应在最终问答证据排序中压过正式词条、教程、项目文档、raw HTML 正文。
- 本地工具只能做客观抽取、排序和提示。解释题、机制题、用法题必须让 LLM 组织答案；本地“列表修复器”只能用于真正的列表/配方/Boss 枚举题，不能覆盖“是什么/怎么用/有什么用/作用/机制”类回答。
- 如果模型回答“无法确认父主题关联”，但证据中已经有组件资料和父主题间接证据，系统不应立刻触发补库；应先把“组件资料可回答、父主题关联程度有限”作为可展示答案。

本次验证用例：

- 问题：`落幕曲里的塔罗牌是什么？有哪些用法？`
- 期望：优先命中 MC百科塔罗牌/TarotCardsPlus/Tarot Deck 等资料，结合落幕曲相关来源说明关联；不再误触发 Crawler。
- 验证结果：端到端 `/api/chat` 已不再创建 Crawler job，回答可从本地资料生成。

## 14. MCagent 工具意识与第一手意图（2026-05-17）

MCagent 必须先接收用户原始话，再决定调用什么能力。后端路由只能承载 MCagent 的工具选择，不能在 MCagent 理解之前用关键词把消息截走。

MCagent 当前可用能力：

- `local_rag_search`：检索本地 RAG、FTS、raw HTML 资料，用于正常问答。
- `crawler_status`：查看 Crawler 任务、采集、入库、进度。用户问“状态/进度/监控/入库怎么样”时使用。
- `delegate_crawler`：把资料缺口交给 CrawlerAgent。用户明确说“叫/让 Crawler 收集/获取/爬取/补库某资料”时使用。
- `answer_from_evidence`：基于检索证据组织答案并标注来源。

执行原则：

- 用户原始消息永远优先。上下文改写只能作为检索补充，不能覆盖原始意图。
- “叫 CrawlerAgent 收集落幕曲的BOSS清单”应选择 `delegate_crawler`，并把干净任务“落幕曲的BOSS清单”交给 Crawler。
- “状态”应选择 `crawler_status`，不是普通 RAG 检索。
- “有哪些BOSS”这类追问应由 MCagent 根据会话上下文规划检索，例如扩展为“落幕曲 BOSS / 下亚 / 亚波伦”等，再查本地资料。
- 证据筛选不能只要求标题命中。整合包列表题如果正文同时命中父主题和目标实体，也可以进入模型判断；工具只做证据门控，最终是否足够由 MCagent 基于证据说明。
- EvidenceSelector 必须接收 MCagent 的检索计划。短追问如“有哪些BOSS”本身没有父主题，门控时要使用 plan 中的 `topic/subqueries/required_terms`，否则会把“落幕曲 BOSS”证据误判成标题不匹配。

## 15. 当前未完成问题与下一轮优化方向（2026-05-18）

用户反馈的核心问题不是某个关键词没匹配，而是两个 Agent 的思考链还不够像 Agent。后续所有修改必须遵守：LLM/Agent 做主观判断，工具函数只做客观执行、候选收集、状态读取和结果承载。禁止新增“针对某个测试句子的硬编码特例”来假装解决问题。

当前问题：

1. 前端会在 `/api/chat` 前先调用 `/api/search` 做预览，导致用户看到“已命中多少资料/正在查询”，这不是 MCagent 的思考过程。应取消这类抢跑预检索，改成显示 MCagent trace 或 SSE 事件。
2. 目前回答不是流式输出。长回答时用户不知道要等多久。需要新增 `/api/chat/stream` 或等价 SSE，让前端逐步显示 `observe/decide/retrieve/evidence/answer/delegate/done` 阶段；如果模型 API 本身暂时不能 token 流，也至少要阶段流式。
3. “状态”虽然现在能路由到 `crawler_status`，但状态内容不易读。应把当前任务、轮次、数据源、查询词、为什么在跑、入库状态、预计下一步拆成结构化中文，不只显示 `7/16 Modrinth API`。
4. MCagent 与 Crawler 在 UI 里共用同一个会话窗口，切换 Agent 后上下文和消息混在一起。需要前端明确：两个 Agent 可以共用项目记忆，但聊天会话应按 agent 隔离，或至少按 agent 展示独立消息流。
5. Crawler 直连时，用户说“帮 MCagent 获取资料”，应被识别为“用户直接委托 Crawler，交付目标是 MCagent/RAG”，不能显示成“MCagent 判断资料不足”。后端需要区分 `requested_by=user` 和 `requested_by=mcagent`，并在 collaboration/answer 中展示不同文案。
6. MCagent 追问如“有哪些BOSS”已经能用检索计划补全“落幕曲 BOSS”，但仍需持续测试，避免检索结果混入无关 Boss。证据筛选可以使用 plan，但不能用规则替代 MCagent 判断。

下一轮执行顺序：

1. 前端移除正式问答前的 `/api/search` 预览，改为“等待 MCagent 判断工具”。
2. 后端新增结构化 `tool_selected`、`status_payload`、`delegation_requested_by` 等字段，供前端显示真实过程。
3. 增加 SSE 阶段流接口；先做阶段事件流，后续再接模型 token 级流。
4. 修正 Crawler 直连身份和交付目标：用户直接找 Crawler 时，Crawler 自己判断清洗/保存/是否给 MCagent RAG 使用。
5. 前端会话按 agent 隔离，或切换 Agent 自动切到对应 agent 的当前会话。
6. 对测试矩阵逐项验证：状态、Crawler直连、MCagent派单、追问、资料不足、正常RAG。

## 16. 编码与乱码治理规范（2026-05-18）

本节是硬性工程规范，用来解决历史开发中反复出现的中文乱码问题。后续任何 Agent 或开发者改源码、prompt、前端、脚本、开发文档时都必须遵守。

原则：

- 项目维护文件统一使用 UTF-8，不允许用系统默认编码、GBK、cp936 或 PowerShell 默认输出覆盖文件。
- 修改文件必须用明确编码读取/写入；Python 使用 `encoding="utf-8"`，PowerShell 使用 `-Encoding UTF8`。
- 不要把终端显示乱码误判为文件乱码。先用 Python 按 UTF-8 读取文件确认实际内容。
- `data/`、`logs/`、`runtime/` 中包含外部网页、原始 HTML、历史运行结果和缓存，不参与项目源码乱码修复；这些内容只在需要清洗入库时单独处理。
- 源码、prompt、文档和前端文本中不能出现连续三个以上问号、mojibake 字符或 Unicode replacement character。正则、SQL 参数占位、URL 查询中的普通 `?` 可以存在，但不应出现在中文说明或用户可见文案里。
- 如果发现历史乱码，不要继续在末尾追加“干净中文”绕过问题；应直接修复源文件中的乱码段，并记录修复内容。

检查命令：

```powershell
cd D:\magic\MC_Agent
python scripts\check_text_encoding.py
python -m py_compile mcagent\web_server.py scripts\check_text_encoding.py
```

当前修复结果：

- 已清理 `mcagent\web_server.py` 中影响会话摘要、追问实体抽取、列表题判断、证据行抽取和答案元信息剥离的历史乱码。
- 已清理 `docs\crawler_runbook.md` 中第 11 章后的历史乱码段，并改写为可读的中文开发记录。
- 已新增 `scripts\check_text_encoding.py`，默认扫描 `mcagent/`、`scripts/`、`docs/`、项目根部维护文件和 `D:\magic\AgentConsole` 前端文件。
- 当前检查结果：维护范围内未发现乱码标记，后端相关文件编译通过。

## 17. Agent 首轮体验修复记录（2026-05-18）

本轮修复目标来自用户反馈：MCagent/Crawler 的行为必须像 Agent，而不是前端或后端脚本抢先替 Agent 判断。

已完成：

- 前端移除正式问答前的 `/api/search` 预检索。现在发送问题后只显示“MCagent 正在理解问题并选择工具”，真实检索、状态、派单都由 `/api/chat` 返回的 trace 决定。
- 前端会话按 Agent 隔离。`MCagent` 和 `Crawler` 各有自己的会话列表与当前会话；切换 Agent 不再继续显示另一个 Agent 的消息流。
- 保留“仅检索”为 MCagent 的运行模式，不作为第三个 Agent。
- Crawler 直连身份修正。用户切到 Crawler 后说“帮 MCagent 获取资料”，现在记录为 `requested_by=user`、`delivery_target=MCagent/RAG`，文案显示“用户直接委托 Crawler”，不再伪装成“MCagent 判断资料不足”。
- Crawler 派单 payload 增加 `requested_by` 与 `delivery_target`，供后续 Crawler LLM 判断清洗、保存和交付格式。
- `状态` 输出改为结构化中文，包含本地库规模、导出目录、当前后台任务、批量采集进度、最近文件和下一步建议。
- 清理前端历史乱码兼容标记，继续保持维护文件 UTF-8 检查通过。

验证：

- `状态`：trace 为 `observe/received -> decide/tool_selected(status)`，不再先触发 RAG 检索。
- Crawler 直连：`帮MCAgent获取落幕曲有哪些BOSS 这些BOSS如何打哪里打怎样打` 返回用户委托文案，delegation 为 `requested_by=user`、`delivery_target=MCagent/RAG`。
- `python scripts\check_text_encoding.py` 通过。
- `python -m py_compile mcagent\web_server.py` 通过。
- `node --check D:\magic\AgentConsole\static\app.js` 通过。

仍未完成：

- 还没有真正的 SSE 阶段流。当前只是去掉了假预检索，用户仍需等 `/api/chat` 完整返回后才能看到最终 trace。下一步应新增 `/api/chat/stream`，先实现阶段事件流，再考虑模型 token 级流。
- Crawler running 阶段的状态仍偏少；需要在 Crawler 规划完成后实时写入 job result 或 progress，让状态页显示当前 source、query、reason、交付对象和下一步。
- Crawler 对 `Boss` 这类宽词仍可能抓到 HUGO BOSS、Windows BOSS 等跑偏网页。下一步应让 Crawler LLM 做主题复判，工具只提供标题、URL、正文摘要和 Minecraft 上下文信号。
- Crawler 直连任务现在能标记调用者和交付对象，但后续 Crawler prompt/plan 还要显式利用这两个字段，决定是否按 MCagent RAG 格式清洗。

## 18. Crawler 规划卫生修复（2026-05-18）

本节修复用户指出的核心问题：Crawler 可以宽词搜索已确认组件，但不能把泛类词或交付对象误当成资料主题。

已完成：

- Crawler planner 增加通用“交付对象剥离”：`帮/给/为 MCagent/RAG/知识库 获取 X` 中，MCagent/RAG 是调用者或交付对象，不是采集主题。
- 增加通用“问题主题抽取”：`X 有哪些 Boss`、`X 的 Boss`、`X 怎么玩` 这类问题里，主题是 `X`，后面的 Boss/玩法/攻略是资料维度。
- 移除 `Boss` 作为 `ITEM_HINTS` 实体。`Boss` 是泛类，不是已确认组件；可以生成 `目标主题 + Boss + 清单/攻略/打法`，不能单独搜索 `Boss` 或 `Boss 攻略`。
- Crawler planner 加入计划卫生检查：拒绝单独的泛类查询，例如 `Boss`、`攻略`、`教程`、`系统`、`物品`；拒绝把 `MCagent/RAG/CrawlerAgent` 作为独立搜索主题。
- 保留用户要求的“组件宽词原则”：如果上下文已经确认 `TACZ`、`SlashBlade`、`亚波伦`、`塔罗牌` 等属于目标资料范围，可以单独搜组件；但 `Boss` 这类类别词不等于组件。

验证：

- 输入：`帮MCAgent获取落幕曲有哪些BOSS 这些BOSS如何打哪里打怎样打`
- 期望：主题是 `落幕曲`，交付对象是 `MCagent/RAG`，查询围绕 `落幕曲 Boss`、`落幕曲 Boss 清单`、`落幕曲 Boss 攻略`、`落幕曲 Boss 打法`、`亚波伦 Boss` 等。
- 验证结果：planner 输出 topic=`落幕曲`，delivery=`MCagent/RAG`，不再把 `MCAgent` 当主题，不再生成单独 `Boss` 查询。
- 额外注意：PowerShell here-string 在某些调用里会把中文显示成问号，测试中文 planner 时应使用 Unicode 转义或确认请求体实际 UTF-8。

## 19. SSE 阶段流与自动测试矩阵（2026-05-18）

本轮目标是让用户不再面对“处理中...”黑箱等待，而是看到真实 Agent 阶段事件。

已完成：

- 新增 `/api/chat/stream`，使用 `text/event-stream` 输出阶段事件。
- SSE 事件包含：
  - `trace`：`observe`、`decide`、`retrieve`、`answer`、`delegate`、`done` 等阶段。
  - `response`：最终完整回答、sources、collaboration、job、delegation 等。
  - `done`：流结束。
  - `error`：异常信息。
- 前端 `sendQuestion()` 改为优先调用 `/api/chat/stream`，收到 trace 就即时更新消息下方的过程条和顶部活动状态。
- 前端不再显示假的“已命中多少资料”；所有阶段都来自后端真实 trace。
- 增加 `answer_timeout_seconds`，默认 90 秒。模型生成超过上限时返回本地证据抽取兜底，避免用户一直等。
- 本地兜底回答过滤图片、Logo、站点标题、identicon 等噪声；新手/攻略类问题优先抽取证据句，不再乱列候选名称。
- 增加编码损坏保护：如果问题在传输或终端里变成大量问号，后端会提示重新发送，并且不会触发 Crawler，避免污染资料库。
- 新增 `scripts/smoke_agent_flows.py`，用于自动测试核心 Agent 流程。

Smoke 测试覆盖：

1. `状态`：应直接选择 `status` 工具，不走 RAG。
2. `落幕曲新手该怎么玩`：应产生 retrieve trace，并返回回答或本地兜底，不触发无意义补库。
3. Crawler 直连：`帮MCAgent获取落幕曲有哪些BOSS...` 应标记 `requested_by=user`、`delivery_target=MCagent/RAG`，并启动 Crawler 后立即停止测试任务。

当前验证结果：

- `python scripts\check_text_encoding.py` 通过。
- `python -m py_compile mcagent\web_server.py mcagent\crawler_llm_planner.py scripts\smoke_agent_flows.py` 通过。
- `node --check D:\magic\AgentConsole\static\app.js` 通过。
- `python scripts\smoke_agent_flows.py` 三项通过。

仍未完成/下一步：

- Crawler job 的“停止”仍是 stop 标记，不能立即中断正在规划或正在运行的子进程；需要实现真正的子进程取消和 planner 取消。
- 当前 SSE 是阶段流，不是模型 token 级流。下一步如果模型 API 支持 streaming，再把 `answer` 阶段升级为 token 流。
- RAG 回答若模型超时会使用本地兜底，兜底可读性已提升，但仍不如 LLM 组织语言。后续要继续优化模型超时、模型选择和 prompt 压缩。
- Crawler 规划任务启动后，前端状态仍要等 job result 更新才显示 source/query/reason；后续应在 Crawler planner 阶段更早写入 job.result。

## 20. 停止任务与超时兜底修复（2026-05-18）

已完成：

- `Job` 增加 `current_pid`，Crawler 执行具体抓取脚本时会记录当前子进程 PID。
- `/api/jobs/stop` 现在会设置 `stop_requested`，并尝试通过 `taskkill /T /F` 终止当前抓取子进程树。
- `_run_crawler_command()` 从 `subprocess.run()` 改为 `Popen + poll`，可在执行中检查 `job.stop_requested`，支持用户主动停止、超时终止和状态回写。
- `_running_job()` 不再把已经 `stop_requested` 的 running job 当作阻塞任务，避免“已停止但还在清理”的任务挡住新任务。
- Crawler job 在规划前、规划后检查 `stop_requested`，能在这些边界点尽快结束。
- Crawler planner 增加 35 秒超时包装。超时会转入规则兜底计划；如果用户在规划期间停止任务，会返回 `stopped_before_planner_finished`，job 进入 stopped。
- 回答生成增加 `answer_timeout_seconds`。超过上限时返回本地证据抽取兜底，不再让 UI 无限等。
- 本地兜底不会因为模型超时自动触发 Crawler；只有明确资料不足才补库。

验证：

- `python scripts\smoke_agent_flows.py` 已通过三项核心流程。
- Smoke 会启动 Crawler 直连任务并请求停止；停止标记能返回，子进程阶段可被终止。

仍未完成：

- Crawler LLM planner 已有超时/停止包装，但底层远程请求线程无法真正杀死，只是不再阻塞 job 状态和新任务。后续可进一步把 LLM client 改成支持请求级取消。
- 本地兜底回答已过滤图片/Logo噪声，但仍偏“证据摘录”，不是完整自然语言攻略。后续应继续优化 LLM 响应速度、模型选择和 prompt 压缩。

## 21. MCagent 流式体验与追问证据修复（2026-05-18）

本轮修复目标来自用户反馈：回答过程要像 Agent 在思考和行动，而不是前端假进度；追问必须继承当前会话主题；本地资料不足时要说清楚缺口，不能输出候选词垃圾。

已完成：

- 前端正式问答走 `/api/chat/stream`，收到后端 `trace` 就即时更新同一条消息文本，不再在消息下方堆调试标签。
- 新增 `retrieve:planning` 和 `decide:selecting_evidence` 阶段，用户能看到“理解问题、规划检索、检索、筛证据、组织回答”的自然文字变化。
- 前端默认 `answer_timeout_seconds` 调整为 20 秒，避免普通问答长时间黑箱等待；超时后走本地证据兜底。
- 检索规划 LLM 超时从 35 秒降到 8 秒。LLM 仍优先负责检索规划，但慢或失败时使用规则兜底计划，不阻塞主流程。
- MCagent 现在区分原始用户问题和上下文检索问题：原始问题用于判断用户意图，改写后的问题只用于检索补全。
- 追问中的父主题会进入证据约束。例如先问“落幕曲新手该怎么玩”，再问“有哪些BOSS / 这些BOSS怎么打”，证据会优先保留落幕曲相关来源，避免被其他整合包或普通 Boss 攻略压过。
- 列表型兜底和过程型兜底分离：
  - “有哪些 / 列表 / 清单”可以使用名称抽取兜底。
  - “怎么打 / 怎么做 / 掉落什么 / 哪里打”必须使用证据句或明确资料不足，不能返回“候选内容如下”的词表。
- Boss 过程证据增加通用噪声过滤：过滤悬赏令、词缀、存储、过长混合技巧段、loading 图片、Logo、站点图片等非打法证据。
- 资料不足文案改为面向用户解释，例如掉落问题会说“没有足够稳定的掉落/奖励证据”，再交给 Crawler 补齐。
- `scripts/check_text_encoding.py` 增加常见 mojibake 片段检测，并用 UTF-8/backslashreplace 输出，避免检查脚本本身因控制台编码失败。

验证矩阵：

- `状态`：直接选择 status 工具，不走 RAG。
- `进度怎么样`：直接选择 status 工具。
- `叫CrawlerAgent收集乌托邦完整资料`：MCagent 识别为派单，`requested_by=mcagent`。
- `让Crawler获取落幕曲Boss清单`：MCagent 识别为派单。
- Crawler 直连 `帮MCAgent获取落幕曲有哪些BOSS 这些BOSS如何打哪里打怎样打`：识别为用户直接委托 Crawler，`delivery_target=MCagent/RAG`。
- 连续上下文：`落幕曲新手该怎么玩 -> 有哪些BOSS -> 这些BOSS怎么打 -> 掉落什么` 全部通过自动测试；掉落证据不足时说明缺口并触发 Crawler。
- RAG 问答：`落幕曲里的塔罗牌是什么`、`拔刀剑怎么玩`、`梦想一心怎么做`、`乌托邦有哪些模组` 全部通过自动测试。

验证命令：

```powershell
cd D:\magic\MC_Agent
python -m py_compile mcagent\web_server.py mcagent\retrieval_planner.py scripts\smoke_agent_flows.py scripts\check_text_encoding.py
python scripts\check_text_encoding.py
node --check D:\magic\AgentConsole\static\app.js
python scripts\smoke_agent_flows.py
```

仍需继续优化：

- 当前 SSE 是阶段流，不是模型 token 级流。后续如果所选 LLM API 支持 streaming，再把 `answer` 阶段升级为 token 流。
- 本地证据兜底仍偏“证据摘录”，没有 LLM 正常回答自然；需要继续优化模型响应速度、上下文压缩和 prompt。
- 数据目录里仍存在 HUGO BOSS / Windows BOSS 等历史宽词污染文件。当前证据层会规避它们，但后续应由 Crawler LLM 复判后清理或隔离。
- Crawler running 阶段的 job progress 还不够细，需要在 planner 产出 source/query/reason 后立即写入 job.result，方便状态页实时显示。
- `梦想一心怎么做` 已能命中教程，但兜底仍可能只是摘录标题和证据行；后续要做通用“步骤型答案压缩器”，把同来源教程整理成 1、2、3 步。

2026-05-18 补充：用户反馈“落幕曲新手该怎么玩”最终回答仍显示“本地资料中找到以下相关证据”。已修复超时兜底的回答形态：新手/玩法/攻略类问题会组织为可读路线建议，不再把内部证据清单直接作为最终回复；同时过滤 legacy hosted search Query/source、下载站标题、loading 图片等元信息。常规 RAG 检索候选数改为自适应，不再固定 200；检索规划 LLM 默认只在复杂/完整资料/多实体配方等场景启用，普通问答使用快速兜底计划。

## 2026-05-24 MCagent Role Identity Fix

User feedback: MCagent should not behave like a generic keyword router. Its first reaction should come from its role identity: a Minecraft-focused knowledge agent. It should semantically decide whether the user is asking about Minecraft, modpacks, mods, items, bosses, gameplay, servers, versions, guides, MC reference sites, or the local Minecraft knowledge base.

Principles:

- This is role reasoning, not keyword-trigger routing. The LLM still owns semantic judgment, tool choice, and final wording.
- If MCagent judges the request as Minecraft-related, it should consider local RAG/evidence workflow first. It delegates to Crawler only when evidence is missing or collection is explicitly requested.
- If MCagent judges the request as not Minecraft-related, it may use direct_answer, chat normally, or explain its boundary. It should not force the request into RAG or Crawler.
- AgentMessage only delivers content to the target agent. The receiving agent then decides its next step from its own role and tool catalog.

Implemented:

- `agent_runtime.py` now describes MCagent as a Minecraft-focused knowledge agent.
- `agent_router.py` adds an MCagent self-check before tool selection: interpret as a Minecraft knowledge assistant first, then choose RAG, delegation, status, or direct answer.
- Tool catalog descriptions now make `local_rag_search` the local Minecraft knowledge base and `delegate_crawler` the Minecraft evidence-gap handoff.

## 2026-05-24 Crawler Research Method

User feedback: collection pressure was high because Crawler sometimes kept trying broad searches instead of doing deliberate research. The fix is not a topic-specific rule. It is a general research method the Crawler LLM must apply.

Method:

- Identify the target entity first: aliases, language variants, official names, version scope, and likely source ecosystem.
- Build a source graph before scaling: official/project pages, documentation, repositories, package indexes, download/file pages, dependency/relation pages, changelogs/releases, wiki pages, forum posts, video indexes, and community mirrors.
- Use broad discovery only to find candidate source nodes. Once a node is found, crawl exact URLs or source-specific pages directly.
- When a result is empty, duplicate, blocked, off-topic, or low-yield, switch source class or graph node instead of repeating similar generic searches.
- For MCagent/RAG delivery, persist markdown, manifest, source URL, metadata, raw text/raw HTML when available, and an explicit coverage/gap summary.

Implemented:

- `agent_runtime.py` now exposes this Crawler research method in the collection tool catalog.
- `crawler_llm_planner.py` teaches the planner and reflection loop to use source-graph replanning under collection pressure.
- Tests assert that the Crawler catalog and planner prompt contain the general source-graph method, without encoding a single-topic special case.
