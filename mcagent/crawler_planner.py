from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from .query_intent import analyze_query


@dataclass(slots=True)
class Toolset:
    id: str
    label: str
    purpose: str
    read_only: bool = True
    network: bool = True
    output: str = "Markdown + manifest.json"
    default_limit: int = 12
    timeout_seconds: int = 480


@dataclass(slots=True)
class PlannedTask:
    source: str
    query: str
    reason: str
    priority: int = 50
    search_limit: int | None = None
    max_urls: int | None = None
    params: dict[str, Any] = field(default_factory=dict)


TOOLSETS: dict[str, Toolset] = {
    "mediawiki": Toolset(
        id="mediawiki",
        label="Minecraft Wiki",
        purpose="原版 Minecraft 机制、物品、生物、维度、命令、合成等。",
        default_limit=12,
        timeout_seconds=420,
    ),
    "modrinth": Toolset(
        id="modrinth",
        label="Modrinth",
        purpose="模组、整合包、资源包、光影的公开项目元数据。",
        default_limit=60,
        timeout_seconds=600,
    ),
    "mcmod": Toolset(
        id="mcmod",
        label="MC百科",
        purpose="中文 MC 模组、整合包、教程、资料页；通过 MC百科搜索页发现结果并抓取正文。",
        default_limit=8,
        timeout_seconds=600,
    ),
    "createwiki": Toolset(
        id="createwiki",
        label="Create Wiki",
        purpose="机械动力 Create 的机械部件、动力、物流、火车、自动化教程。",
        default_limit=12,
        timeout_seconds=480,
    ),
    "ftbwiki": Toolset(
        id="ftbwiki",
        label="FTB Wiki",
        purpose="常见大型模组 Wiki，例如暮色森林 Boss、地标、魔法/科技模组机制。",
        default_limit=12,
        timeout_seconds=480,
    ),
    "followup": Toolset(
        id="followup",
        label="深度跟进文档",
        purpose="从已有 Modrinth 项目的 Source/Wiki/README 链接继续抓公开文档。",
        default_limit=40,
        timeout_seconds=900,
    ),
    "web_discovery": Toolset(
        id="web_discovery",
        label="公开搜索发现",
        purpose="当结构化 API 找不到资料时，用公开搜索发现候选页面，再抓取可读 Markdown。",
        default_limit=8,
        timeout_seconds=900,
    ),
    "tavily": Toolset(
        id="tavily",
        label="Tavily Search/Extract",
        purpose="通过 Tavily 搜索公开网页并提取 Markdown 正文，作为多源补库的高质量搜索/抽取渠道。",
        default_limit=8,
        timeout_seconds=900,
    ),
    "firecrawl": Toolset(
        id="firecrawl",
        label="Firecrawl Search/Scrape",
        purpose="通过 Firecrawl 云端或自建服务搜索、抓取和清洗网页正文；云端需要 FIRECRAWL_API_KEY，自建需要 FIRECRAWL_API_URL。",
        default_limit=8,
        timeout_seconds=900,
    ),
    "jina": Toolset(
        id="jina",
        label="Jina Reader/Search",
        purpose="无需 API key 的公开搜索与网页转 Markdown 兜底渠道。",
        default_limit=8,
        timeout_seconds=900,
    ),
    "playwright": Toolset(
        id="playwright",
        label="Playwright Browser Search/Extract",
        purpose="本地浏览器搜索、渲染网页并保存正文与原始 HTML。适合 API 额度不足、JS 页面、复杂表格、图片和下载页，不只是兜底。",
        default_limit=6,
        timeout_seconds=900,
    ),
    "browser_collect": Toolset(
        id="browser_collect",
        label="Browser Structured Collect",
        purpose="通用浏览器结构化采集：按用户目标提取列表、商品、表格等字段，并保存 CSV、JSON、report、raw HTML 和截图到指定目录；不绕过登录、验证码或安全验证。",
        default_limit=50,
        timeout_seconds=900,
    ),
    "save_artifact": Toolset(
        id="save_artifact",
        label="Save Artifact",
        purpose="Generic local persistence primitive: save agent-provided txt/md/json/jsonl/csv/html content to a file path or directory and write a manifest. It does not fetch or ingest content by itself.",
        default_limit=1,
        timeout_seconds=120,
    ),
    "modpack_download": Toolset(
        id="modpack_download",
        label="Modpack Archive Discovery/Download",
        purpose="发现并下载公开 .mrpack/.zip 整合包包体，保存到本地 manual_research，供整合包内部解析工具继续抽取 manifest、modlist、任务、脚本和配方；不绕过登录、付费、网盘或验证码。",
        default_limit=8,
        timeout_seconds=1200,
    ),
}


