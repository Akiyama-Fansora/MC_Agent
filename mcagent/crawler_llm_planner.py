from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import load_config
from .crawler_planner import decompose_crawler_queries, plan_crawler_tasks
from .crawler_reflection_decision_service import CrawlerReflectionDecisionService
from .crawler_reflection_service import CrawlerReflectionSnapshotService
from .agent_memory import read_memory_events
from .agent_runtime import classify_crawler_tool_result, crawler_collection_catalog_prompt
from .llm import OpenAICompatibleClient
from .llm_profiles import client_for_agent


ALLOWED_SOURCES = {"mcagent_context", "topic_discovery", "modpack_internal", "modpack_download", "mcmod", "modrinth", "followup", "web_discovery", "playwright", "browser_collect", "fetch_url", "save_artifact", "read_local_file", "search_local_files", "mediawiki", "ftbwiki", "createwiki"}

SOURCE_DEFAULTS: dict[str, dict[str, Any]] = {
    "mcagent_context": {"priority": 150},
    "modpack_internal": {"priority": 145},
    "topic_discovery": {"priority": 130, "max_files": 120, "max_queries": 40},
    "mcmod": {"priority": 100, "search_limit": 10, "max_urls": 8},
    "modrinth": {"priority": 88},
    "followup": {"priority": 76, "max_urls": 16},
    "web_discovery": {"priority": 62, "search_limit": 8, "max_urls": 8},
    "playwright": {"priority": 82, "search_limit": 8, "max_urls": 6},
    "browser_collect": {"priority": 120, "max_items": 50},
    "fetch_url": {"priority": 125},
    "save_artifact": {"priority": 110},
    "read_local_file": {"priority": 108},
    "search_local_files": {"priority": 106},
    "modpack_download": {"priority": 130, "search_limit": 8},
    "ftbwiki": {"priority": 80, "search_limit": 8},
    "createwiki": {"priority": 80, "search_limit": 8},
    "mediawiki": {"priority": 50, "search_limit": 8},
}

GENERAL_COLLECTION_SOURCES = ["web_discovery", "playwright", "fetch_url", "browser_collect", "save_artifact", "read_local_file", "search_local_files"]
MINECRAFT_DOMAIN_SOURCES = ["mcmod", "modrinth", "followup", "mediawiki", "ftbwiki", "createwiki", "modpack_download", "modpack_internal"]

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
    "新手路线": ["新手路线", "FTB任务", "开局 攻略"],
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
    text = re.sub(r"^(?:用户原始目标|原始请求|用户请求|Task goal|Original user request)\s*[:：]\s*", "", text, flags=re.I)
    english_target = _english_modpack_target_hint(text)
    if english_target:
        return english_target
    modern_target = _modern_chinese_modpack_target_hint(text)
    if modern_target:
        return modern_target
    named_alias = _named_modpack_alias_hint(text)
    if named_alias:
        return named_alias
    embedded_target = _embedded_modpack_target_hint(text)
    if embedded_target:
        return embedded_target
    quoted_patterns = [
        r"(?:整合包|modpack)[「《\"'“]([^」》\"'”]{2,60})[」》\"'”]",
        r"[「《\"'“]([^」》\"'”]{2,60})[」》\"'”](?:的)?(?:完整公开数据|完整公开资料|完整数据|完整资料|资料|数据|内容|知识库|整合包|modpack)",
    ]
    for pattern in quoted_patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            target = _clean_target_hint(match.group(1))
            if target:
                return target
    slash_alias_patterns = [
        r"(?:以|用|拿|对|关于|针对)\s*([\u4e00-\u9fff]{2,60})\s*(?:/|／)\s*([A-Za-z][A-Za-z0-9_ （）()+.·' -]{2,60})\s*(?:为例|进行|做|采集|抓取|测试|公开资料|资料)",
        r"([\u4e00-\u9fffA-Za-z0-9_ （）()+.·-]{2,60}\s*(?:/|／)\s*[A-Za-z][A-Za-z0-9_ （）()+.·' -]{2,60})\s*(?:整合包|modpack)(?:的)?(?:完整公开数据|完整公开资料|完整数据|完整资料|详细资料|公开资料|资料|数据|内容|知识库|模组列表|玩法指南|包括)",
        r"([\u4e00-\u9fff]{2,60})\s*(?:/|／)\s*([A-Za-z][A-Za-z0-9_ （）()+.·' -]{2,60})\s*(?:整合包|modpack)",
    ]
    for pattern in slash_alias_patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        target = " / ".join(part.strip() for part in match.groups() if part and part.strip())
        target = _clean_target_hint(target, max_len=90)
        if target:
            return target
    package_patterns = [
        r"(?:针对|为|关于|有关)?\s*([\u4e00-\u9fffA-Za-z0-9_ （）()+.-]{2,60}?)(整合包|modpack)(?:的)?(?:采集|收集|获取|补充|补齐|整理)?(?:缺失|缺少|缺口|完整|详细|相关)?(?:资料|数据|内容|知识库)",
        r"(?:关于|有关)?\s*(?:Minecraft|MC)?\s*(整合包|modpack)\s*([\u4e00-\u9fffA-Za-z0-9_ （）()+-]{2,60}?)(?:的)?(?:完整数据|完整资料|详细资料|资料|数据|内容|知识库|模组列表|玩法指南|包括)",
        r"([\u4e00-\u9fffA-Za-z0-9_ （）()+-]{2,60}?)(整合包|modpack)(?:还)?(?:缺|缺少|缺哪些|有哪些缺口|还差|需要补|补全|补充)",
        r"([\u4e00-\u9fffA-Za-z0-9_ （）()+-]{2,60}?)(?:整合包|modpack)(?:的)?(?:完整数据|完整资料|资料|数据|内容|知识库)",
        r"(?:补齐|补充|采集|收集|获取|整理|检查)([\u4e00-\u9fffA-Za-z0-9_ （）()+-]{2,60}?)(?:整合包|modpack)",
    ]
    for pattern in package_patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        if len(match.groups()) >= 2 and str(match.group(1)).lower() in {"整合包", "modpack"}:
            suffix = match.group(1)
            target = match.group(2)
        else:
            suffix = match.group(2) if len(match.groups()) >= 2 else ""
            target = match.group(1)
        target = re.split(r"[,，。；;：:]|流程手册|采集流程|教学复跑|请回忆|重新检查", target, maxsplit=1)[-1]
        target = re.sub(r"^(?:(?:一下|这个|那个|关于|有关|请|帮我|帮忙|问下|问问|询问|咨询|你去|去|把|将|对|并|重新|开始|复跑|检查|补齐|补充|采集|收集|获取|整理|教学复跑|请回忆)\s*)+", "", target.strip())
        target = re.sub(r"^(?:MCagent|MCAgent|MC Agent|CrawlerAgent|Crawler|RAG|本地资料库|知识库)\s*", "", target, flags=re.I)
        if suffix and suffix.lower() in {"整合包", "modpack"} and not re.search(r"(整合包|modpack)$", target, flags=re.I):
            target = f"{target}{suffix}"
        target = _clean_target_hint(target)
        if target:
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
        target = _clean_target_hint(target)
        if target:
            return target
    return ""


