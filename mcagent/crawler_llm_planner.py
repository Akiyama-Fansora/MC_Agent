from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import OllamaConfig, load_config
from .crawler_planner import decompose_crawler_queries, plan_crawler_tasks
from .agent_memory import read_memory_events
from .llm import OllamaOpenAIClient, OpenAICompatibleClient


AGENTTEST_LLM_ENV = Path(r"D:\magic\AgentTest\config\llm.env")
ALLOWED_SOURCES = {"topic_discovery", "modpack_internal", "modpack_download", "mcmod", "modrinth", "followup", "tavily", "firecrawl", "jina", "web_discovery", "playwright", "browser_collect", "mediawiki", "ftbwiki", "createwiki"}

SOURCE_DEFAULTS: dict[str, dict[str, Any]] = {
    "modpack_internal": {"priority": 145},
    "topic_discovery": {"priority": 130, "max_files": 120, "max_queries": 40},
    "mcmod": {"priority": 100, "search_limit": 10, "max_urls": 8},
    "modrinth": {"priority": 88},
    "followup": {"priority": 76, "max_urls": 16},
    "tavily": {"priority": 72, "search_limit": 8, "max_urls": 8, "search_depth": "advanced"},
    "firecrawl": {"priority": 70, "search_limit": 8, "max_urls": 8},
    "jina": {"priority": 66, "search_limit": 8, "max_urls": 8},
    "web_discovery": {"priority": 62, "search_limit": 8, "max_urls": 8},
    "playwright": {"priority": 82, "search_limit": 8, "max_urls": 6},
    "browser_collect": {"priority": 120, "max_items": 50},
    "modpack_download": {"priority": 130, "search_limit": 8},
    "ftbwiki": {"priority": 80, "search_limit": 8},
    "createwiki": {"priority": 80, "search_limit": 8},
    "mediawiki": {"priority": 50, "search_limit": 8},
}

AGENT_WORDS = {"MCagent", "MCAgent", "Crawler", "CrawlerAgent", "RAG", "LLM", "Agent"}
ITEM_HINTS = {
    "梦想一心",
    "幻魔",
    "雪鸦",
    "冻樱",
    "明兽",
    "天元刀",
    "天星刀",
    "至纯之血",
    "嬗变台",
    "拔刀剑",
    "TACZ",
}
GOAL_QUERY_HINTS = {
    "新手路线": ["落幕曲 新手路线", "FTB任务", "开局 攻略"],
    "FTB任务": ["FTB任务", "FTB Quests", "任务系统"],
    "拔刀剑": ["拔刀剑 获取步骤", "拔刀剑 合成 配方", "SlashBlade"],
    "梦想一心": ["梦想一心 获取步骤", "梦想一心 MC百科"],
    "至纯之血": ["至纯之血 获取", "至纯之血"],
    "嬗变台": ["嬗变台 原理", "嬗变台 教程"],
    "Boss": [],
    "TACZ": ["TACZ 枪械", "TACZ", "Timeless and Classics Zero"],
    "特殊系统": ["塔罗牌 诅咒饰品 黑魔法 女仆", "塔罗牌", "诅咒饰品", "黑魔法", "女仆"],
}