CONCEPTS: list[dict[str, Any]] = [
    {
        "aliases": ["机械动力", "机械力", "create", "create mod"],
        "canonical": "Create mod automation",
        "primary_source": "createwiki",
        "tasks": [
            ("createwiki", "Create mod automation", "Create 本体玩法总览、自动化与机械系统", 95),
            ("createwiki", "Create mechanical power", "补充动力、传动和机械组件", 90),
            ("createwiki", "Create trains", "补充火车和物流玩法", 70),
            ("modrinth", "Create mod addons", "补充 Create 附属模组和整合包生态", 55),
        ],
    },
    {
        "aliases": ["暮色", "暮色森林", "twilight forest"],
        "canonical": "Twilight Forest bosses",
        "primary_source": "ftbwiki",
        "tasks": [
            ("ftbwiki", "Twilight Forest bosses", "暮色森林 Boss、地标和战斗机制", 95),
            ("ftbwiki", "Twilight Forest landmarks", "暮色森林地标与探索路线", 85),
            ("modrinth", "Twilight Forest", "补充 Modrinth 上的暮色相关项目", 55),
            ("followup", "Twilight Forest", "跟进公开项目文档", 35),
        ],
    },
    {
        "aliases": ["蜜蜂世界", "蜜蜂维度", "蜂巢世界", "bumblezone"],
        "canonical": "The Bumblezone bee dimension",
        "primary_source": "modrinth",
        "tasks": [
            ("modrinth", "The Bumblezone bee dimension", "蜜蜂维度模组项目资料", 95),
            ("followup", "The Bumblezone", "跟进项目 Source/Wiki/README", 60),
        ],
    },
    {
        "aliases": ["应用能源", "ae2", "applied energistics"],
        "canonical": "Applied Energistics 2",
        "primary_source": "ftbwiki",
        "tasks": [
            ("ftbwiki", "Applied Energistics 2", "AE2 存储网络和频道机制", 90),
            ("modrinth", "Applied Energistics 2", "AE2 项目元数据和附属生态", 65),
        ],
    },
    {
        "aliases": ["通用机械", "mekanism"],
        "canonical": "Mekanism",
        "primary_source": "ftbwiki",
        "tasks": [
            ("ftbwiki", "Mekanism", "Mekanism 科技线和机器机制", 90),
            ("modrinth", "Mekanism", "Mekanism 项目元数据和附属生态", 65),
        ],
    },
    {
        "aliases": ["植物魔法", "botania"],
        "canonical": "Botania",
        "primary_source": "ftbwiki",
        "tasks": [
            ("ftbwiki", "Botania", "Botania 魔法系统和核心机制", 90),
            ("modrinth", "Botania", "Botania 项目元数据和附属生态", 65),
        ],
    },
]


VANILLA_HINTS = (
    "原版",
    "难度",
    "合成",
    "指令",
    "生物",
    "村民",
    "下界",
    "末地",
    "红石",
    "附魔",
    "酿造",
)


QUESTION_ACTION_HINTS = (
    "玩法",
    "怎么玩",
    "有什么",
    "有哪些",
    "攻略",
    "流程",
    "boss",
    "内容",
)


def toolsets_payload() -> list[dict[str, Any]]:
    return [asdict(item) for item in TOOLSETS.values()]


DESCRIPTIVE_TERMS = {
    "\u6839\u636e\u8d44\u6599",
    "\u8d44\u6599",
    "\u540c\u4e00\u7bc7\u6559\u7a0b",
    "\u6559\u7a0b",
    "\u8be6\u7ec6\u63cf\u8ff0",
    "\u5408\u6210\u987a\u5e8f",
    "\u5148\u5408\u6210",
    "\u518d\u5408",
    "\u6700\u540e\u5408",
    "\u8fd9\u4e9b\u540d\u79f0",
    "\u8fd9\u4e9b\u5251",
    "\u8fd9\u4e9b",
    "\u540d\u79f0",
    "\u51fa\u81ea",
    "\u5982\u4f55\u83b7\u53d6",
    "\u600e\u4e48\u83b7\u53d6",
    "\u83b7\u53d6",
    "\u600e\u4e48\u5408\u6210",
    "\u5982\u4f55\u5408\u6210",
    "\u5408\u6210",
    "\u914d\u65b9",
    "\u5236\u4f5c",
}