def _english_modpack_target_hint(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    direct_patterns = [
        r"\bfor\s+(?:the\s+)?([A-Z][A-Za-z0-9][A-Za-z0-9 _'.:+-]{1,60}?)\s+(?:Minecraft\s+)?modpack\b",
        r"\b(?:Minecraft\s+)?modpack\s+([A-Z][A-Za-z0-9][A-Za-z0-9 _'.:+-]{1,60}?)(?:\s+(?:and|to|with|public|complete|download|archive)\b|[,.;:]|\(|$)",
    ]
    for pattern in direct_patterns:
        match = re.search(pattern, value, flags=re.I)
        if not match:
            continue
        raw_target = str(match.group(1) or "")
        if re.search(r"\b(?:archive|download|public|complete|data|files?|routes?|version|versions)\b|\.mrpack|\.zip", raw_target, flags=re.I):
            continue
        target = _clean_target_hint(_strip_target_suffix(raw_target), max_len=80)
        if target and not re.fullmatch(r"Minecraft|MC|Modpack|Archive|Download|Public|Data|Files?|Routes?", target, flags=re.I):
            return target
    patterns = [
        r"modpack\s+[\"'“”]?([A-Z][A-Za-z0-9][A-Za-z0-9 _'.:+-]{1,60}?)[\"'“”]?\s*(?:[,.;:]|\(|$)",
        r"[\"'“”]([A-Z][A-Za-z0-9][A-Za-z0-9 _'.:+-]{1,60}?)[\"'“”]\s*(?:\([^)]*(?:Chinese name|中文名)[^)]*\))?",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if not match:
            continue
        target = _strip_target_suffix(match.group(1))
        target = re.sub(r"\b(?:complete|public|data|minecraft|modpack|archive|download|public archive routes?)\b", "", target, flags=re.I)
        target = _clean_target_hint(target, max_len=80)
        if target and not re.fullmatch(r"Minecraft|MC|Modpack", target, flags=re.I):
            return target
    return ""


def _modern_chinese_modpack_target_hint(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    slash_patterns = [
        r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_（）()·.路之旅探险纪元 -]{1,60}?)\s*(?:/|／)\s*([A-Za-z][A-Za-z0-9_ ()'.·-]{2,60})\s*(?:整合包|modpack|模组|mod|公开资料|资料|采集)",
        r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_（）()·.路之旅探险纪元 -]{1,60}?)\s+([A-Z][A-Za-z0-9_ ()'.·-]{2,60})\s*(?:整合包|modpack)",
    ]
    for pattern in slash_patterns:
        match = re.search(pattern, value, flags=re.I)
        if not match:
            continue
        cn = _strip_modern_target_prefix(match.group(1))
        en = _strip_target_suffix(match.group(2))
        target = _clean_target_hint(f"{cn} / {en}", max_len=90)
        if target:
            return target
    single_patterns = [
        r"(?:获取|采集|收集|爬取|补齐|补充|整理|查找|搜索|告诉\s*CrawlerAgent\s*获取|让\s*CrawlerAgent\s*获取)\s*([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_（）()·.路之旅探险纪元 -]{1,60}?)\s*(?:整合包|modpack)",
        r"(?:关于|针对|目标(?:是|为)?|主题(?:是|为)?)\s*([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_（）()·.路之旅探险纪元 -]{1,60}?)\s*(?:整合包|modpack)",
        r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_（）()·.路之旅探险纪元 -]{1,60}?)\s*(?:整合包|modpack)(?:的|完整|公开|资料|数据|信息|版本|下载|模组|玩法|攻略)",
    ]
    for pattern in single_patterns:
        match = re.search(pattern, value, flags=re.I)
        if not match:
            continue
        target = _strip_modern_target_prefix(match.group(1))
        target = _clean_target_hint(target, max_len=80)
        if target:
            return target
    return ""


def _strip_modern_target_prefix(value: str) -> str:
    target = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。；;：:")
    target = re.sub(
        r"^(?:请你|请|麻烦|帮我|告诉|叫|让|派|通知|CrawlerAgent|Crawler|MCagent|MCAgent|获取|采集|收集|爬取|补齐|补充|整理|查找|搜索|关于|针对|这个|那个|完整|公开|资料|数据|信息)+\s*",
        "",
        target,
        flags=re.I,
    )
    return target.strip(" ，,。；;：:")


def _named_modpack_alias_hint(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    match = re.search(
        r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_ （）()+.·-]{1,60}?)\s*(?:/|／)\s*([A-Za-z][A-Za-z0-9_ （）()+.·' -]{2,60})\s*(?:整合包|modpack|模组|mod|公开资料|资料|采集)",
        value,
        flags=re.I,
    )
    if not match:
        return ""
    cn = _strip_target_prefix(match.group(1))
    en = _strip_target_suffix(match.group(2))
    target = _clean_target_hint(f"{cn} / {en}", max_len=90)
    return target


def _embedded_modpack_target_hint(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    patterns = [
        r"(?:本地资料(?:里|中)?|本地上下文|MCagent/RAG|MCagent).*?([\u4e00-\u9fffA-Za-z0-9_ （）()+.·-]{2,60}\s*(?:/|／)\s*[A-Za-z][A-Za-z0-9_ （）()+.·' -]{2,60})\s*(?:整合包|modpack)",
        r"(?:本地资料(?:里|中)?|本地上下文|MCagent/RAG|MCagent).*?([\u4e00-\u9fffA-Za-z0-9_ （）()+.·-]{2,60}?)(整合包|modpack)",
        r"(?:目标是|补齐|采集|收集|获取).*?([\u4e00-\u9fffA-Za-z0-9_ （）()+.·-]{2,60}\s*(?:/|／)\s*[A-Za-z][A-Za-z0-9_ （）()+.·' -]{2,60})\s*(?:整合包|modpack)",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if not match:
            continue
        target = re.sub(r"^(?:问下|问问|检查|先检查|然后|把|将|缺失的|公开|资料|里|中)\s*", "", match.group(1).strip())
        suffix = match.group(2) if len(match.groups()) >= 2 else ""
        if suffix and suffix.lower() in {"整合包", "modpack"} and not re.search(r"(整合包|modpack)$", target, flags=re.I):
            target = f"{target}{suffix}"
        target = _clean_target_hint(target, max_len=90)
        if target:
            return target
    return ""


def _session_target_hint(session_summary: dict[str, Any] | None = None) -> str:
    if not isinstance(session_summary, dict):
        return ""
    candidates: list[tuple[int, str]] = []
    weighted_keys = [
        ("authoritative_task_goal", 80),
        ("task_goal", 70),
        ("collection_target", 65),
        ("mcagent_gap_summary", 55),
        ("missing_evidence", 50),
        ("known_context", 30),
        ("handoff_brief", 30),
        ("goal", 25),
        ("target", 20),
        ("current_topic", 5),
    ]
    for key, weight in weighted_keys:
        value = str(session_summary.get(key) or "").strip()
        if not value:
            continue
        extracted = _collection_target_hint(value) or _general_collection_target_hint(value) or _question_subject_hint(value)
        if extracted:
            cleaned = _clean_target_hint(extracted, max_len=80) or extracted
            candidates.append((weight, cleaned))
            continue
        value = _strip_delivery_recipient(value)
        value = _clean_target_hint(value, max_len=80)
        if value:
            candidates.append((weight, value))
    if not candidates:
        return ""
    deduped: dict[str, int] = {}
    for weight, candidate in candidates:
        deduped[candidate] = max(deduped.get(candidate, 0), weight)
    return sorted(deduped, key=lambda item: (deduped[item], *_target_specificity_score(item)), reverse=True)[0]


GENERIC_TARGET_PATTERNS = (
    r"^(?:的)?相关(?:整合包|资料|数据|内容|网页|信息)?$",
    r"^(?:缺失|缺少|缺口|不足|待补)(?:整合包|资料|数据|内容|网页|信息)?$",
    r"^(?:该|此|这个|那个|上述|刚才)(?:主题|目标|整合包|资料|数据|内容)?$",
    r"^(?:资料|数据|内容|信息|缺口|缺失|本地资料|知识库)$",
    r"^(?:MCagent|MCAgent|MC Agent|CrawlerAgent|Crawler|RAG|human)$",
)


def _clean_target_hint(value: Any, *, max_len: int = 90) -> str:
    target = re.sub(r"\s+", " ", str(value or "")).strip(" ：:，,。；;？?！!")
    if not target:
        return ""
    if re.search(r"判断.*(?:模组|mod).*整合包|模组还是整合包|^(?:自己|自行)?判断目标类型$", target, flags=re.I):
        return ""
    target = re.sub(r"^.*?(?:主题|目标|对象|名称)\s*(?:是|为|[:：])\s*", "", target)
    target = re.sub(r"^(?:针对|为|给)\s*", "", target)
    target = _strip_target_prefix(target)
    target = re.split(r"[,，。；;：:]|缺少|缺失|缺口|不足|还缺|需要补", target, maxsplit=1)[0]
    target = re.sub(r"^(?:的|关于|有关)\s*", "", target)
    target = re.sub(r"^(?:MCagent|MCAgent|MC Agent|CrawlerAgent|Crawler|RAG|本地资料库|知识库)\s*", "", target, flags=re.I).strip()
    target = re.sub(r"(?:缺失|缺少|缺的|缺口|还缺|不足|待补|需要补(?:充|齐)?)(?:的)?(?:资料|数据|内容|信息)?$", "", target, flags=re.I)
    target = re.sub(r"(?:缺失|缺少|缺的|缺口|还缺|不足|待补|需要补(?:充|齐)?)(?:的)?$", "", target, flags=re.I)
    target = re.sub(r"(?:的)?(?:资料|数据|内容|信息)$", "", target, flags=re.I)
    target = re.sub(r"\s+", " ", target).strip(" ：:，,。；;？?！!")
    compact = re.sub(r"\s+", "", target)
    if not (2 <= len(target) <= max_len):
        return ""
    if _looks_like_agent_target(target):
        return ""
    if any(re.fullmatch(pattern, compact, flags=re.I) for pattern in GENERIC_TARGET_PATTERNS):
        return ""
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", target):
        return ""
    return target


def _strip_target_prefix(value: str) -> str:
    target = re.sub(r"\s+", " ", str(value or "")).strip(" ：:，,。；;？?！!")
    target = re.sub(
        r"^.*?(?:本地资料(?:里|中)?|本地上下文|目标是|目标为|补齐|补充|采集|收集|获取|整理|检查)\s*(?=[\u4e00-\u9fffA-Za-z0-9])",
        "",
        target,
        flags=re.I,
    )
    target = re.sub(
        r"^(?:MCagent|MCAgent|MC Agent|CrawlerAgent|Crawler|RAG|转达|请|先|让|把|将|缺失的|公开|资料|里|中|的|关于|有关)\s*",
        "",
        target,
        flags=re.I,
    )
    return target.strip(" ：:，,。；;？?！!")


def _strip_target_suffix(value: str) -> str:
    target = re.sub(r"\s+", " ", str(value or "")).strip(" ：:，,。；;？?！!")
    target = re.split(r"\s*(?:完整公开资料|完整资料|公开资料|资料|数据|内容|信息|供\s*MCagent|给\s*MCagent|用于\s*RAG|MCagent/RAG|回答|使用)\b", target, maxsplit=1, flags=re.I)[0]
    return target.strip(" ：:，,。；;？?！!")


def _looks_like_broken_target(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return True
    if "?" in text and not re.search(r"[\u4e00-\u9fff]", text):
        return True
    if re.search(r"\b(?:MCagent|CrawlerAgent|RAG)\b", text, flags=re.I) and not re.search(r"[\u4e00-\u9fff]", text):
        return True
    return False


def _looks_like_instruction_query(query: str, target_hint: str = "") -> bool:
    value = re.sub(r"\s+", " ", str(query or "")).strip()
    if not value:
        return False
    if len(value) > 120 and not _is_url_query(value):
        return True
    if re.search(r"(先检查|然后|转交|交给|入库|给\s*MCagent|给\s*RAG|CrawlerAgent|MCagent|完整信息|玩法路线|新手到毕业)", value, flags=re.I):
        if target_hint and _query_mentions_target(value, target_hint):
            return True
    return False


def _clean_task_query(query: str, *, target_hint: str = "", topic: str = "") -> str:
    value = re.sub(r"\s+", " ", str(query or "").strip())
    if not value or _is_url_query(value):
        return value
    target = target_hint or topic
    if _looks_like_instruction_query(value, target):
        extracted = _collection_target_hint(value) or target
        if extracted:
            return extracted
        return ""
    return value


def _target_specificity_score(value: str) -> tuple[int, int]:
    text = str(value or "")
    lowered = text.lower()
    score = 0
    if re.search(r"探险之旅|journey|closing song|落幕曲", text, flags=re.I):
        score += 6
    if re.search(r"整合包|modpack|minecraft|mc", text, flags=re.I):
        score += 4
    if re.search(r"\d+(?:\.\d+)*", text):
        score += 2
    if text in {"乌托邦", "Utopia"}:
        score -= 3
    return score, min(len(text), 80)


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


def _general_collection_target_hint(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    patterns = [
        r"(?:采集|收集|获取|爬取|整理|查找)\s+([A-Za-z][A-Za-z0-9_.+\- ]{1,80}?)(?:\s*(?:库|项目|框架|文档|官方|的|，|,|。|$))",
        r"(?:collect|crawl|scrape|gather)\s+([A-Za-z][A-Za-z0-9_.+\- ]{1,80}?)(?:\s+(?:docs?|documentation|github|releases?|data|for)\b|[,.;:]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if not match:
            continue
        target = re.sub(r"\b(?:official|docs?|documentation|github|releases?|data|rag|for|use)\b", "", match.group(1), flags=re.I)
        target = re.sub(r"\s+", " ", target).strip(" ，,。.;:")
        if 2 <= len(target) <= 80 and not _looks_like_agent_target(target):
            return target
    return ""


def _looks_like_agent_target(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text)
    if not stripped:
        return True
    lowered = stripped.lower()
    agent_hits = sum(1 for word in AGENT_WORDS if word.lower() in lowered)
    handoff_terms = ("交付", "转交", "委托", "本地上下文", "缺口", "用户原始", "taskgoal", "originaluserrequest", "deliverytarget")
    return agent_hits > 0 and (len(stripped) <= 16 or any(term in lowered for term in handoff_terms))


def _target_core_terms(target: str) -> list[str]:
    terms = [target.strip()]
    lowered = target.lower()
    if "乌托邦" in target and ("整合包" in target or "modpack" in lowered or "minecraft" in lowered):
        terms.extend(["乌托邦探险之旅", "Utopian Journey"])
    cleaned = re.sub(r"(整合包|模组|MOD|modpack|mod|资料|数据)$", "", target.strip(), flags=re.I).strip()
    if cleaned and cleaned != target:
        terms.append(cleaned)
    return [term for term in dict.fromkeys(terms) if term]


def _target_queries(target: str) -> list[str]:
    if not target:
        return []
    base_terms = _target_core_terms(target)
    queries: list[str] = list(base_terms[:4])
    for base in base_terms[:4]:
        pack_query = base if re.search(r"(整合包|modpack)$", base, flags=re.I) else f"{base} 整合包"
        queries.extend(
            [
                base,
                pack_query,
                f"{base} MC百科",
                f"{base} 模组列表",
                f"{base} 新手攻略",
                f"{base} 玩法 教程",
                f"{base} 下载 CurseForge Modrinth",
            ]
        )
    return list(dict.fromkeys(query for query in queries if 2 <= len(query) <= 80))


def _general_target_queries(target: str, context_text: str) -> list[str]:
    if not target:
        return []
    queries = [target]
    lowered = str(context_text or "").lower()
    if any(term in lowered for term in ("官方", "official", "文档", "docs", "documentation")):
        queries.extend([f"{target} official documentation", f"{target} docs"])
    if "github" in lowered or "仓库" in lowered or "repository" in lowered:
        queries.extend([f"{target} GitHub", f"{target} repository"])
    if any(term in lowered for term in ("release", "发布", "changelog", "更新日志")):
        queries.extend([f"{target} releases", f"{target} changelog"])
    if any(term in lowered for term in ("用法", "示例", "example", "usage", "教程")):
        queries.extend([f"{target} examples", f"{target} usage"])
    return list(dict.fromkeys(query for query in queries if 2 <= len(query) <= 120))


def _query_mentions_target(query: str, target: str) -> bool:
    query_compact = re.sub(r"\s+", "", str(query).lower())
    target_terms = [term for term in _target_core_terms(target) if term]
    for term in target_terms:
        compact = re.sub(r"\s+", "", term.lower())
        if compact and compact in query_compact:
            return True
    return False


def _bind_query_to_target(query: str, target: str) -> str:
    value = re.sub(r"\s+", " ", str(query).strip())
    target = re.sub(r"\s+", " ", str(target).strip())
    if not value or not target or _query_mentions_target(value, target):
        return value
    if re.fullmatch(r"(FTB\s*Quests?|FTB任务|任务系统|开局\s*攻略|新手\s*攻略|新手路线|攻略|教程|玩法|Boss|BOSS|boss|模组列表|mod\s*list|dependencies|依赖列表|任务线|阶段攻略|更新日志|版本差异|changelog|下载|配置要求)", value, flags=re.I):
        return f"{target} {value}"
    if len(value) <= 12 and re.search(r"(任务|攻略|教程|玩法|配置|下载|Boss|boss|模组|配方|问题|bug)", value, flags=re.I):
        return f"{target} {value}"
    return value


def _target_bound_queries(queries: list[str], target: str) -> list[str]:
    return list(dict.fromkeys(_bind_query_to_target(query, target) for query in queries if str(query).strip()))


def _is_url_query(query: str) -> bool:
    return bool(re.search(r"https?://", str(query or ""), flags=re.I))


def _task_url_is_grounded(task: dict[str, Any], *, query: str = "") -> bool:
    """Return whether a direct URL task is based on objective input, not a guessed slug."""
    url = str(query or task.get("query") or "").strip()
    if not _is_url_query(url):
        return True
    if task.get("from_discovered_candidate") or task.get("source_url") or task.get("candidate_url"):
        return True
    if task.get("artifact_ref") or task.get("content_ref"):
        return True
    reason = str(task.get("reason") or "")
    if re.search(r"discovered|candidate|search result|from manifest|source page|user provided|exact url|objective", reason, flags=re.I):
        return True
    return False


def _mark_grounded_url_tasks_from_recent_results(tasks: list[dict[str, Any]], recent_results: list[dict[str, Any]]) -> None:
    discovered_urls = _urls_from_recent_results(recent_results)
    if not discovered_urls:
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        query = str(task.get("query") or "").strip()
        if not _is_url_query(query) or _task_url_is_grounded(task, query=query):
            continue
        if _url_objectively_seen(query, discovered_urls):
            task["from_discovered_candidate"] = True
            task["objective_evidence"] = "recent_results manifest_preview/artifact_refs contained this URL or same canonical page URL"


def _urls_from_recent_results(recent_results: list[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"url", "project_url", "download_url", "api_url", "source_url", "candidate_url"}:
                    text = str(item or "").strip()
                    if _is_url_query(text):
                        urls.add(_canonical_url_for_grounding(text))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for result in recent_results:
        visit(result)
    return {item for item in urls if item}


def _url_objectively_seen(url: str, discovered_urls: set[str]) -> bool:
    canonical = _canonical_url_for_grounding(url)
    if canonical in discovered_urls:
        return True
    if canonical.endswith("/"):
        return canonical[:-1] in discovered_urls
    return canonical + "/" in discovered_urls


def _canonical_url_for_grounding(url: str) -> str:
    value = str(url or "").strip()
    value = re.sub(r"#.*$", "", value)
    value = re.sub(r"\?.*$", "", value)
    return value.rstrip("/").lower()


def _downgrade_ungrounded_url_task(task: dict[str, Any], *, target: str) -> dict[str, Any]:
    """Keep CrawlerAgent's intent but make it discover before fetching a guessed URL."""
    cloned = dict(task)
    url = str(cloned.get("query") or "").strip()
    host_match = re.search(r"https?://(?:www\.)?([^/\s]+)", url, flags=re.I)
    host = host_match.group(1) if host_match else ""
    slug = re.sub(r"https?://(?:www\.)?[^/]+/?", "", url, flags=re.I)
    slug = re.sub(r"[/#?].*$", "", slug)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    discovery_terms = [target.strip(), slug, host]
    query = " ".join(term for term in discovery_terms if term)
    cloned["source"] = "web_discovery"
    cloned["query"] = re.sub(r"\s+", " ", query).strip()[:120] or target or url
    cloned["reason"] = (
        "CrawlerAgent proposed an exact URL without objective evidence that it was discovered; "
        "first discover candidate pages, then CrawlerAgent can inspect objective results and choose exact URLs."
    )
    cloned["priority"] = min(int(cloned.get("priority") or 80), 92)
    cloned["original_unverified_url"] = url
    return cloned


def _prefer_general_web_first(context_text: str, delivery_target: str, requested_by: str) -> bool:
    lowered = context_text.lower()
    if any(term in lowered for term in ("mc百科重复", "mcmod duplicate", "重复页", "重复多", "换源", "外部", "非 mc百科", "non-duplicate", "external sources")):
        return True
    if delivery_target == "MCagent/RAG" and any(term in lowered for term in ("缺口", "缺失", "还缺", "本地资料", "本地上下文", "补给", "补充", "补库")):
        return True
    return requested_by in {"user", "mcagent", "user_via_mcagent"} and any(term in lowered for term in ("网上找", "去网上", "public web", "search online"))


def _looks_like_minecraft_domain_context(context_text: str) -> bool:
    value = str(context_text or "")
    if not value.strip():
        return False
    return bool(
        re.search(
            r"\b(?:minecraft|mc百科|mcmod|modrinth|curseforge|modpack|mod list|kubejs|ftb quests|packwiz|mrpack)\b|整合包|模组|光影|资源包|我的世界",
            value,
            flags=re.I,
        )
    )


def _default_collection_sources_for_context(context_text: str, *, prefer_general_web: bool = False, archive_negated: bool = False) -> list[str]:
    """Return capability-first source defaults without making Crawler Minecraft-only."""

    if not _looks_like_minecraft_domain_context(context_text):
        return list(GENERAL_COLLECTION_SOURCES)
    if prefer_general_web:
        sources = ["web_discovery", "playwright", "modpack_download", "fetch_url", "followup", "mcmod", "modrinth"]
    else:
        sources = ["web_discovery", "playwright", "fetch_url", "modpack_download", "followup", "mcmod", "modrinth"]
    if archive_negated:
        sources = [source for source in sources if source != "modpack_download"]
    return sources


def _is_gap_analysis_collection_context(context_text: str, delivery_target: str = "") -> bool:
    lowered = re.sub(r"\s+", "", str(context_text or "").lower())
    if not lowered:
        return False
    has_agent_context = bool(re.search(r"mcagent|rag|本地资料|本地上下文|知识库|localcontext|localevidence", lowered, flags=re.I))
    has_gap_intent = any(term in lowered for term in ("还缺", "缺什么", "缺哪些", "缺口", "缺失", "缺少", "补给", "补齐", "补充", "找补"))
    return (delivery_target == "MCagent/RAG" or has_agent_context) and has_gap_intent


def _literal_gap_meta_query(query: str) -> bool:
    compact = re.sub(r"\s+", "", str(query or "").lower())
    if not compact:
        return False
    return any(
        marker in compact
        for marker in (
            "还缺什么",
            "还缺哪些",
            "缺哪些东西",
            "有哪些缺口",
            "缺少哪些",
            "缺失哪些",
            "缺口列表",
            "缺少模组",
            "缺失模组",
            "待添加",
            "待补充",
            "待实现",
            "开发计划",
        )
    )


def _rebalance_general_web_tasks(tasks: list[dict[str, Any]], *, prefer_general_web: bool) -> None:
    if not prefer_general_web:
        return
    bump = {
        "web_discovery": 140,
        "playwright": 136,
        "browser_collect": 132,
        "modpack_download": 128,
        "followup": 118,
        "fetch_url": 116,
        "modrinth": 96,
        "mcmod": 88,
    }
    for task in tasks:
        source = str(task.get("source") or "")
        if source in bump:
            current = int(task.get("priority") or 0)
            if source in {"mcmod", "modrinth"}:
                task["priority"] = min(current, bump[source])
            else:
                task["priority"] = max(current, bump[source])


def _planner_context_text(question: str, session_summary: dict[str, Any] | None = None) -> str:
    parts = [question]
    if isinstance(session_summary, dict):
        for key in (
            "authoritative_task_goal",
            "task_goal",
            "collection_target",
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


def _context_has_archive_input(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    if re.search(r"[a-zA-Z]:\\[^\r\n]+?\.(?:mrpack|zip)\b", value, flags=re.I):
        return True
    if re.search(r"https?://\S+\.(?:mrpack|zip)\b", value, flags=re.I):
        return True
    return any(
        token in lowered
        for token in (
            ".mrpack",
            ".zip",
            "archive_path",
            "pack_archive",
            "downloaded archive",
            "local archive",
            "downloaded .mrpack",
            "downloaded .zip",
        )
    )


def _modpack_archive_negated(text: str) -> bool:
    value = str(text or "")
    return bool(
        re.search(
        r"不要强行|不要强制|不强行|不强制|不是整合包|如果不是整合包|自行判断.*(?:模组|mod).*整合包|自己判断.*(?:模组|mod).*整合包|模组还是整合包|mod\s+or\s+modpack|if\s+not\s+a\s+modpack|not\s+a\s+modpack|do\s+not\s+force\s+(?:archive|modpack)\s+download|must\s+not\s+force\s+(?:archive|modpack)\s+download|avoid\s+forced\s+(?:archive|modpack)\s+download|avoid\s+forcing\s+(?:archive|modpack)\s+download",
        value,
        flags=re.I,
        )
    )


def _modpack_archive_goal(text: str) -> bool:
    value = str(text or "")
    if _modpack_archive_negated(value):
        return False
    return bool(
        re.search(r"modpack|整合包", value, flags=re.I)
        and re.search(r"download|下载|fully automatically download|公开.*(?:\.mrpack|\.zip)|\.mrpack|\.zip|archive|包体|manifest|modlist|mod list|FTB\s*Quests?|KubeJS", value, flags=re.I)
    )


def _prioritize_modpack_archive_tasks(tasks: list[dict[str, Any]], target: str, context_text: str) -> list[dict[str, Any]]:
    if not _modpack_archive_goal(context_text):
        return tasks
    query = target.strip() or _collection_target_hint(context_text) or _question_subject_hint(context_text) or "Minecraft modpack"
    archive_task = {
        "source": "modpack_download",
        "query": query if re.search(r"modpack|整合包|\.mrpack|\.zip", query, flags=re.I) else f"{query} modpack .mrpack .zip",
        "reason": "User explicitly requires a fully automatic public modpack archive route before internal parsing; collect objective download facts first.",
        "priority": 220,
        "search_limit": 8,
    }
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for task in [archive_task, *tasks]:
        source = str(task.get("source") or "")
        task_query = re.sub(r"\s+", " ", str(task.get("query") or "").strip())
        key = (source, task_query.lower())
        if not source or not task_query or key in seen:
            continue
        seen.add(key)
        output.append(task)
    return output


def _strip_delivery_recipient(text: str) -> str:
    value = str(text).strip()
    value = re.sub(
        r"^\s*(?:请|麻烦|帮我|帮忙)?\s*(?:告诉|叫|让|派|通知)?\s*(?:CrawlerAgent|Crawler|爬虫Agent|爬虫)\s*(?:帮(?:我|你|忙)?|去|来)?\s*(?:收集|采集|获取|抓取|爬取|补充|补库|更新资料)?\s*",
        "",
        value,
        flags=re.I,
    )
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
    subject = _clean_target_hint(target or _collection_target_hint(question) or _question_subject_hint(question), max_len=80)
    queries: list[str] = []
    queries.extend(_known_components(session_summary))
    for hint, hint_queries in GOAL_QUERY_HINTS.items():
        if hint.lower() in text.lower():
            queries.extend(hint_queries)
    if _coverage_allows_item_hint_expansion(text, session_summary):
        for item in ITEM_HINTS:
            if item.lower() in text.lower():
                queries.append(item)
    gap_text = text.lower()
    if any(term in gap_text for term in ("模组列表", "mod list", "modlist", "依赖列表", "dependencies")):
        queries.extend(["模组列表", "mod list", "dependencies"])
    if any(term in gap_text for term in ("任务线", "任务/阶段", "阶段攻略", "任务系统", "ftb quests", "ftb任务")):
        queries.extend(["任务线", "任务系统", "FTB Quests", "阶段攻略"])
    if any(term in gap_text for term in ("新手路线", "新手攻略", "开局攻略", "入门路线")):
        queries.extend(["新手路线", "新手攻略", "开局 攻略"])
    if any(term in gap_text for term in ("玩法", "核心玩法", "特色玩法", "机制说明")):
        queries.extend(["玩法 教程", "核心玩法", "特色机制"])
    if any(term in gap_text for term in ("版本差异", "更新日志", "changelog", "release notes")):
        queries.extend(["更新日志", "版本差异", "changelog"])
    if any(term in gap_text for term in ("官方链接", "下载页", "下载地址", "curseforge", "modrinth")):
        queries.extend(["官方发布页", "下载地址", "CurseForge", "Modrinth"])
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
    if any(re.fullmatch(pattern, compact, flags=re.I) for pattern in GENERIC_TARGET_PATTERNS):
        return False
    if compact.startswith("的相关") or compact.startswith("相关"):
        return False
    if compact in GENERIC_COLLECTION_TERMS:
        return False
    if compact in {"boss", "bosses"}:
        return False
    if re.fullmatch(r"boss\s*(攻略|列表|清单|打法)?", value, flags=re.I):
        return False
    if target and value in {"攻略", "教程", "玩法", "Boss", "BOSS", "boss"}:
        return False
    if _is_gap_analysis_collection_context(context_text, "MCagent/RAG") and _literal_gap_meta_query(value):
        return False
    return True


def _known_components(session_summary: dict[str, Any] | None = None) -> list[str]:
    if not isinstance(session_summary, dict):
        return []
    raw = session_summary.get("known_components")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()][:40]


def _coverage_allows_item_hint_expansion(text: str, session_summary: dict[str, Any] | None = None) -> bool:
    if isinstance(session_summary, dict) and isinstance(session_summary.get("known_components"), list):
        return True
    lowered = str(text or "").lower()
    return any(
        term in lowered
        for term in (
            "known component",
            "known components",
            "component mods",
            "included mods",
            "mod list",
            "modlist",
            "dependencies",
            "依赖",
            "组件",
            "模组列表",
            "包含模组",
        )
    )


def _fallback_plan_with_target(question: str, source_dir: Path, max_tasks: int, planner_error: str = "", session_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    question = _strip_delivery_recipient(question)
    target = _clean_target_hint(_session_target_hint(session_summary) or _collection_target_hint(question) or _question_subject_hint(question) or _general_collection_target_hint(_planner_context_text(question, session_summary)), max_len=80)
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
    modpack_archive_goal = _modpack_archive_goal(context_text)
    archive_negated = _modpack_archive_negated(context_text)
    if isinstance(session_summary, dict):
        output_dir = str(session_summary.get("output_dir") or "").strip()
        fields = session_summary.get("schema_fields") or session_summary.get("fields")
        max_items = session_summary.get("max_items") or 50
        structured_goal = not modpack_archive_goal and (
            bool(output_dir or fields)
            or bool(
                re.search(
                    r"(?:save|保存).{0,24}(?:csv|json|xlsx|xls|excel|table|表格|字段)"
                    r"|(?:csv|json|xlsx|xls|excel|table|表格).{0,24}(?:输出|导出|保存|output|export)"
                    r"|(?:商品|价格).{0,20}(?:链接|link|url)",
                    context_text,
                    flags=re.I,
                )
            )
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
                "cleaning_policy": "Structured browser output with XLSX, CSV, JSON, report, raw HTML, screenshot, and manifest.",
                "requested_by": requested_by,
                "handoff_from": handoff_from,
                "coverage_goals": ["按用户要求采集结构化字段", "保存到指定目录", "遇到登录或验证时保存证据并说明限制"],
                "known_components": _known_components(session_summary),
                "success_criteria": ["保存 XLSX/CSV/JSON/report", "保留来源 URL、raw HTML 和截图", "不绕过登录、验证码或安全验证"],
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
    prefer_external = _prefer_general_web_first(context_text, delivery_target, requested_by)
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
    minecraft_context = _looks_like_minecraft_domain_context(context_text)
    coverage_only_queries = _target_bound_queries(coverage_only_queries, target)
    base_queries = _target_queries(target) if minecraft_context else _general_target_queries(target, context_text)
    queries = [*coverage_only_queries, *base_queries]
    queries = list(dict.fromkeys(query for query in queries if query))
    sources = _default_collection_sources_for_context(context_text, prefer_general_web=prefer_external, archive_negated=archive_negated)
    if modpack_archive_goal:
        sources = ["modpack_download", "modrinth", "web_discovery", "playwright", "fetch_url", "followup", "mcmod"]
    elif archive_negated:
        sources = [source for source in sources if source != "modpack_download"]
    if _is_gap_analysis_collection_context(context_text, delivery_target):
        sources.insert(0, "mcagent_context")
    if any(term in _planner_context_text(question, session_summary) for term in ("完整", "完整资料", "完整数据", "全量", "发现", "未知主题")):
        sources.insert(0, "topic_discovery")
    if any(term.lower() in _planner_context_text(question, session_summary).lower() for term in ("本地安装包", "本地包体", "已下载", "内部文件", "kubejs", "openloader", "modlist", "manifest", "整合包完整")):
        sources.insert(0, "modpack_internal")
    if not _context_has_archive_input(context_text):
        sources = [source for source in sources if source != "modpack_internal"]
    if not _looks_like_minecraft_domain_context(context_text):
        needs_structured_browser = bool(
            re.search(r"xlsx|csv|json|excel|table|表格|字段|结构化|商品|价格|列表", context_text, flags=re.I)
        )
        has_local_path = bool(re.search(r"[A-Za-z]:\\|/[^ \n]+", context_text))
        wants_save_existing_content = bool(re.search(r"save_artifact|保存已有|写入文件|导出", context_text, flags=re.I))
        if not needs_structured_browser:
            sources = [source for source in sources if source != "browser_collect"]
        if not has_local_path:
            sources = [source for source in sources if source not in {"read_local_file", "search_local_files"}]
        if not wants_save_existing_content:
            sources = [source for source in sources if source != "save_artifact"]
    tasks: list[dict[str, Any]] = []
    for source in sources:
        if source == "modpack_download":
            source_queries = [target if re.search(r"modpack|\.mrpack|\.zip|整合包", target, flags=re.I) else f"{target} modpack .mrpack .zip"]
        elif source in {"mcagent_context", "modrinth", "followup", "modpack_internal"}:
            source_queries = [target]
        elif source == "fetch_url":
            source_queries = [query for query in queries[:6] if _is_url_query(query)]
        elif source == "playwright":
            source_queries = queries[:2]
        elif source == "mcmod":
            if prefer_external:
                source_queries = [target, *queries[:2]]
            else:
                source_queries = queries[:12]
        elif prefer_external and source in {"web_discovery"}:
            source_queries = queries[:5]
        else:
            source_queries = queries[:12]
        for offset, query in enumerate(source_queries):
            defaults = SOURCE_DEFAULTS.get(source, {})
            if prefer_external:
                priority_base = {
                    "web_discovery": 126,
                    "playwright": 124,
                    "modpack_download": 120,
                    "fetch_url": 112,
                    "followup": 102,
                    "mcmod": 88,
                    "modrinth": 84,
                    "topic_discovery": 118,
                    "mcagent_context": 150,
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
    tasks = _prioritize_modpack_archive_tasks(tasks, target, context_text)
    is_modpack = bool(re.search(r"整合包|modpack", context_text, flags=re.I))
    if not _looks_like_minecraft_domain_context(context_text):
        minecraft_coverage_goals = []
    else:
        minecraft_coverage_goals = [
            "基本信息、别名和简介",
            "官方链接、下载页和社区链接",
            "整合包模组列表或依赖列表",
            "新手路线、核心玩法和教程",
            "关键系统、物品、配方、Boss、已知问题",
        ]
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
        "coverage_goals": minecraft_coverage_goals
        or [
            "identify the target entity, aliases, official names, and source ecosystem",
            "collect authoritative/project/docs/repository/package-index pages when available",
            "preserve source URLs, titles, metadata, raw text or raw HTML when available",
            "record blocked, empty, duplicate, and off-topic routes for CrawlerAgent review",
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


def _planner_client() -> tuple[OpenAICompatibleClient, str]:
    config = load_config()
    return client_for_agent(config, "crawler_agent", temperature=0.0, timeout_seconds=90)


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
    if not str(text or "").strip():
        raise ValueError("planner output was empty; cannot repair JSON")
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
    for key in (
        "search_limit",
        "max_urls",
        "mods",
        "modpacks",
        "resourcepacks",
        "shaders",
        "search_depth",
        "max_items",
        "output_dir",
        "start_url",
        "timeout_ms",
        "fields",
        "content",
        "format",
        "artifact_format",
        "path",
        "output_path",
        "filename",
        "overwrite",
        "metadata",
        "content_ref",
        "artifact_ref",
    ):
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
        "fetch_url": max(2, max_tasks // 5),
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
        if source in {"modpack_download", "modpack_internal"}:
            add(task)
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
    target_hint = _clean_target_hint(_session_target_hint(session_summary) or _collection_target_hint(question) or _question_subject_hint(question) or _general_collection_target_hint(_planner_context_text(question, session_summary)), max_len=80)
    topic = str(raw.get("topic") or "").strip()
    if target_hint and (not topic or _looks_like_agent_target(topic) or _looks_like_broken_target(topic)):
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
    if coverage_queries and (target_hint or topic):
        coverage_queries = _target_bound_queries(coverage_queries, target_hint or topic)
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
        if not (_query_is_delivery_target(query, target_hint))
        and not _generic_standalone_query(query)
        and _valid_coverage_query(query, target_hint or topic, _planner_context_text(question, session_summary))
    ][:16]

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list):
        raw_sources = []
    context_text = _planner_context_text(question, session_summary)
    archive_negated = _modpack_archive_negated(context_text)
    minecraft_context = _looks_like_minecraft_domain_context(context_text)
    prefer_general_defaults = _prefer_general_web_first(context_text, delivery_target, str((session_summary or {}).get("requested_by") or ""))
    sources = [str(source).strip() for source in raw_sources if str(source).strip() in ALLOWED_SOURCES]
    if not sources:
        sources = _default_collection_sources_for_context(context_text, prefer_general_web=prefer_general_defaults, archive_negated=archive_negated)
    elif not minecraft_context:
        sources = [source for source in sources if source not in MINECRAFT_DOMAIN_SOURCES]
        if not sources:
            sources = _default_collection_sources_for_context(context_text, prefer_general_web=True, archive_negated=True)
    modpack_context = "\n".join([context_text, package_type, topic, target_hint])
    looks_like_modpack_collection = bool(re.search(r"modpack|整合包", modpack_context, flags=re.I))
    if looks_like_modpack_collection and not archive_negated and "modpack_download" not in sources:
        sources.append("modpack_download")
    if "mcmod" in sources:
        for source in ("fetch_url", "web_discovery", "playwright", "modpack_download"):
            if source == "modpack_download" and archive_negated:
                continue
            if source not in sources:
                sources.append(source)
    if _is_gap_analysis_collection_context(context_text, str(raw.get("delivery_target") or "")) and "mcagent_context" not in sources:
        sources.insert(0, "mcagent_context")

    tasks: list[dict[str, Any]] = []
    raw_tasks = raw.get("tasks")
    if isinstance(raw_tasks, list):
        for index, raw_task in enumerate(raw_tasks):
            if not isinstance(raw_task, dict):
                continue
            raw_task = dict(raw_task)
            raw_task["query"] = _clean_task_query(str(raw_task.get("query") or ""), target_hint=target_hint, topic=topic)
            if target_hint:
                raw_query = str(raw_task.get("query") or "").strip()
                if _looks_like_broken_target(raw_query):
                    raw_task["query"] = target_hint
                    raw_query = target_hint
                if raw_query and not _is_url_query(raw_query) and not _query_mentions_target(raw_query, target_hint):
                    raw_task["query"] = _bind_query_to_target(raw_query, target_hint)
            task = _normalize_task(raw_task, str(raw.get("reason") or ""), 80 - index)
            if task:
                if not minecraft_context and str(task.get("source") or "") in MINECRAFT_DOMAIN_SOURCES:
                    continue
                query = str(task.get("query") or "").strip()
                if _is_url_query(query) and not _task_url_is_grounded(task, query=query):
                    task = _downgrade_ungrounded_url_task(task, target=target_hint or topic)
                    query = str(task.get("query") or "").strip()
                if target_hint and _looks_like_broken_target(query):
                    task["query"] = target_hint
                    query = target_hint
                if target_hint and query and not _is_url_query(query) and not _query_mentions_target(query, target_hint):
                    task["query"] = _bind_query_to_target(query, target_hint)
                    query = str(task.get("query") or "").strip()
                if not _is_url_query(query) and not _valid_coverage_query(query, target_hint or topic, context_text):
                    continue
                tasks.append(task)
    if coverage_queries and len(tasks) < max_tasks:
        for offset, query in enumerate(coverage_queries[: max(0, max_tasks - len(tasks))]):
            supplemental = _task(
                "web_discovery" if _prefer_general_web_first(_planner_context_text(question, session_summary), delivery_target, str((session_summary or {}).get("requested_by") or "")) else "mcmod",
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
        if source == "save_artifact":
            continue
        if source in {"mcagent_context", "modrinth", "followup", "mediawiki", "ftbwiki", "createwiki", "modpack_download"}:
            source_queries = [topic or queries[0]]
        elif source == "fetch_url":
            source_queries = [query for query in queries[:4] if "http://" in query.lower() or "https://" in query.lower()]
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
    _rebalance_general_web_tasks(
        tasks,
        prefer_general_web=_prefer_general_web_first(
            context_text,
            delivery_target,
            str((session_summary or {}).get("requested_by") or ""),
        ),
    )
    tasks.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    tasks = _prioritize_modpack_archive_tasks(tasks, target_hint or topic, context_text)
    if not tasks:
        fallback = plan_crawler_tasks(question, source_dir, max_tasks=max_tasks, include_completed=True)
        tasks = list(fallback.get("tasks") or [])
    return {
        "question": question,
        "strategy": str(raw.get("strategy") or "crawler_llm_planner"),
        "planner_model": raw.get("_planner_model", ""),
        "planner_recovered_from_error": str(raw.get("planner_recovered_from_error") or ""),
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
    target_hint = _clean_target_hint(
        _session_target_hint(session_summary)
        or _collection_target_hint(question)
        or _question_subject_hint(question)
        or _general_collection_target_hint(_planner_context_text(question, session_summary)),
        max_len=80,
    )
    learned_memory = _crawler_memory_digest(limit=8)
    schema = {
        "topic": "short target entity, e.g. a product, paper, website, Minecraft modpack, company, dataset, or local file target",
        "package_type": "general|document|product|site|repository|dataset|modpack|mod|item|guide|unknown",
        "coverage_goals": [
            "basic info and aliases",
            "official/download/community links",
            "official/project/docs/repository/package-index pages",
            "downloads/files/assets/changelog/release pages when relevant",
            "structured fields, tables, or local files requested by the user",
            "known issues, blockers, mirrors, and source quality notes",
        ],
        "subqueries": ["short search phrase, not the full user sentence"],
        "sources": ["mcagent_context", "modpack_internal", "modpack_download", "browser_collect", "fetch_url", "save_artifact", "read_local_file", "search_local_files", "mcmod", "modrinth", "web_discovery", "followup", "playwright"],
        "tasks": [
            {"source": "mcagent_context", "query": "topic or question for MCagent/RAG local context", "reason": "ask MCagent/RAG what local evidence and gaps exist before collecting", "priority": 150},
            {"source": "browser_collect", "query": "short task/query", "reason": "why this source/query", "priority": 120, "output_dir": "optional user requested folder", "max_items": 50, "fields": ["name", "price", "url"]},
            {"source": "fetch_url", "query": "https://example.com/page", "reason": "fetch this exact public URL with local HTTP and parse readable text", "priority": 125},
            {"source": "save_artifact", "query": "short artifact purpose", "reason": "why this content should be saved", "priority": 110, "content": "text/object/list to save or omit when using content_ref", "content_ref": "optional artifact id such as latest or r1.1", "format": "md", "path": "optional file or directory path", "filename": "optional file name"},
            {"source": "read_local_file", "query": "read a specific local file", "reason": "inspect a known local file path", "priority": 108, "path": "absolute or workspace-relative path"},
            {"source": "search_local_files", "query": "terms to search locally", "reason": "find relevant local files before reading/saving", "priority": 106, "path": "directory or file path"},
        ],
        "success_criteria": [
            "save markdown plus manifest",
            "preserve source URLs and raw HTML when the fetcher supports it",
            "content is chunkable and citeable when the caller is an RAG agent",
        ],
        "delivery_target": "MCagent RAG|human|both|unknown",
        "cleaning_policy": "RAG-oriented markdown chunks with source URL, title, metadata, raw_html path, dedupe fingerprint",
        "reason": "planning rationale",
    }
    tool_catalog = crawler_collection_catalog_prompt()
    prompt = {
        "role": "user",
        "content": (
            "You are CrawlerAgent. Plan crawler tool actions only; do not answer the user.\n"
            "Participants: human user, MCagent, CrawlerAgent. Preserve caller and delivery target, but do not treat MCagent/RAG/ingest as search topics.\n"
            "Use this shared Agent Runtime tool catalog as capability context, not as keyword triggers:\n"
            f"{tool_catalog}\n"
            "Decide target entity, target ecosystem, coverage goals, short source-specific queries, and ordered tasks. Tools execute after your JSON plan.\n"
            "The authoritative collection target is the current handoff/task goal. Prior current_topic/topics are background memory only; never let old session topics override collection_target/task_goal.\n"
            "CrawlerAgent is a general-purpose crawler. Minecraft is only one optional domain toolset. For non-Minecraft targets, do not choose mcmod, modrinth, mediawiki, ftbwiki, createwiki, modpack_download, or modpack_internal.\n"
            "Think in capability groups: general discovery/search (web_discovery), exact URL fetch (fetch_url), browser rendering or structured extraction (playwright/browser_collect), local file inspection (read_local_file/search_local_files), artifact persistence (save_artifact), then domain-specific tools only when the source ecosystem calls for them.\n"
            "When a handoff says 'what is missing / 还缺哪些 / 缺口' for MCagent/RAG, treat it as local coverage-gap analysis: read mcagent_gap_summary/gaps, turn each gap into positive coverage queries (mod list, quest line, boss guide, beginner route, changelog, download page), and then collect evidence. Do not search literal phrases like '缺少模组', '还缺什么', or '待添加' unless the user explicitly asks for roadmap/future features/community wish lists.\n"
            "Use mcagent_context when CrawlerAgent needs to ask MCagent what local evidence/gaps exist. This sends an inter-agent message to MCagent; MCagent then uses its own local RAG/evidence workflow and replies to CrawlerAgent. It is not a web search provider and not direct database access by CrawlerAgent.\n"
            "For general data collection tasks that ask for structured fields and a save location, use browser_collect. It can open a browser, collect item rows, and save XLSX/CSV/JSON/report to output_dir. Keep the user's requested output_dir exactly.\n"
            "Use fetch_url for exact public URL extraction when local HTTP plus readable-text parsing is enough. It does not require hosted extraction APIs. If fetch_url fails because the page needs rendering, then choose Playwright/browser tools.\n"
            "Use save_artifact when the useful content is already in the task context, generated by the agent, or available as an artifact_ref/content_ref from earlier objective tool output. It accepts content or content_ref, format, path/filename, overwrite, and metadata; do not use it as a substitute for web extraction.\n"
            "Research method: avoid broad keyword blasting. First identify the target entity, aliases, language variants, official names, version scope, and likely source ecosystem. Then build a source graph: official/project pages, docs, repositories, package indexes, download/file pages, dependency/relation pages, changelogs/releases, wiki pages, forum posts, video indexes, and community mirrors. Use broad discovery only to find candidate source nodes, then crawl exact URLs or source-specific pages directly.\n"
            "Do not invent exact URLs, repository names, Modrinth/CurseForge slugs, or organization names. A direct URL task is valid only if the URL came from the user, MCagent context, a previous objective tool result, an artifact_ref/content_ref, or a discovered candidate/search result. If you only infer a likely slug, make a web_discovery or playwright search task for the target plus slug/domain instead of a fetch_url/direct URL task.\n"
            "When a source is empty, duplicate, blocked, or off-topic, change the source class or graph node instead of repeating similar generic searches. Examples of source-class changes: HTTP fetch -> Playwright rendering; public search -> exact project URL; project page -> files/dependencies/changelog page; web page -> repository README/releases; local context -> targeted public collection.\n"
            "For full Minecraft modpack collection, cover: basic info, official/download/community links, mod list, quests/beginner route, key systems, items/recipes/acquisition, bosses, tutorials, known issues.\n"
            "If local archive or manifest exists, use modpack_internal first. It extracts manifest, modlist, FTB Quests, KubeJS, OpenLoader/data, recipes, config, raw text. Then use mcmod/modrinth/public web to fill gaps.\n"
            "If no local archive exists, first discover official/download/project pages. Use modpack_download to look for public .mrpack/.zip archives and save them locally; after a real archive is downloaded, use modpack_internal. Use Modrinth API style discovery first: exact alias search with project_type:modpack, then inspect project versions and files.url for primary .mrpack files. Use CurseForge only when a public/API file page exposes an objective downloadUrl or direct file download; if it needs API key/login/Cloudflare/captcha, record that blocker and change route. Use GitHub Releases by finding release assets/browser_download_url, and packwiz repositories by finding pack.toml/index.toml plus releases or package manifests. Use Playwright/topic_discovery to find public download pages and preserve their HTML. Do not pretend the pack internals are available until an archive/manifest is actually downloaded or provided.\n"
            "For Chinese community packs, check public install guides and official/server sites too. A useful route may be: public guide page -> small installer or metadata page -> text endpoint -> final public release .zip. Do not accept the cloud-drive page itself; accept only if objective tool output shows a no-login direct archive URL with HTTP status/content-type/size plus downloaded zip validation.\n"
            "When teaching yourself an archive route, record how you found it: aliases tried, source graph node, search query or source-specific endpoint, candidate URL, HTTP status/redirect/content-type/filename/size when available, and why you trust or reject it. Tools may expose these objective facts, but CrawlerAgent must decide relevance, keep/delete/retry, and whether modpack_internal is now allowed.\n"
            "Quark, Baidu, 123pan, client-only cloud drives, paywalls, login pages, and captcha pages are not fully automatic unless a direct public .mrpack/.zip URL is visible without manual user action. Treat them as blocked evidence, not as downloaded archives.\n"
            "For hard-to-find Minecraft modpacks, include at least one public archive/download route early: modpack_download for the target/aliases, plus web_discovery/playwright for Chinese forum/mirror/download pages. If ordinary pages fail, switch to package archive discovery instead of repeating wiki/mod-list searches.\n"
            "Playwright is a first-class local browser collection tool, not only a last fallback. Use it when API search/extract is empty, quota-limited, blocked, JS-rendered, or when you need to preserve page HTML after normal readers lose tables, tabs, images, or download links.\n"
            "When recent results are empty/off-topic, move Playwright or topic_discovery earlier instead of repeating the same path. Browser-rendered evidence is often better for Chinese modpack pages, tabs, images, and download links.\n"
            "Queries must be short. Do not use the whole user sentence as a query. Component/system queries may omit the parent pack name when context confirms membership; later validation judges relevance.\n"
            "For MCagent/RAG delivery, require Markdown, manifest, stable title, source URL/internal path, metadata, dedupe key, raw_html/raw_text where available.\n"
            "Your final plan should show this method through task ordering: identity/context tasks first when needed, authoritative exact-source tasks next, then targeted gap-filling searches, and only then broad fallback discovery.\n"
            "General sources: browser_collect, fetch_url, save_artifact, read_local_file, search_local_files, web_discovery, playwright.\n"
            "Inter-agent source for MCagent/RAG handoff context: mcagent_context.\n"
            "Minecraft-domain sources only when the target is Minecraft/MC/modpack related: modpack_internal, modpack_download, mcmod, modrinth, followup, mediawiki, ftbwiki, createwiki.\n"
            "Available sources: mcagent_context, modpack_internal, modpack_download, browser_collect, fetch_url, save_artifact, read_local_file, search_local_files, mcmod, modrinth, followup, web_discovery, playwright, mediawiki, ftbwiki, createwiki.\n"
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
        first_error = f"{type(exc).__name__}: {exc}"
        try:
            return _quick_recovery_plan_with_llm(
                question,
                source_dir,
                max_tasks=max_tasks,
                session_summary=session_summary,
                planner_error=first_error,
            )
        except Exception as recovery_exc:  # noqa: BLE001
            fallback_error = f"{first_error}; quick recovery failed: {type(recovery_exc).__name__}: {recovery_exc}"
        fallback = _fallback_plan_with_target(question, source_dir, max_tasks, planner_error=fallback_error, session_summary=session_summary)
        fallback.setdefault("strategy", "rule_fallback_after_llm_planner_error")
        return fallback


def _quick_recovery_plan_with_llm(
    question: str,
    source_dir: Path,
    *,
    max_tasks: int,
    session_summary: dict[str, Any] | None,
    planner_error: str,
) -> dict[str, Any]:
    client, label = _planner_client()
    context = _planner_context_text(question, session_summary)
    target = _clean_target_hint(_session_target_hint(session_summary) or _collection_target_hint(question) or _question_subject_hint(question), max_len=80)
    prompt = (
        "You are CrawlerAgent. The previous long planner prompt failed. Produce a small executable JSON plan only.\n"
        "Do not answer the user. Do not use literal meta queries like 'what is missing'. Convert gaps into positive coverage tasks.\n"
        "Use the crawler research method: identify the entity and aliases, choose authoritative source nodes, prefer exact URLs/source-specific pages, then use targeted searches only for remaining gaps.\n"
        "Do not invent exact URLs or slugs. If a URL was not supplied by the user or discovered by an objective previous result, search for the target plus domain/slug with web_discovery or playwright first.\n"
        "Allowed sources: mcagent_context, fetch_url, browser_collect, web_discovery, playwright, mcmod, modrinth, modpack_download, modpack_internal, followup, save_artifact, read_local_file, search_local_files.\n"
        "For a request that says Crawler should ask MCagent what is missing and then collect, task 1 must be mcagent_context. Later tasks should collect concrete public evidence.\n"
        "Return JSON with: topic, delivery_target, coverage_goals, tasks. Each task has source, query, reason, priority.\n"
        f"Previous planner error: {planner_error}\n"
        f"Question: {question}\n"
        f"Target hint: {target}\n"
        f"Session summary: {json.dumps(session_summary or {}, ensure_ascii=False)[:2500]}\n"
        f"Context: {context[:2000]}\n"
    )
    raw_text = client.chat(
        [
            {"role": "system", "content": "Return valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=1800,
    )
    raw = _json_from_text(raw_text)
    raw["_planner_model"] = label
    raw["strategy"] = "quick_recovery_llm_plan_after_planner_error"
    raw["planner_recovered_from_error"] = planner_error
    return _sanitize_plan(raw, question, source_dir, max_tasks, session_summary=session_summary)


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
    snapshot = CrawlerReflectionSnapshotService().build(plan=plan, task_results=task_results, pending_tasks=pending_tasks)
    compact_results = list(snapshot["recent_results"])
    learned_memory = _crawler_memory_digest(limit=8)
    compact_pending = list(snapshot["pending_tasks"])
    context_text = _planner_context_text(question, session_summary)
    target_for_archive = str(plan.get("target_hint") or plan.get("topic") or _session_target_hint(session_summary) or _collection_target_hint(question) or _question_subject_hint(question) or "").strip()
    if _modpack_archive_goal(context_text) and not any(str(result.get("source") or "") == "modpack_download" for result in task_results):
        for offset, task in enumerate(compact_pending):
            if str(task.get("source") or "") == "modpack_download":
                return CrawlerReflectionDecisionService().normalize(
                    {
                        "action": "execute_pending",
                        "selected_index": offset,
                        "reason": "The user explicitly requires a fully automatic public modpack archive route; CrawlerAgent must run modpack_download before more page-only collection.",
                    },
                    pending_count=len(compact_pending),
                    normalized_tasks=[],
                    planner="archive_goal_guard",
                )
        archive_tasks = _prioritize_modpack_archive_tasks([], target_for_archive, context_text)[:1]
        return CrawlerReflectionDecisionService().normalize(
            {
                "action": "add_tasks",
                "reason": "The archive route is required and no modpack_download task has run yet.",
                "tasks": archive_tasks,
            },
            pending_count=len(compact_pending),
            normalized_tasks=archive_tasks,
            planner="archive_goal_guard",
        )
    if not task_results and _plan_requires_llm_confirmation(plan):
        return _confirm_fallback_plan_first_step(
            question=question,
            plan=plan,
            compact_pending=compact_pending,
            pending_count=len(pending_tasks),
            session_summary=session_summary,
        )
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
            "When collection pressure rises, replan by source graph, not by more generic wording. Identify which graph node is missing: official/project page, docs, repository, package index, download/file page, dependency/relation page, changelog/release page, wiki, forum, video/community index, or local archive. Add tasks for the missing node or switch source class, such as fetch_url -> Playwright, generic search -> exact URL, project page -> dependencies/files/changelog, or web page -> repository README/releases.\n"
            "Do not invent exact URLs, repository names, Modrinth/CurseForge slugs, or organization names during reflection. You may use direct URL tasks only for URLs visible in objective recent_results manifest_preview/artifact_refs, user input, or MCagent context. Otherwise choose web_discovery/playwright to discover candidate URLs first.\n"
            "For MCagent/RAG gap handoffs, 'missing / 还缺 / 缺口' is not itself a web topic. Derive positive coverage goals from mcagent_gap_summary/gaps and search those. Avoid literal meta queries such as '缺少模组', '还缺哪些', '待添加', or '开发计划' unless the user explicitly asked for future roadmap/community requests.\n"
            "Use mcagent_context if the next best step is for CrawlerAgent to ask MCagent for local evidence/gaps or to validate what Crawler should collect. The tool returns MCagent's reply back to CrawlerAgent.\n"
            "If the task is for MCagent/RAG, prefer evidence that is citeable and chunkable; raw HTML support is valuable for hard pages.\n"
            "Playwright is a first-class browser tool. Prefer it when lightweight HTTP fetch cannot read enough text, when a page needs rendering, or when project tabs/download pages need browser HTML.\n"
            "For full modpack collection without a local archive, use modpack_download to find and save public .mrpack/.zip archives, and use Playwright/topic_discovery to inspect project/download pages and preserve download-link HTML. Route order: Modrinth project_type:modpack -> versions files.url .mrpack; CurseForge public/API file pages only when a direct downloadUrl or /files download is objectively visible; GitHub Releases assets/browser_download_url; packwiz pack.toml/index.toml repositories; then forum/community direct links. Use modpack_internal only after a real local archive/manifest is available.\n"
            "For Chinese community packs, do not stop at Quark/Xunlei/123pan blockers. Inspect public install guides, official/server sites, small public installers, and text endpoints that may disclose a final release .zip URL. Accept only objective no-login direct archive evidence with HTTP status/content-type/size and zip validation.\n"
            "If a candidate is Quark/Baidu/123pan/cloud-drive/login/captcha/paywall/client-only, record the blocker and switch source graph nodes. Do not mark it as fully automatic unless an objective direct .mrpack/.zip URL can be downloaded without manual user action.\n"
            "When recent_results show candidate pages or archive URLs, judge them yourself from objective evidence: alias match, page/source title, URL path, extension, redirect, content-type, filename, size, HTTP status, and manifest/download result. The tool does not decide relevance for you.\n"
            "If recent public-page searches are empty/off-topic for a modpack and no accepted evidence exists, escalate to modpack_download and browser-rendered download/forum/mirror discovery before giving up. After a real archive download, choose modpack_internal.\n"
            "If several HTTP/search tasks are empty or off-topic, do not keep cycling similar queries; escalate to browser-rendered collection or finish with a clear blocked/missing-download reason.\n"
            "For structured extraction with requested fields/output directory, choose browser_collect and preserve output_dir/max_items/fields.\n"
            "For an exact public URL, prefer fetch_url before broad search. If fetch_url returns blocked/short/empty output, escalate to Playwright/browser tools.\n"
            "Use save_artifact when the selected next step is local persistence of content already held in the task/context or available through an artifact_ref/content_ref, not when the content still needs to be fetched.\n"
            "Available sources: mcagent_context, modpack_internal, modpack_download, browser_collect, fetch_url, save_artifact, read_local_file, search_local_files, mcmod, modrinth, followup, web_discovery, playwright, mediawiki, ftbwiki, createwiki.\n"
            "Return valid JSON only.\n"
            f"question: {question}\n"
            f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
            f"crawler_memory: {json.dumps(learned_memory, ensure_ascii=False)}\n"
            f"plan: {json.dumps(snapshot['plan'], ensure_ascii=False)}\n"
            f"loop_snapshot: {json.dumps({key: snapshot[key] for key in ('observation_statuses', 'retryable_recent_results', 'pressure')}, ensure_ascii=False)}\n"
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
        tasks: list[dict[str, Any]] = []
        discovered_urls = _urls_from_recent_results(compact_results)
        for raw_task in list(raw.get("tasks") or [])[:max_new_tasks]:
            if isinstance(raw_task, dict):
                task = _normalize_task(raw_task, str(raw.get("reason") or "CrawlerAgent reflection task"), 75)
                task_query = str(task.get("query") or "") if task else ""
                if task and _is_url_query(task_query) and not _task_url_is_grounded(task, query=task_query):
                    if _url_objectively_seen(task_query, discovered_urls):
                        task["from_discovered_candidate"] = True
                        task["objective_evidence"] = "recent_results manifest_preview/artifact_refs contained this URL or same canonical page URL"
                    else:
                        task = _downgrade_ungrounded_url_task(task, target=str(plan.get("target_hint") or plan.get("topic") or ""))
                        task_query = str(task.get("query") or "")
                if task and (
                    _is_url_query(task_query)
                    or _valid_coverage_query(
                        task_query,
                        str(plan.get("target_hint") or plan.get("topic") or ""),
                        _planner_context_text(question, session_summary),
                    )
                ):
                    tasks.append(task)
        _mark_grounded_url_tasks_from_recent_results(tasks, compact_results)
        if _is_gap_analysis_collection_context(_planner_context_text(question, session_summary), str(plan.get("delivery_target") or "")):
            selected_index = raw.get("selected_index")
            try:
                selected_task = compact_pending[int(selected_index)] if raw.get("action") == "execute_pending" else None
            except Exception:
                selected_task = None
            if selected_task and _literal_gap_meta_query(str(selected_task.get("query") or "")):
                target = str(plan.get("target_hint") or plan.get("topic") or _session_target_hint(session_summary) or _collection_target_hint(question) or "").strip()
                replacement_queries = _target_bound_queries(_coverage_queries(question, session_summary, target), target)[:max_new_tasks]
                replacement_tasks = [
                    _task("web_discovery" if index == 0 else "playwright", query, "replace literal gap-meta query with positive coverage collection", 95 - index)
                    for index, query in enumerate(replacement_queries)
                ]
                return CrawlerReflectionDecisionService().normalize(
                    {
                        "action": "add_tasks",
                        "reason": "Pending task treats the local gap question as a literal web topic; replace it with positive coverage queries derived from MCagent/RAG gaps.",
                        "tasks": replacement_tasks,
                    },
                    pending_count=len(compact_pending),
                    normalized_tasks=_select_diverse_tasks(replacement_tasks, max_new_tasks),
                    planner=label,
                )
        return CrawlerReflectionDecisionService().normalize(
            raw,
            pending_count=len(compact_pending),
            normalized_tasks=_select_diverse_tasks(tasks, max_new_tasks),
            planner=label,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "action": "finish",
            "selected_index": 0,
            "reason": f"CrawlerAgent reflection failed, so the executor must not choose another crawler tool on its behalf: {type(exc).__name__}: {exc}",
            "tasks": [],
            "done_summary": (
                "CrawlerAgent could not review the latest objective results because its reflection LLM failed. "
                "No more tools were executed automatically; retry after the model/quota issue is resolved."
            ),
            "planner": "reflection_fallback_after_llm_error",
            "contract": {
                "valid": False,
                "issues": ["reflection_llm_error", "stopped_before_executor_tool_choice"],
                "requires_llm_task_materialization": False,
                "pending_count": len(pending_tasks),
            },
        }


def _plan_requires_llm_confirmation(plan: dict[str, Any]) -> bool:
    strategy = str(plan.get("strategy") or "")
    return strategy not in {"crawler_llm_planner", "quick_recovery_llm_plan_after_planner_error", "topic_discovery_llm_review"}


def _confirm_fallback_plan_first_step(
    *,
    question: str,
    plan: dict[str, Any],
    compact_pending: list[dict[str, Any]],
    pending_count: int,
    session_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        client, label = _planner_client()
        schema = {
            "action": "execute_pending|finish",
            "selected_index": 0,
            "reason": "why this existing pending task is safe and useful, or why no tool should run",
            "done_summary": "only when action=finish",
        }
        prompt = (
            "You are CrawlerAgent. A rule fallback produced candidate crawler tasks because the long planner timed out. "
            "The executor is not allowed to choose a tool for you. Choose exactly one existing pending task to execute, or finish/stop if none should run.\n"
            "Do not add new tasks in this response. Do not answer the user. Output valid JSON only.\n"
            "Use this exact JSON shape: {\"action\":\"execute_pending\",\"selected_index\":0,\"reason\":\"short reason\",\"done_summary\":\"\"}\n"
            "Prefer objective public collection steps. For Minecraft modpacks, do not choose modpack_internal unless a real local archive/path/manifest is visible in the plan or context; choose modpack_download/web_discovery/playwright first when no archive exists.\n"
            "Do not use MCagent/RAG/ingest or the full user request as a search query. Choose the best pending index by source/query quality.\n"
            f"question: {question}\n"
            f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)[:1200]}\n"
            f"plan: {json.dumps(_compact_plan_for_reflection(plan), ensure_ascii=False)}\n"
            f"artifact_refs: {json.dumps(list(plan.get('artifact_refs') or [])[:8], ensure_ascii=False)}\n"
            f"pending_tasks: {json.dumps(compact_pending[:12], ensure_ascii=False)}\n"
            f"JSON schema: {json.dumps(schema, ensure_ascii=False)}"
        )
        raw_text = client.chat(
            [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=900,
        )
        try:
            raw = _json_from_text(raw_text)
        except Exception:
            if not str(raw_text or "").strip():
                raise ValueError("fallback confirmation returned empty JSON")
            raw = _repair_planner_json(client, raw_text, label=label, schema=schema)
        action = str(raw.get("action") or "").strip().lower()
        if action not in {"execute_pending", "finish"}:
            raw["action"] = "execute_pending" if pending_count else "finish"
        raw["tasks"] = []
        return CrawlerReflectionDecisionService().normalize(raw, pending_count=pending_count, normalized_tasks=[], planner=f"{label} fallback-confirmation")
    except Exception as exc:  # noqa: BLE001
        reason = f"CrawlerAgent fallback-plan confirmation failed, so the executor must not choose a crawler tool on its behalf: {type(exc).__name__}: {exc}"
        return {
            "action": "finish",
            "selected_index": 0,
            "reason": reason,
            "tasks": [],
            "done_summary": reason,
            "planner": "fallback_confirmation_after_llm_error",
            "contract": {
                "valid": False,
                "issues": ["fallback_confirmation_llm_error", "stopped_before_executor_tool_choice"],
                "requires_llm_task_materialization": False,
                "pending_count": pending_count,
            },
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
    duplicate_review = result.get("existing_evidence_review") if isinstance(result.get("existing_evidence_review"), dict) else {}
    observation = result.get("observation") if isinstance(result.get("observation"), dict) else classify_crawler_tool_result(result).to_dict()
    return {
        "source": result.get("source"),
        "query": result.get("query"),
        "returncode": result.get("returncode"),
        "observation_status": observation.get("status"),
        "observation_summary": observation.get("summary"),
        "retryable": observation.get("retryable"),
        "suggested_next": observation.get("suggested_next"),
        "records": manifest.get("records"),
        "skipped": manifest.get("skipped"),
        "errors": manifest.get("errors"),
        "matched": validation.get("matched"),
        "validation_reason": validation.get("reason"),
        "crawler_review_action": result.get("crawler_review_action") or validation.get("cleanup_action"),
        "crawler_review_next_action": result.get("crawler_review_next_action") or validation.get("next_action"),
        "rejected_examples": list(validation.get("rejected_examples") or [])[:3],
        "local_gap_summary": result.get("mcagent_gap_summary") if result.get("source") == "mcagent_context" else None,
        "local_source_count": result.get("mcagent_source_count") if result.get("source") == "mcagent_context" else None,
        "reused_existing": reusable.get("matched"),
        "duplicate_review_reason": duplicate_review.get("reason"),
        "duplicate_review_action": duplicate_review.get("cleanup_action"),
        "duplicate_review_next_action": duplicate_review.get("next_action"),
        "empty": bool(result.get("empty_result")),
        "off_topic": bool(result.get("off_topic_result")),
        "uncertain": bool(result.get("uncertain_result")),
        "records_pending_review": bool(result.get("records_pending_review")),
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
    client, label = client_for_agent(fallback_config, "crawler_agent", temperature=0.0, timeout_seconds=120)
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
        "Allowed source values: mcmod, fetch_url, web_discovery, playwright, modpack_download.\n"
        "Choose generic public web/browser routes for broad discovery; choose mcmod only when the candidate is clearly an MC百科 project page or MC百科-specific query. "
        "Use fetch_url only for exact public URLs and web_discovery/playwright for video/community indexes or rendered pages. "
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
            "Allowed sources: mcmod,fetch_url,web_discovery,playwright,modpack_download. Pick useful topics for the target and prefer generic public web/browser routes unless the candidate is clearly MC百科-specific. "
            f"Target: {question}\n"
            f"Candidates: {json.dumps(compact_candidates[:12], ensure_ascii=False)}"
        )
        text = client.chat(
            [
                {"role": "system", "content": "Output only ACCEPT/REJECT lines."},
                {"role": "user", "content": short_prompt},
            ],
            temperature=0.0,
            max_tokens=900,
        )
        if not text.strip():
            text = client.chat(
                [
                    {"role": "system", "content": "Output only ACCEPT/REJECT lines."},
                    {"role": "user", "content": short_prompt},
                ],
                temperature=0.0,
                max_tokens=900,
            )
    if not text.strip():
        short_prompt = (
            "Output only ACCEPT lines. Format: ACCEPT|source|query|reason. "
            "Allowed sources: mcmod,fetch_url,web_discovery,playwright,modpack_download. Prefer generic public web/browser routes unless the candidate is clearly MC百科-specific. "
            f"Target: {question}\n"
            f"Candidates: {json.dumps(compact_candidates[:10], ensure_ascii=False)}"
        )
        text = client.chat(
            [
                {"role": "system", "content": "Output only ACCEPT lines."},
                {"role": "user", "content": short_prompt},
            ],
            temperature=0.0,
            max_tokens=600,
        )
    tasks: list[dict[str, Any]] = []
    accepted_topics: list[str] = []
    rejected_topics: list[dict[str, str]] = []
    allowed_sources = {"mcmod", "fetch_url", "web_discovery", "playwright", "modpack_download"}
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
                source = "web_discovery"
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