def _collection_target_hint(question: str) -> str:
    text = re.sub(r"\s+", " ", question.strip())
    quoted_patterns = [
        r"(?:整合包|modpack)[「《\"']([^」》\"']{2,60})[」》\"']",
        r"[「《\"']([^」》\"']{2,60})[」》\"'](?:的)?(?:完整数据|完整资料|资料|数据|内容|知识库|整合包|modpack)",
    ]
    for pattern in quoted_patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            target = re.sub(r"\s+", " ", match.group(1)).strip(" ：:，,。；;")
            if 2 <= len(target) <= 60 and not _looks_like_agent_target(target):
                return target
    package_patterns = [
        r"([\u4e00-\u9fffA-Za-z0-9_ （）()+-]{2,60}?)(?:整合包|modpack)(?:的)?(?:完整数据|完整资料|资料|数据|内容|知识库)",
        r"(?:补齐|补充|采集|收集|获取|整理|检查)([\u4e00-\u9fffA-Za-z0-9_ （）()+-]{2,60}?)(?:整合包|modpack)",
    ]
    for pattern in package_patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        target = match.group(1)
        target = re.split(r"[,，。；;：:]|流程手册|采集流程|教学复跑|请回忆|重新检查", target, maxsplit=1)[-1]
        target = re.sub(r"^(?:(?:一下|这个|那个|关于|有关|请|帮我|你去|去|把|将|对|并|重新|开始|复跑|检查|补齐|补充|采集|收集|获取|整理|教学复跑|请回忆)\s*)+", "", target.strip())
        target = re.sub(r"\s+", " ", target).strip(" ：:，,。；;")
        if 2 <= len(target) <= 40 and not _looks_like_agent_target(target):
            return target
    patterns = [
        r"(?:获取|采集|爬取|补充|补齐|建立|做|整理)(.+?)(?:的)?(?:完整数据|完整资料|资料包|数据包|本地资料|知识库|相关资料)",
        r"(?:我要|需要)(.+?)(?:的)?(?:完整数据|完整资料|资料包|数据包|本地资料|知识库|相关资料)",
        r"(.+?)(?:的)?(?:完整数据|完整资料|资料包|数据包)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        target = match.group(1)
        target = re.split(r"[,，。；;]|保存到|让\s*MCagent|给\s*MCagent|供\s*MCagent|用于\s*RAG|使\s*RAG", target, maxsplit=1)[0]
        target = re.sub(r"^(?:(?:一下|这个|那个|关于|有关|请|帮我|你去|去|把|将|对|并|重新|开始|复跑|检查|补齐|补充|采集|收集|获取|整理)\s*)+", "", target.strip())
        target = re.sub(r"\s+", " ", target).strip(" ：:，,。；;")
        if 2 <= len(target) <= 40 and not _looks_like_agent_target(target):
            return target
    return ""


def _session_target_hint(session_summary: dict[str, Any] | None = None) -> str:
    if not isinstance(session_summary, dict):
        return ""
    for key in ("current_topic", "target", "goal", "collection_target"):
        value = str(session_summary.get(key) or "").strip()
        if not value:
            continue
        value = _strip_delivery_recipient(value)
        value = re.sub(r"\s+", " ", value).strip(" ：:，,。；;")
        if 2 <= len(value) <= 80 and not _looks_like_agent_target(value):
            return value
    return ""


def _crawler_memory_digest(limit: int = 8) -> dict[str, Any]:
    events = read_memory_events(limit=120)
    lessons: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "crawler_lesson":
            lessons.append(
                {
                    "title": str(event.get("title") or "")[:120],
                    "topic": str(event.get("topic") or "")[:80],
                    "lesson": str(event.get("lesson") or "")[:900],
                    "playbook_path": str(event.get("playbook_path") or "")[:240],
                    "success_pattern": [str(item)[:160] for item in list(event.get("success_pattern") or [])[:6]],
                }
            )
        elif event_type in {"crawler_plan_completed", "crawler_gap_delegated", "crawler_background_ingest_completed"}:
            summary = event.get("summary") if isinstance(event.get("summary"), dict) else {}
            totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
            recent.append(
                {
                    "type": event_type,
                    "question": str(event.get("question") or "")[:180],
                    "success_count": event.get("success_count"),
                    "failure_count": event.get("failure_count"),
                    "totals": {key: totals.get(key) for key in ("records", "empty_tasks", "off_topic_tasks", "duplicate_skipped")},
                    "next_actions": [str(item)[:180] for item in list(summary.get("next_actions") or [])[:4]],
                }
            )
    return {
        "lessons": lessons[-4:],
        "recent_events": recent[-limit:],
    }


def _question_subject_hint(question: str) -> str:
    text = _strip_delivery_recipient(question)
    text = re.sub(r"\s+", " ", text.strip())
    patterns = [
        r"^(?P<subject>[\u4e00-\u9fffA-Za-z0-9_ ()（）+-]{2,40})(?:有哪些|有什么|有多少|包含哪些|包括哪些)(?:Boss|BOSS|boss|物品|模组|系统|玩法|配方|教程|资料|内容)",
        r"^(?P<subject>[\u4e00-\u9fffA-Za-z0-9_ ()（）+-]{2,40})(?:的|中|里|里面|中的|里的)(?:Boss|BOSS|boss|物品|模组|系统|玩法|配方|教程|资料|内容)",
        r"^(?P<subject>[\u4e00-\u9fffA-Za-z0-9_ ()（）+-]{2,40})(?:新手|前期|后期|开局|怎么|如何|怎样|玩法|攻略)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        subject = match.group("subject").strip(" 的中里里面：:，,。；;？?！!").strip()
        subject = re.sub(r"^(?:请|帮我|帮忙|获取|采集|收集|整理|补充)\s*", "", subject)
        if 2 <= len(subject) <= 40 and not _looks_like_agent_target(subject):
            return subject
    return ""


def _looks_like_agent_target(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text)
    if not stripped:
        return True
    lowered = stripped.lower()
    agent_hits = sum(1 for word in AGENT_WORDS if word.lower() in lowered)
    return agent_hits > 0 and len(stripped) <= 16


def _target_core_terms(target: str) -> list[str]:
    terms = [target.strip()]
    cleaned = re.sub(r"(整合包|模组|MOD|modpack|mod|资料|数据)$", "", target.strip(), flags=re.I).strip()
    if cleaned and cleaned != target:
        terms.append(cleaned)
    return [term for term in dict.fromkeys(terms) if term]


def _target_queries(target: str) -> list[str]:
    if not target:
        return []
    base_terms = _target_core_terms(target)
    queries: list[str] = []
    for base in base_terms[:2]:
        queries.extend(
            [
                base,
                f"{base} 整合包",
                f"{base} MC百科",
                f"{base} 模组列表",
                f"{base} 新手攻略",
                f"{base} 玩法 教程",
                f"{base} 下载 CurseForge Modrinth",
            ]
        )
    return list(dict.fromkeys(query for query in queries if 2 <= len(query) <= 80))


def _planner_context_text(question: str, session_summary: dict[str, Any] | None = None) -> str:
    parts = [question]
    if isinstance(session_summary, dict):
        for key in (
            "handoff_brief",
            "known_context",
            "current_topic",
            "missing_evidence",
            "mcagent_gap_summary",
            "delivery_target",
            "goal",
            "last_result",
            "next_actions",
        ):
            value = session_summary.get(key)
            if value:
                parts.append(str(value))
        gaps = session_summary.get("gaps")
        if isinstance(gaps, list):
            parts.extend(str(item) for item in gaps if str(item).strip())
        goals = session_summary.get("coverage_goals")
        if isinstance(goals, list):
            parts.extend(str(item) for item in goals if str(item).strip())
        components = session_summary.get("known_components")
        if isinstance(components, list):
            parts.extend(str(item) for item in components if str(item).strip())
    return "\n".join(parts)


def _strip_delivery_recipient(text: str) -> str:
    value = str(text).strip()
    value = re.sub(
        r"^\s*(?:请)?\s*(?:帮|给|为)\s*(?:MCagent|MCAgent|MC Agent|RAG|本地资料库|知识库)\s*(?:去|来)?\s*(?:收集|采集|获取|抓取|爬取|补充|补库|更新资料)\s*",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s*(?:给MCagent用|给 MCagent 用|用于RAG|给RAG用|入库)\s*$", "", value, flags=re.I)
    return value.strip(" \t\r\n，,。；;：:？?！!") or text


def _coverage_queries(question: str, session_summary: dict[str, Any] | None = None, target: str = "") -> list[str]:
    question = _strip_delivery_recipient(question)
    text = _planner_context_text(question, session_summary)
    subject = target or _collection_target_hint(question) or _question_subject_hint(question)
    queries: list[str] = []
    queries.extend(_known_components(session_summary))
    for hint, hint_queries in GOAL_QUERY_HINTS.items():
        if hint.lower() in text.lower():
            queries.extend(hint_queries)
    for item in ITEM_HINTS:
        if item.lower() in text.lower():
            queries.append(item)
    if subject and re.search(r"\bBoss\b|BOSS|boss|有哪些BOSS|有哪些 Boss|Boss", text, flags=re.I):
        queries.extend([f"{subject} Boss", f"{subject} Boss 清单", f"{subject} Boss 攻略", f"{subject} Boss 打法"])
    return [query for query in dict.fromkeys(queries) if _valid_coverage_query(query, target, text)]


GENERIC_COLLECTION_TERMS = {
    "boss",
    "boss攻略",
    "boss列表",
    "攻略",
    "教程",
    "玩法",
    "获取",
    "合成",
    "配方",
    "物品",
    "模组",
    "整合包",
    "系统",
}


def _valid_coverage_query(query: str, target: str = "", context_text: str = "") -> bool:
    value = re.sub(r"\s+", " ", str(query).strip())
    if not (2 <= len(value) <= 80):
        return False
    compact = re.sub(r"\s+", "", value).lower()
    if compact in GENERIC_COLLECTION_TERMS:
        return False
    if compact in {"boss", "bosses"}:
        return False
    if re.fullmatch(r"boss\s*(攻略|列表|清单|打法)?", value, flags=re.I):
        return False
    if target and value in {"攻略", "教程", "玩法", "Boss", "BOSS", "boss"}:
        return False
    return True


def _known_components(session_summary: dict[str, Any] | None = None) -> list[str]:
    if not isinstance(session_summary, dict):
        return []
    raw = session_summary.get("known_components")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()][:40]


def _fallback_plan_with_target(question: str, source_dir: Path, max_tasks: int, planner_error: str = "", session_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    question = _strip_delivery_recipient(question)
    target = _session_target_hint(session_summary) or _collection_target_hint(question) or _question_subject_hint(question)
    coverage_only_queries = _coverage_queries(question, session_summary, target)
    context_text = _planner_context_text(question, session_summary)
    explicit_delivery_target = ""
    requested_by = ""
    handoff_from = ""
    if isinstance(session_summary, dict):
        explicit_delivery_target = str(session_summary.get("delivery_target") or "").strip()
        requested_by = str(session_summary.get("requested_by") or "").strip()
        handoff_from = str(session_summary.get("handoff_from") or "").strip()
    if explicit_delivery_target:
        delivery_target = explicit_delivery_target
    elif any(term.lower() in context_text.lower() for term in ("mcagent", "rag", "知识库", "入库", "索引", "切分")):
        delivery_target = "MCagent/RAG"
    else:
        delivery_target = "unknown"
    if isinstance(session_summary, dict):
        output_dir = str(session_summary.get("output_dir") or "").strip()
        fields = session_summary.get("schema_fields") or session_summary.get("fields")
        max_items = session_summary.get("max_items") or 50
        structured_goal = bool(output_dir or fields) or any(
            term in context_text.lower()
            for term in ("csv", "json", "table", "structured", "price", "link", "商品", "价格", "链接", "表格", "字段", "保存到")
        )
        if structured_goal:
            structured_target = target or str(session_summary.get("collection_target") or question)[:80]
            task = _normalize_task(
                {
                    "source": "browser_collect",
                    "query": structured_target,
                    "reason": "fallback structured browser collection after Crawler LLM planning failed",
                    "priority": 120,
                    "max_items": max_items,
                    "output_dir": output_dir,
                    "fields": fields,
                },
                "fallback structured browser collection",
                120,
            )
            return {
                "question": question,
                "strategy": "structured_browser_fallback_after_llm_planner_error",
                "planner_error": planner_error,
                "topic": structured_target,
                "target_hint": target,
                "package_type": "unknown",
                "delivery_target": delivery_target,
                "cleaning_policy": "Structured browser output with CSV, JSON, report, raw HTML, screenshot, and manifest.",
                "requested_by": requested_by,
                "handoff_from": handoff_from,
                "coverage_goals": ["按用户要求采集结构化字段", "保存到指定目录", "遇到登录或验证时保存证据并说明限制"],
                "known_components": _known_components(session_summary),
                "success_criteria": ["保存 CSV/JSON/report", "保留来源 URL、raw HTML 和截图", "不绕过登录、验证码或安全验证"],
                "subqueries": [structured_target],
                "sources": ["browser_collect"],
                "reason": "Crawler LLM planner timed out or failed, so Crawler preserved the structured collection goal and used the browser_collect tool.",
                "tasks": [task] if task else [],
            }
    if requested_by == "user_via_mcagent":
        plan_reason = "Crawler LLM planner timed out or failed, so Crawler used a conservative target-aware plan while preserving the identity chain: human user -> MCagent handoff -> CrawlerAgent."
    elif requested_by == "mcagent":
        plan_reason = "Crawler LLM planner timed out or failed, so Crawler used a conservative target-aware plan for an MCagent evidence-gap delegation."
    elif requested_by == "user":
        plan_reason = "Crawler LLM planner timed out or failed, so Crawler used a conservative target-aware plan for a direct human Crawler task."
    else:
        plan_reason = "Crawler LLM planner timed out or failed, so Crawler used a conservative target-aware plan."
    prefer_external = any(term in context_text.lower() for term in ("mc百科重复", "mcmod duplicate", "重复页", "重复多", "换源", "外部", "非 mc百科", "non-duplicate", "external sources"))
    if not target:
        if "落幕曲" in context_text or "Closing Song" in context_text:
            target = "落幕曲 Closing Song"
        elif "乌托邦" in context_text or "Utopia" in context_text:
            target = "乌托邦 Utopia"
        elif coverage_only_queries:
            target = str(coverage_only_queries[0]).split()[0]
        else:
            fallback = plan_crawler_tasks(question, source_dir, max_tasks=max_tasks, include_completed=True)
            if planner_error:
                fallback["planner_error"] = planner_error
            return fallback
    queries = [*coverage_only_queries, *_target_queries(target)]
    queries = list(dict.fromkeys(query for query in queries if query))
    if prefer_external:
        sources = ["tavily", "firecrawl", "jina", "web_discovery", "playwright", "modpack_download", "followup", "mcmod", "modrinth"]
    else:
        sources = ["mcmod", "modrinth", "modpack_download", "tavily", "firecrawl", "jina", "web_discovery", "followup", "playwright"]
    if any(term in _planner_context_text(question, session_summary) for term in ("完整", "完整资料", "完整数据", "全量", "发现", "未知主题")):
        sources.insert(0, "topic_discovery")
    if any(term.lower() in _planner_context_text(question, session_summary).lower() for term in ("安装包", "内部文件", "ftb", "kubejs", "openloader", "modlist", "manifest", "整合包完整")):
        sources.insert(0, "modpack_internal")
    tasks: list[dict[str, Any]] = []
    for source in sources:
        if source in {"modrinth", "followup", "modpack_internal", "modpack_download"}:
            source_queries = [target]
        elif source == "playwright":
            source_queries = queries[:2]
        elif source == "mcmod":
            if prefer_external:
                source_queries = [target, *queries[:2]]
            else:
                source_queries = queries[:12]
        elif prefer_external and source in {"tavily", "firecrawl", "jina", "web_discovery"}:
            source_queries = queries[:5]
        else:
            source_queries = queries[:12]
        for offset, query in enumerate(source_queries):
            defaults = SOURCE_DEFAULTS.get(source, {})
            if prefer_external:
                priority_base = {
                    "tavily": 120,
                    "firecrawl": 116,
                    "jina": 112,
                    "web_discovery": 108,
                    "playwright": 104,
                    "followup": 98,
                    "mcmod": 88,
                    "modrinth": 82,
                    "topic_discovery": 125,
                }.get(source, int(defaults.get("priority") or 50))
            else:
                priority_base = int(defaults.get("priority") or 50)
            if source == "topic_discovery":
                source_queries = [target]
            task = _normalize_task(
                {
                    "source": source,
                    "query": query,
                    "reason": "fallback target plan after Crawler LLM planning failed",
                    "priority": priority_base - offset,
                },
                "fallback target plan",
                50,
            )
            if task:
                tasks.append(task)
    tasks.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    is_modpack = bool(re.search(r"整合包|modpack", context_text, flags=re.I))
    return {
        "question": question,
        "strategy": "target_fallback_after_llm_planner_error" if planner_error else "target_fallback",
        "planner_error": planner_error,
        "topic": target,
        "target_hint": target,
        "package_type": "modpack" if is_modpack else "unknown",
        "delivery_target": delivery_target,
        "cleaning_policy": "RAG-oriented markdown chunks with source URL, title, metadata, raw_html path, and dedupe fingerprint.",
        "requested_by": requested_by,
        "handoff_from": handoff_from,
        "coverage_goals": [
            "基本信息、别名和简介",
            "官方链接、下载页和社区链接",
            "整合包模组列表或依赖列表",
            "新手路线、核心玩法和教程",
            "关键系统、物品、配方、Boss、已知问题",
        ],
        "known_components": _known_components(session_summary),
        "success_criteria": [
            "保存 Markdown、manifest 和来源 URL",
            "支持 raw_html 的抓取器保存原始 HTML",
            "资料切分后能被 MCagent RAG 引用和回答",
        ],
        "subqueries": queries,
        "sources": sources,
        "reason": plan_reason,
        "tasks": _select_diverse_tasks(tasks, max(1, max_tasks)),
    }


def _read_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key.strip().lstrip("\ufeff")] = value.strip().strip('"').strip("'")
    return data


def _planner_client() -> tuple[OpenAICompatibleClient, str]:
    config = load_config()
    env = _read_env_file(AGENTTEST_LLM_ENV)
    if env.get("LLM_API_KEY"):
        model = env.get("LLM_MODEL_ID", "deepseek-v4-pro")
        endpoint_config = OllamaConfig(
            base_url=env.get("LLM_BASE_URL", "https://api.deepseek.com"),
            model=model,
            temperature=0.0,
            timeout_seconds=90,
        )
        return OpenAICompatibleClient(endpoint_config, api_key=env.get("LLM_API_KEY", ""), provider_label="CrawlerPlanner"), f"DeepSeek {model}"
    endpoint_config = OllamaConfig(
        base_url=config.ollama.base_url,
        model=config.ollama.model,
        temperature=0.0,
        timeout_seconds=75,
    )
    return OllamaOpenAIClient(endpoint_config), f"Ollama {endpoint_config.model}"


def _first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    stripped = _first_json_object(stripped)
    candidates = [
        stripped,
        re.sub(r",\s*([}\]])", r"\1", stripped),
    ]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            last_error = exc
    else:
        raise last_error or ValueError("planner did not return parseable JSON")
    if not isinstance(value, dict):
        raise ValueError("planner did not return a JSON object")
    return value


def _repair_planner_json(client: OpenAICompatibleClient, text: str, *, label: str, schema: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "The previous CrawlerAgent planner output was not valid JSON. "
        "Repair it into one complete valid JSON object that matches the schema. "
        "Do not add Markdown or prose. If content is truncated, keep only complete useful fields and tasks.\n"
        f"Schema example: {json.dumps(schema, ensure_ascii=False)}\n"
        f"Broken output:\n{text[:8000]}"
    )
    repaired = client.chat(
        [
            {"role": "system", "content": "Output only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=2800,
    )
    value = _json_from_text(repaired)
    value["_planner_model"] = f"{label} json-repair"
    return value


def _task(source: str, query: str, reason: str, priority: int, *, search_limit: int | None = None, max_urls: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"source": source, "query": query, "reason": reason, "priority": priority}
    if search_limit is not None:
        item["search_limit"] = search_limit
    if max_urls is not None:
        item["max_urls"] = max_urls
    if source == "tavily":
        item["search_depth"] = "advanced"
    return item


def _normalize_task(raw: dict[str, Any], reason: str, fallback_priority: int) -> dict[str, Any] | None:
    source = str(raw.get("source") or "").strip()
    query = str(raw.get("query") or "").strip()
    if source not in ALLOWED_SOURCES or not (2 <= len(query) <= 260):
        return None
    if _generic_standalone_query(query) or _query_is_delivery_target(query):
        return None
    defaults = SOURCE_DEFAULTS.get(source, {})
    item: dict[str, Any] = {
        "source": source,
        "query": query,
        "reason": str(raw.get("reason") or reason or "Crawler LLM planned collection task"),
        "priority": int(raw.get("priority") or defaults.get("priority") or fallback_priority),
    }
    for key in ("search_limit", "max_urls", "mods", "modpacks", "resourcepacks", "shaders", "search_depth", "max_items", "output_dir", "start_url", "timeout_ms", "fields"):
        value = raw.get(key, defaults.get(key))
        if value is not None:
            item[key] = value
    return item


def _generic_standalone_query(query: str) -> bool:
    value = re.sub(r"\s+", " ", str(query).strip())
    compact = re.sub(r"\s+", "", value).lower()
    if compact in GENERIC_COLLECTION_TERMS:
        return True
    if re.fullmatch(r"(boss|bosses|攻略|教程|玩法|物品|系统|模组|整合包)\s*(攻略|列表|清单|打法|教程)?", value, flags=re.I):
        return True
    return False


def _query_is_delivery_target(query: str, target_hint: str = "") -> bool:
    value = re.sub(r"\s+", " ", str(query).strip())
    lowered = value.lower()
    if _looks_like_agent_target(value):
        return True
    if target_hint and re.search(r"\bmcagent\b|\brag\b|crawleragent", lowered, flags=re.I):
        if target_hint.lower() not in lowered:
            return True
    return False


def _select_diverse_tasks(tasks: list[dict[str, Any]], max_tasks: int) -> list[dict[str, Any]]:
    if len(tasks) <= max_tasks:
        return tasks
    selected: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    source_counts: dict[str, int] = {}
    mcmod_first_limit = min(6, max(2, max_tasks // 3))
    per_source_soft_cap = {
        "mcmod": mcmod_first_limit,
        "modrinth": 2,
        "followup": 2,
        "tavily": max(2, max_tasks // 5),
        "firecrawl": max(2, max_tasks // 5),
        "jina": max(2, max_tasks // 5),
        "web_discovery": max(2, max_tasks // 5),
        "playwright": max(3, max_tasks // 4),
    }

    def add(task: dict[str, Any]) -> None:
        source = str(task.get("source") or "")
        query = re.sub(r"\s+", " ", str(task.get("query") or "").strip()).lower()
        if not query or (source, query) in seen_pairs:
            return
        selected.append(task)
        seen_pairs.add((source, query))
        source_counts[source] = source_counts.get(source, 0) + 1

    for task in tasks:
        if len(selected) >= max_tasks:
            break
        source = str(task.get("source") or "")
        query = re.sub(r"\s+", " ", str(task.get("query") or "").strip()).lower()
        if (source, query) in seen_pairs:
            continue
        if source_counts.get(source, 0) >= per_source_soft_cap.get(source, max(2, max_tasks // 4)):
            continue
        add(task)

    for task in tasks:
        if len(selected) >= max_tasks:
            break
        add(task)
    return selected[:max_tasks]


def _sanitize_plan(raw: dict[str, Any], question: str, source_dir: Path, max_tasks: int, session_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    question = _strip_delivery_recipient(question)
    target_hint = _session_target_hint(session_summary) or _collection_target_hint(question) or _question_subject_hint(question)
    topic = str(raw.get("topic") or "").strip()
    if target_hint and (not topic or _looks_like_agent_target(topic)):
        topic = target_hint
    if target_hint and topic != target_hint and target_hint in topic:
        topic = target_hint
    if target_hint and re.match(r"^(?:请|帮我|开始|重新|复跑|采集|收集|获取|整理|补齐|补充)", topic):
        topic = target_hint
    if target_hint and any(_looks_like_agent_target(value) for value in (topic, str(raw.get("target_hint") or ""))):
        topic = target_hint
    package_type = str(raw.get("package_type") or "").strip()
    delivery_target = str(raw.get("delivery_target") or "").strip()
    cleaning_policy = str(raw.get("cleaning_policy") or "").strip()
    coverage_goals = raw.get("coverage_goals")
    if not isinstance(coverage_goals, list):
        coverage_goals = []
    coverage_goals = [str(item).strip() for item in coverage_goals if str(item).strip()][:12]
    success_criteria = raw.get("success_criteria")
    if not isinstance(success_criteria, list):
        success_criteria = []
    success_criteria = [str(item).strip() for item in success_criteria if str(item).strip()][:12]
    fallback = decompose_crawler_queries(question)
    subqueries = raw.get("subqueries")
    if not isinstance(subqueries, list):
        subqueries = []
    queries = []
    for item in subqueries:
        query = str(item).strip()
        if 2 <= len(query) <= 80:
            queries.append(query)
    if not queries:
        queries = [str(item) for item in fallback.get("queries") or []]
        topic = topic or str(fallback.get("project_query") or "")
    if target_hint:
        core_terms = _target_core_terms(target_hint)
        has_target = any(any(term.lower() in query.lower() for term in core_terms) for query in queries)
        if not has_target or any(_looks_like_agent_target(query) for query in queries[:2]):
            queries = [*_target_queries(target_hint), *queries]
    coverage_queries = _coverage_queries(question, session_summary, target_hint or topic)
    if coverage_queries:
        # Coverage queries are helper suggestions. Keep the Crawler LLM's own
        # task order first so a broad entity probe such as "落幕曲" is not
        # displaced by narrower helper phrases like "落幕曲 Boss".
        queries = [*queries, *coverage_queries]
    fallback_items = [str(item) for item in fallback.get("items") or []]
    fallback_queries = [str(item) for item in fallback.get("queries") or []]
    if len(fallback_items) >= 3:
        covered = [name for name in fallback_items if any(name in query for query in queries)]
        if len(covered) < len(fallback_items):
            queries = [*fallback_queries, *queries]
        elif len(queries) < len(fallback_items):
            queries = [*fallback_queries, *queries]
    queries = [
        query
        for query in list(dict.fromkeys(queries))
        if not (_query_is_delivery_target(query, target_hint)) and not _generic_standalone_query(query)
    ][:16]

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list):
        raw_sources = []
    sources = [str(source).strip() for source in raw_sources if str(source).strip() in ALLOWED_SOURCES]
    if not sources:
        sources = ["mcmod", "jina", "web_discovery", "playwright", "modpack_download", "tavily", "firecrawl"]
    if "mcmod" in sources:
        for source in ("jina", "web_discovery", "playwright", "modpack_download", "tavily", "firecrawl"):
            if source not in sources:
                sources.append(source)

    tasks: list[dict[str, Any]] = []
    raw_tasks = raw.get("tasks")
    if isinstance(raw_tasks, list):
        for index, raw_task in enumerate(raw_tasks):
            if not isinstance(raw_task, dict):
                continue
            task = _normalize_task(raw_task, str(raw.get("reason") or ""), 80 - index)
            if task:
                tasks.append(task)
    if coverage_queries and len(tasks) < max_tasks:
        for offset, query in enumerate(coverage_queries[: max(0, max_tasks - len(tasks))]):
            supplemental = _task(
                "mcmod",
                query,
                "supplemental coverage query suggested by Crawler helper; Crawler LLM tasks remain primary",
                58 - offset,
                search_limit=10,
                max_urls=8,
            )
            if (supplemental["source"], supplemental["query"]) not in {(item["source"], item["query"]) for item in tasks}:
                tasks.append(supplemental)

    priority_base = {source: int(defaults.get("priority") or 50) for source, defaults in SOURCE_DEFAULTS.items()}
    for source in sources:
        if source == "browser_collect" and any(str(item.get("source") or "") == "browser_collect" for item in tasks):
            continue
        if source in {"modrinth", "followup", "mediawiki", "ftbwiki", "createwiki", "modpack_download"}:
            source_queries = [topic or queries[0]]
        elif source == "playwright":
            source_queries = queries[:2]
        elif source == "mcmod":
            source_queries = queries[:14]
        else:
            source_queries = queries[:5]
        for offset, query in enumerate(source_queries):
            task = _task(
                source,
                query,
                str(raw.get("reason") or "Crawler LLM planned focused query"),
                priority_base.get(source, 50) - offset,
                search_limit=8 if source != "playwright" else 6,
                max_urls=8 if source != "playwright" else 4,
            )
            if (task["source"], task["query"]) not in {(item["source"], item["query"]) for item in tasks}:
                tasks.append(task)
    if isinstance(session_summary, dict):
        output_dir = str(session_summary.get("output_dir") or "").strip()
        start_url = str(session_summary.get("start_url") or "").strip()
        max_items = session_summary.get("max_items")
        fields = session_summary.get("schema_fields") or session_summary.get("fields")
        for task in tasks:
            if str(task.get("source") or "") != "browser_collect":
                continue
            if output_dir and not task.get("output_dir"):
                task["output_dir"] = output_dir
            if start_url and not task.get("start_url"):
                task["start_url"] = start_url
            if max_items and not task.get("max_items"):
                task["max_items"] = max_items
            if fields and not task.get("fields"):
                task["fields"] = fields
    tasks.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    if not tasks:
        fallback = plan_crawler_tasks(question, source_dir, max_tasks=max_tasks, include_completed=True)
        tasks = list(fallback.get("tasks") or [])
    return {
        "question": question,
        "strategy": "crawler_llm_planner",
        "planner_model": raw.get("_planner_model", ""),
        "topic": topic,
        "target_hint": target_hint,
        "package_type": package_type,
        "delivery_target": delivery_target,
        "cleaning_policy": cleaning_policy,
        "coverage_goals": coverage_goals,
        "known_components": _known_components(session_summary),
        "success_criteria": success_criteria,
        "subqueries": queries,
        "sources": sources,
        "reason": raw.get("reason", ""),
        "tasks": _select_diverse_tasks(tasks, max(1, max_tasks)),
        "raw_plan": raw,
    }


def plan_crawler_tasks_with_llm(question: str, source_dir: Path, *, max_tasks: int = 8, session_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    question = _strip_delivery_recipient(question)
    client, label = _planner_client()
    fallback = decompose_crawler_queries(question)
    target_hint = _session_target_hint(session_summary) or _collection_target_hint(question)
    learned_memory = _crawler_memory_digest(limit=8)
    schema = {
        "topic": "short target entity, e.g. 落幕曲 Closing Song or Utopia",
        "package_type": "modpack|mod|item|guide|unknown",
        "coverage_goals": [
            "basic info and aliases",
            "official/download/community links",
            "mod list or dependency list",
            "beginner route/gameplay guide",
            "systems/items/bosses/recipes mentioned by the target",
            "known issues and useful tutorials",
        ],
        "subqueries": ["short search phrase, not the full user sentence"],
        "sources": ["modpack_internal", "modpack_download", "browser_collect", "mcmod", "modrinth", "tavily", "firecrawl", "jina", "web_discovery", "followup", "playwright"],
        "tasks": [{"source": "browser_collect", "query": "short task/query", "reason": "why this source/query", "priority": 120, "output_dir": "optional user requested folder", "max_items": 50, "fields": ["name", "price", "url"]}],
        "success_criteria": [
            "save markdown plus manifest",
            "preserve source URLs and raw HTML when the fetcher supports it",
            "content is chunkable and citeable when the caller is an RAG agent",
        ],
        "delivery_target": "MCagent RAG|human|both|unknown",
        "cleaning_policy": "RAG-oriented markdown chunks with source URL, title, metadata, raw_html path, dedupe fingerprint",
        "reason": "planning rationale",
    }
    prompt = {
        "role": "user",
        "content": (
            "You are CrawlerAgent. Plan crawler tool actions only; do not answer the user.\n"
            "Participants: human user, MCagent, CrawlerAgent. Preserve caller and delivery target, but do not treat MCagent/RAG/ingest as search topics.\n"
            "Decide target entity, coverage goals, short source-specific queries, and ordered tasks. Tools execute after your JSON plan.\n"
            "For general data collection tasks that ask for structured fields and a save location, use browser_collect. It can open a browser, collect item rows, and save CSV/JSON/report to output_dir. Keep the user's requested output_dir exactly.\n"
            "For full Minecraft modpack collection, cover: basic info, official/download/community links, mod list, quests/beginner route, key systems, items/recipes/acquisition, bosses, tutorials, known issues.\n"
            "If local archive or manifest exists, use modpack_internal first. It extracts manifest, modlist, FTB Quests, KubeJS, OpenLoader/data, recipes, config, raw text. Then use mcmod/modrinth/public web to fill gaps.\n"
            "If no local archive exists, first discover official/download/project pages. Use modpack_download to look for public .mrpack/.zip archives and save them locally; after a real archive is downloaded, use modpack_internal. Use Modrinth with modpack contents for .mrpack projects; use Playwright/topic_discovery to find public download pages and preserve their HTML. Do not pretend the pack internals are available until an archive/manifest is actually downloaded or provided.\n"
            "Playwright is a first-class local browser collection tool, not only a last fallback. Use it when API search/extract is empty, quota-limited, blocked, JS-rendered, or when you need to preserve page HTML after normal readers lose tables, tabs, images, or download links.\n"
            "When recent results are empty/off-topic or Firecrawl/Tavily quota fails, move Playwright or topic_discovery earlier instead of repeating the same API path. Browser-rendered evidence is often better for Chinese modpack pages, tabs, images, and download links.\n"
            "Queries must be short. Do not use the whole user sentence as a query. Component/system queries may omit the parent pack name when context confirms membership; later validation judges relevance.\n"
            "For MCagent/RAG delivery, require Markdown, manifest, stable title, source URL/internal path, metadata, dedupe key, raw_html/raw_text where available.\n"
            "Available sources: modpack_internal, modpack_download, browser_collect, mcmod, modrinth, followup, tavily, firecrawl, jina, web_discovery, playwright, mediawiki, ftbwiki, createwiki.\n"
            "Return valid JSON only, no Markdown, no prose.\n"
            f"用户问题: {question}\n"
            f"采集目标提示: {target_hint or '未明确，请从问题和会话摘要判断'}\n"
            f"会话摘要: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
            f"Crawler 可回忆经验: {json.dumps(learned_memory, ensure_ascii=False)}\n"
            f"规则 fallback: {json.dumps(fallback, ensure_ascii=False)}\n"
            f"JSON schema example: {json.dumps(schema, ensure_ascii=False)}"
        ),
    }
    text = client.chat([
        {"role": "system", "content": "只输出合法 JSON。"},
        prompt,
    ], temperature=0.0, max_tokens=5000)
    try:
        raw = _json_from_text(text)
        raw["_planner_model"] = label
    except Exception:
        raw = _repair_planner_json(client, text, label=label, schema=schema)
    return _sanitize_plan(raw, question, source_dir, max_tasks, session_summary=session_summary)


def plan_crawler_tasks_resilient(question: str, source_dir: Path, *, max_tasks: int = 8, session_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    question = _strip_delivery_recipient(question)
    try:
        return plan_crawler_tasks_with_llm(question, source_dir, max_tasks=max_tasks, session_summary=session_summary)
    except Exception as exc:  # noqa: BLE001
        fallback = _fallback_plan_with_target(question, source_dir, max_tasks, planner_error=f"{type(exc).__name__}: {exc}", session_summary=session_summary)
        fallback.setdefault("strategy", "rule_fallback_after_llm_planner_error")
        return fallback


def plan_crawler_tasks_rule_fallback(question: str, source_dir: Path, *, max_tasks: int = 8, planner_error: str = "", session_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    question = _strip_delivery_recipient(question)
    fallback = _fallback_plan_with_target(question, source_dir, max_tasks, planner_error=planner_error, session_summary=session_summary)
    fallback.setdefault("strategy", "rule_fallback_after_planner_timeout")
    return fallback


def reflect_crawler_progress(
    question: str,
    plan: dict[str, Any],
    task_results: list[dict[str, Any]],
    pending_tasks: list[dict[str, Any]],
    *,
    session_summary: dict[str, Any] | None = None,
    max_new_tasks: int = 4,
) -> dict[str, Any]:
    """Ask CrawlerAgent what to do next in its action loop.

    The returned object is a decision for the executor. It should not fetch
    anything itself; tools remain outside the LLM and run only after this
    decision is made.
    """
    compact_results = [_compact_result_for_reflection(item) for item in task_results[-8:]]
    learned_memory = _crawler_memory_digest(limit=8)
    compact_pending = [
        {
            "index": index,
            "source": str(task.get("source") or ""),
            "query": str(task.get("query") or ""),
            "reason": str(task.get("reason") or "")[:240],
        }
        for index, task in enumerate(pending_tasks[:12])
        if isinstance(task, dict)
    ]
    try:
        client, label = _planner_client()
        schema = {
            "action": "execute_pending|add_tasks|replan|finish",
            "selected_index": 0,
            "reason": "why this is the next best action",
            "tasks": [{"source": "mcmod", "query": "short query", "reason": "why", "priority": 100}],
            "done_summary": "only when action=finish",
        }
        prompt = (
            "You are CrawlerAgent inside an agentic crawler loop. Decide the next action before any tool runs.\n"
            "You are not a script and not a Q&A answerer. You are the LLM that controls which crawler tool should run next.\n"
            "Tools can only execute objective actions after you choose them: search, scrape, save markdown/raw HTML, write manifest, ingest later.\n"
            "Use the loop: understand goal -> choose next tool/query -> inspect objective result summary -> decide continue/replan/finish.\n"
            "If pending tasks are good, choose execute_pending and selected_index. If pending tasks are off-target, empty, repeated, quota-limited, or too generic, choose replan or add_tasks with short source-specific queries.\n"
            "When action is add_tasks or replan, the tasks array must contain executable next tasks. If you only explain the need to replan without tasks, the executor must ask you again.\n"
            "If enough useful records or reusable evidence has been found for the delivery target, choose finish.\n"
            "Do not use the whole user request as a query. Keep queries short and reusable.\n"
            "If the task is for MCagent/RAG, prefer evidence that is citeable and chunkable; raw HTML support is valuable for hard pages.\n"
            "Playwright is a first-class browser tool. Prefer it when Firecrawl/Tavily/Jina fail, when a page needs rendering, or when project tabs/download pages need browser HTML.\n"
            "For full modpack collection without a local archive, use modpack_download to find and save public .mrpack/.zip archives, and use Playwright/topic_discovery to inspect project/download pages and preserve download-link HTML. Use modpack_internal only after a real local archive/manifest is available.\n"
            "If several API/search tasks are empty or off-topic, do not keep cycling similar queries; escalate to browser-rendered collection or finish with a clear blocked/missing-download reason.\n"
            "For structured extraction with requested fields/output directory, choose browser_collect and preserve output_dir/max_items/fields.\n"
            "Available sources: modpack_internal, modpack_download, browser_collect, mcmod, modrinth, followup, tavily, firecrawl, jina, web_discovery, playwright, mediawiki, ftbwiki, createwiki.\n"
            "Return valid JSON only.\n"
            f"question: {question}\n"
            f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
            f"crawler_memory: {json.dumps(learned_memory, ensure_ascii=False)}\n"
            f"plan: {json.dumps(_compact_plan_for_reflection(plan), ensure_ascii=False)}\n"
            f"recent_results: {json.dumps(compact_results, ensure_ascii=False)}\n"
            f"pending_tasks: {json.dumps(compact_pending, ensure_ascii=False)}\n"
            f"JSON schema: {json.dumps(schema, ensure_ascii=False)}"
        )
        raw_text = client.chat(
            [
                {"role": "system", "content": "只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=3600,
        )
        try:
            raw = _json_from_text(raw_text)
        except Exception:
            raw = _repair_planner_json(client, raw_text, label=label, schema=schema)
        action = str(raw.get("action") or "execute_pending").strip().lower()
        if action not in {"execute_pending", "add_tasks", "replan", "finish"}:
            action = "execute_pending"
        selected_index = _safe_int(raw.get("selected_index"), 0)
        if selected_index < 0 or selected_index >= max(len(compact_pending), 1):
            selected_index = 0
        tasks: list[dict[str, Any]] = []
        for raw_task in list(raw.get("tasks") or [])[:max_new_tasks]:
            if isinstance(raw_task, dict):
                task = _normalize_task(raw_task, str(raw.get("reason") or "CrawlerAgent reflection task"), 75)
                if task:
                    tasks.append(task)
        return {
            "action": action,
            "selected_index": selected_index,
            "reason": str(raw.get("reason") or "CrawlerAgent selected next action.").strip()[:500],
            "tasks": _select_diverse_tasks(tasks, max_new_tasks),
            "done_summary": str(raw.get("done_summary") or "").strip()[:800],
            "planner": label,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "action": "execute_pending" if pending_tasks else "finish",
            "selected_index": 0,
            "reason": f"CrawlerAgent reflection failed; executor will use conservative pending task fallback: {type(exc).__name__}: {exc}",
            "tasks": [],
            "done_summary": "",
            "planner": "reflection_fallback_after_llm_error",
        }


def _compact_plan_for_reflection(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic": plan.get("topic"),
        "target_hint": plan.get("target_hint"),
        "delivery_target": plan.get("delivery_target"),
        "requested_by": plan.get("requested_by"),
        "handoff_from": plan.get("handoff_from"),
        "coverage_goals": list(plan.get("coverage_goals") or [])[:8],
        "success_criteria": list(plan.get("success_criteria") or [])[:6],
        "sources": list(plan.get("sources") or [])[:12],
    }


def _compact_result_for_reflection(result: dict[str, Any]) -> dict[str, Any]:
    manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
    validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
    reusable = result.get("existing_evidence_reused") if isinstance(result.get("existing_evidence_reused"), dict) else {}
    return {
        "source": result.get("source"),
        "query": result.get("query"),
        "returncode": result.get("returncode"),
        "records": manifest.get("records"),
        "skipped": manifest.get("skipped"),
        "errors": manifest.get("errors"),
        "matched": validation.get("matched"),
        "validation_reason": validation.get("reason"),
        "reused_existing": reusable.get("matched"),
        "empty": bool(result.get("empty_result")),
        "off_topic": bool(result.get("off_topic_result")),
        "uncertain": bool(result.get("uncertain_result")),
        "timed_out": bool(result.get("timed_out")),
    }


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def review_topic_discovery_candidates(
    question: str,
    candidates: list[str],
    phrases: list[str],
    existing_tasks: list[dict[str, Any]],
    *,
    max_tasks: int = 12,
) -> dict[str, Any]:
    fallback_config = load_config()
    client = OllamaOpenAIClient(
        OllamaConfig(
            base_url=fallback_config.ollama.base_url,
            model=fallback_config.ollama.model,
            temperature=0.0,
            timeout_seconds=120,
        )
    )
    label = f"Ollama topic-review {fallback_config.ollama.model}"
    compact_candidates = [str(item) for item in candidates if str(item).strip()][:30]
    compact_phrases = [str(item) for item in phrases if str(item).strip()][:50]
    existing_compact = existing_tasks[:30]
    prompt = (
        "You are CrawlerAgent. A tool produced candidate search topics from existing local documents. "
        "The tool only provides candidates; you must judge which are useful. "
        "Pick useful unknown or under-covered topics for the target and reject noise. "
        "Output ONLY lines in this exact format, no markdown:\n"
        "ACCEPT|source|query|reason\n"
        "REJECT|topic|reason\n"
        "Allowed source values: mcmod, tavily, firecrawl, jina, web_discovery, playwright, modpack_download.\n"
        "Prefer mcmod for Chinese MC百科/tutorial content; use tavily/web_discovery for video/community indexes. "
        "Do not choose generic homepage queries if specific candidates exist.\n"
        f"Target/question: {question}\n"
        f"Candidate seed queries: {json.dumps(compact_candidates[:24], ensure_ascii=False)}\n"
        f"Candidate phrases: {json.dumps(compact_phrases[:40], ensure_ascii=False)}\n"
        f"Already planned tasks: {json.dumps(existing_compact[:20], ensure_ascii=False)}"
    )
    messages = [
        {"role": "system", "content": "You are CrawlerAgent. Output only ACCEPT/REJECT lines."},
        {"role": "user", "content": prompt},
    ]
    try:
        text = client.chat(messages, temperature=0.0, max_tokens=900)
    except Exception:
        short_prompt = (
            "Output only ACCEPT/REJECT lines. Format: ACCEPT|source|query|reason. "
            "Allowed sources: mcmod,tavily,web_discovery,playwright,modpack_download. Pick useful topics for the target. "
            f"Target: {question}\n"
            f"Candidates: {json.dumps(compact_candidates[:12], ensure_ascii=False)}"
        )
        try:
            text = client.chat(
                [
                    {"role": "system", "content": "Output only ACCEPT/REJECT lines."},
                    {"role": "user", "content": short_prompt},
                ],
                temperature=0.0,
                max_tokens=900,
            )
        except Exception:
            fallback_config = load_config()
            fallback_client = OllamaOpenAIClient(
                OllamaConfig(
                    base_url=fallback_config.ollama.base_url,
                    model=fallback_config.ollama.model,
                    temperature=0.0,
                    timeout_seconds=120,
                )
            )
            text = fallback_client.chat(
                [
                    {"role": "system", "content": "Output only ACCEPT/REJECT lines."},
                    {"role": "user", "content": short_prompt},
                ],
                temperature=0.0,
                max_tokens=900,
            )
            label = f"Ollama fallback {fallback_config.ollama.model}"
        if not text.strip():
            fallback_config = load_config()
            fallback_client = OllamaOpenAIClient(
                OllamaConfig(
                    base_url=fallback_config.ollama.base_url,
                    model=fallback_config.ollama.model,
                    temperature=0.0,
                    timeout_seconds=120,
                )
            )
            text = fallback_client.chat(
                [
                    {"role": "system", "content": "Output only ACCEPT/REJECT lines."},
                    {"role": "user", "content": short_prompt},
                ],
                temperature=0.0,
                max_tokens=900,
            )
            label = f"Ollama fallback {fallback_config.ollama.model}"
    if not text.strip():
        fallback_config = load_config()
        fallback_client = OllamaOpenAIClient(
            OllamaConfig(
                base_url=fallback_config.ollama.base_url,
                model=fallback_config.ollama.model,
                temperature=0.0,
                timeout_seconds=120,
            )
        )
        short_prompt = (
            "Output only ACCEPT lines. Format: ACCEPT|source|query|reason. "
            "Allowed sources: mcmod,tavily,web_discovery,playwright,modpack_download. "
            f"Target: {question}\n"
            f"Candidates: {json.dumps(compact_candidates[:10], ensure_ascii=False)}"
        )
        text = fallback_client.chat(
            [
                {"role": "system", "content": "Output only ACCEPT lines."},
                {"role": "user", "content": short_prompt},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        label = f"Ollama fallback {fallback_config.ollama.model}"
    tasks: list[dict[str, Any]] = []
    accepted_topics: list[str] = []
    rejected_topics: list[dict[str, str]] = []
    allowed_sources = {"mcmod", "tavily", "firecrawl", "jina", "web_discovery", "playwright", "modpack_download"}
    for index, line in enumerate(text.splitlines()):
        parts = [part.strip() for part in line.strip().strip("-* ").split("|")]
        if not parts:
            continue
        kind = parts[0].upper()
        if kind == "ACCEPT" and len(parts) >= 4:
            source = parts[1]
            query = parts[2]
            reason = "|".join(parts[3:])
            if source not in allowed_sources:
                source = "mcmod"
            task = _normalize_task({"source": source, "query": query, "reason": reason, "priority": 100 - len(tasks)}, "Crawler LLM reviewed topic discovery candidates", 100 - len(tasks))
            if task:
                accepted_topics.append(query)
                tasks.append(task)
        elif kind == "REJECT" and len(parts) >= 3:
            rejected_topics.append({"topic": parts[1], "reason": "|".join(parts[2:])})
    tasks.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    if not tasks:
        raise ValueError(f"topic discovery review produced no ACCEPT tasks; raw={text[:500]!r}")
    return {
        "strategy": "topic_discovery_llm_review",
        "planner_model": label,
        "accepted_topics": accepted_topics,
        "rejected_topics": rejected_topics,
        "reason": "Crawler LLM reviewed topic discovery candidates using ACCEPT/REJECT protocol.",
        "tasks": _select_diverse_tasks(tasks, max(1, max_tasks)),
        "raw_review": text,
    }