ITEM_HINTS = {
    "\u68a6\u60f3\u4e00\u5fc3",
    "\u5e7b\u9b54",
    "\u96ea\u9e26",
    "\u51bb\u6a31",
    "\u660e\u517d",
    "\u5929\u5143\u5200",
    "\u5929\u661f\u5200",
}

TOPIC_HINTS = {
    "\u843d\u5e55\u66f2": "\u843d\u5e55\u66f2",
    "closing song": "Closing Song",
    "\u62d4\u5200\u5251": "\u62d4\u5200\u5251",
    "slashblade": "SlashBlade",
}


def decompose_crawler_queries(question: str, intent: Any | None = None) -> dict[str, Any]:
    """Build short search queries for crawler providers.

    The crawler should not search a whole narrative answer. This function keeps
    the original question for traceability, but emits concise topic/item queries.
    """
    original_action = _action_terms(question)
    text = normalize_crawler_question(question)
    tokens = _meaningful_cjk_tokens(text)
    topics = _topic_terms(text, tokens, intent)
    items = _item_terms(text, tokens)
    if items and "\u62d4\u5200\u5251" not in topics:
        topics.append("\u62d4\u5200\u5251")
    action = original_action if original_action != "\u8d44\u6599" else _action_terms(text)
    base = " ".join(topics[:2]) if topics else ""
    if not base and intent is not None:
        base = str(getattr(intent, "entity", "") or "").strip()
    if len(base) > 40:
        base = ""

    queries: list[str] = []
    if items:
        for name in items:
            queries.append(" ".join(part for part in (name, base, action) if part).strip())
        if base:
            queries.append(" ".join(part for part in (base, " ".join(items[:4]), action) if part).strip())
    elif base:
        queries.append(" ".join(part for part in (base, action) if part).strip())
    else:
        queries.extend(tokens[:6])

    project_query = base or " ".join([*topics, *items][:4]) or text[:60]
    return {
        "original": question,
        "normalized": text,
        "topics": topics,
        "items": items,
        "action": action,
        "project_query": project_query,
        "queries": _dedupe_queries([query for query in queries if query])[:10],
    }


def normalize_crawler_question(question: str) -> str:
    text = re.sub(r"\s+", " ", question.strip())
    for term in sorted(DESCRIPTIVE_TERMS, key=len, reverse=True):
        text = text.replace(term, " ")
    text = re.sub(r"[，。；、,.!?！？:：()（）\[\]【】]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _meaningful_cjk_tokens(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,8}", text)
    output: list[str] = []
    for token in raw:
        if token in DESCRIPTIVE_TERMS:
            continue
        if len(token) > 8 and re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for known in [*ITEM_HINTS, *TOPIC_HINTS]:
                if known in token:
                    output.append(known)
            continue
        output.append(token)
    return _dedupe_queries(output)


def _topic_terms(text: str, tokens: list[str], intent: Any | None) -> list[str]:
    topics: list[str] = []
    lowered = text.lower()
    for key, value in TOPIC_HINTS.items():
        if key.lower() in lowered:
            topics.append(value)
    if intent is not None:
        entity = str(getattr(intent, "entity", "") or "")
        if 2 <= len(entity) <= 16 and not any(term in entity for term in DESCRIPTIVE_TERMS):
            topics.insert(0, entity)
    for token in tokens:
        if token in ITEM_HINTS:
            continue
        if token in {"\u843d\u5e55\u66f2", "\u62d4\u5200\u5251", "SlashBlade"}:
            topics.append(token)
    return _dedupe_queries(topics)[:4]


def _item_terms(text: str, tokens: list[str]) -> list[str]:
    items = [name for name in ITEM_HINTS if name in text]
    for token in tokens:
        if token in ITEM_HINTS:
            items.append(token)
        elif token.endswith(("\u5200", "\u5251")) and 2 <= len(token) <= 6:
            items.append(token)
    return _dedupe_queries(items)


def _action_terms(text: str) -> str:
    if any(term in text for term in ("\u5408\u6210", "\u914d\u65b9", "\u5236\u4f5c", "\u83b7\u53d6")):
        return "\u5408\u6210 \u914d\u65b9 \u83b7\u53d6"
    if any(term in text for term in ("\u73a9\u6cd5", "\u653b\u7565", "\u6d41\u7a0b")):
        return "\u73a9\u6cd5 \u653b\u7565"
    return "\u8d44\u6599"


def _dedupe_queries(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", str(value).strip())
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def task_key(source: str, query: str) -> str:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", f"{source} {query}".lower())
    return " ".join(tokens)


def find_concept(question: str) -> dict[str, Any] | None:
    return analyze_query(question, CONCEPTS).concept


def likely_project_query(question: str) -> bool:
    return analyze_query(question, CONCEPTS).domain in {"known_mod", "project"}


def project_search_query(question: str) -> str:
    intent = analyze_query(question, CONCEPTS)
    return intent.entity or question


def _manifest_completed(source_dir: Path, source: str, query: str) -> bool:
    if not source_dir.exists():
        return False
    wanted = task_key(source, query)
    source_marker = {
        "mediawiki": "mediawiki",
        "modrinth": "modrinth_agent",
        "mcmod": "mcmod",
        "createwiki": "createwiki",
        "ftbwiki": "ftbwiki",
        "followup": "followup",
    }.get(source, source)
    manifests = sorted(source_dir.rglob("manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in manifests[:250]:
        if source_marker not in str(path).lower():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        manifest_query = str(data.get("query") or "")
        if task_key(source, manifest_query) == wanted and (data.get("records") or data.get("skipped")):
            return True
    return False


def plan_crawler_tasks(
    question: str,
    source_dir: Path,
    *,
    max_tasks: int = 4,
    include_completed: bool = False,
) -> dict[str, Any]:
    intent = analyze_query(question, CONCEPTS)
    decomposed = decompose_crawler_queries(question, intent)
    concept = intent.concept
    tasks: list[PlannedTask] = []
    completed_skipped = 0
    if concept:
        for source, query, reason, priority in concept["tasks"]:
            if not include_completed and _manifest_completed(source_dir, source, query):
                completed_skipped += 1
                continue
            tasks.append(PlannedTask(source=source, query=query, reason=reason, priority=priority))
    else:
        if intent.domain == "project":
            query = decomposed["project_query"] or (intent.search_queries[0] if intent.search_queries else intent.entity or question)
            if intent.question_type == "recipe":
                recipe_queries = _recipe_queries(question, intent)
                recipe_queries = _dedupe_queries([*decomposed["queries"], *recipe_queries])
                for index, recipe_query in enumerate(recipe_queries[:6]):
                    tasks.append(
                        PlannedTask(
                            source="mcmod",
                            query=recipe_query,
                            reason="配方/合成类问题优先用 MC百科搜索具体物品页、资料页、表格和图片证据。",
                            priority=96 - index,
                            search_limit=10,
                        )
                    )
                if recipe_queries:
                    tasks.append(
                        PlannedTask(
                            source="web_discovery",
                            query=recipe_queries[0],
                            reason="若 MC百科未给出表格或图片，用公开搜索发现合成表、教程图或资料页。",
                            priority=72,
                            max_urls=10,
                        )
                    )
                    tasks.append(
                        PlannedTask(
                            source="tavily",
                            query=recipe_queries[0],
                            reason="Tavily 可直接搜索并抽取正文 Markdown，补充 MC百科以外的配方、教程和图片线索。",
                            priority=74,
                            search_limit=8,
                            max_urls=8,
                            params={"search_depth": "advanced"},
                        )
                    )
            tasks.append(PlannedTask(source="mcmod", query=query, reason="中文 MC 项目/教程优先查 MC百科，覆盖 Modrinth 缺少的中文资料和教程页。", priority=90, search_limit=8))
            tasks.append(PlannedTask(source="modrinth", query=query, reason="问题像 MC 生态项目，需要先查 Modrinth 项目元数据。", priority=80))
            tasks.append(PlannedTask(source="followup", query=query, reason="若项目元数据不足，跟进公开文档。", priority=45, max_urls=40))
            tasks.append(PlannedTask(source="tavily", query=query, reason="使用 Tavily 搜索并提取公开网页正文，补充项目文档和教程资料。", priority=55, search_limit=8, max_urls=8, params={"search_depth": "advanced"}))
            tasks.append(PlannedTask(source="firecrawl", query=query, reason="使用 Firecrawl 搜索/抓取网页正文，补充高质量 Markdown 证据。", priority=54, search_limit=8, max_urls=8))
            tasks.append(PlannedTask(source="jina", query=query, reason="使用 Jina Reader/Search 免费兜底搜索和抽取网页正文。", priority=48, search_limit=8, max_urls=8))
            tasks.append(PlannedTask(source="playwright", query=query, reason="用 Playwright 浏览器采集复杂页面、下载页或需要 raw HTML 的页面。", priority=74, search_limit=8, max_urls=6))
            tasks.append(PlannedTask(source="web_discovery", query=query, reason="若结构化 API 和项目链接仍无资料，使用公开搜索发现候选资料源。", priority=35, max_urls=8))
        elif intent.domain == "vanilla":
            tasks.append(PlannedTask(source="mediawiki", query=question, reason="问题像原版 Minecraft 机制，使用 Minecraft Wiki。", priority=80))
        else:
            query = decomposed["project_query"] or (intent.search_queries[0] if intent.search_queries else question)
            tasks.append(PlannedTask(source="mcmod", query=query, reason="主题不明确时先用 MC百科中文搜索发现候选模组、整合包或教程资料。", priority=65, search_limit=8))
            tasks.append(PlannedTask(source="modrinth", query=query, reason="未知 MC 主题，先用 Modrinth 做项目发现。", priority=60))
            tasks.append(PlannedTask(source="mediawiki", query=question, reason="同时检查原版 Wiki 是否有同名机制。", priority=50))
            tasks.append(PlannedTask(source="tavily", query=query, reason="主题不明确时用 Tavily 做公开网页搜索和正文抽取。", priority=45, search_limit=8, max_urls=8, params={"search_depth": "advanced"}))
            tasks.append(PlannedTask(source="jina", query=query, reason="主题不明确时用 Jina Reader/Search 做免费搜索抽取兜底。", priority=42, search_limit=8, max_urls=8))
            tasks.append(PlannedTask(source="playwright", query=query, reason="用 Playwright 浏览器渲染并保存候选页面正文和 raw HTML。", priority=72, search_limit=8, max_urls=6))
            tasks.append(PlannedTask(source="web_discovery", query=query, reason="项目发现不明确时，用公开搜索兜底寻找候选资料源。", priority=35, max_urls=6))

    tasks.sort(key=lambda item: item.priority, reverse=True)
    trimmed = tasks[: max(1, max_tasks)]
    return {
        "question": question,
        "intent": intent.to_dict(),
        "decomposed_queries": decomposed,
        "concept": concept["canonical"] if concept else "",
        "strategy": "planner_executor",
        "toolsets": toolsets_payload(),
        "tasks": [asdict(item) for item in trimmed],
        "skipped_completed": completed_skipped,
        "truncated_tasks": max(0, len(tasks) - len(trimmed)),
    }


def _recipe_queries(question: str, intent: Any) -> list[str]:
    raw_terms = [str(intent.entity or ""), *[str(item) for item in intent.keywords], *[str(item) for item in intent.search_queries]]
    names: list[str] = []
    stop = {
        "这些",
        "这些拔刀剑",
        "拔刀剑",
        "落幕曲拔刀剑",
        "如何合成",
        "怎么合成",
        "合成",
        "配方",
        "制作",
        "Minecraft",
        "MC",
    }
    for raw in raw_terms:
        for term in re.findall(r"[\u4e00-\u9fff]{2,10}|[A-Za-z][A-Za-z0-9_+-]{2,}", raw):
            if term in stop or term.lower() in {item.lower() for item in names}:
                continue
            if any(noise in term for noise in ("整合包", "根据", "资料", "名称", "这些", "本地")):
                continue
            if term.endswith(("刀", "剑", "刃", "樱", "鸦", "兽", "心", "魔")) or term in {"落幕曲", "SlashBlade"}:
                names.append(term)
    base = " ".join(term for term in ("落幕曲" if "落幕曲" in question else "", "拔刀剑") if term)
    queries: list[str] = []
    for name in names:
        if name == "落幕曲":
            continue
        queries.append(" ".join(part for part in (name, base, "合成 配方 MC百科") if part))
    queries.append(" ".join(part for part in (base, "合成表 配方 图片") if part))
    return list(dict.fromkeys(query for query in queries if query.strip()))
