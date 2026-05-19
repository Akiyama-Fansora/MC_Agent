from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(slots=True)
class QueryIntent:
    question: str
    domain: str
    question_type: str
    entity: str
    keywords: list[str] = field(default_factory=list)
    preferred_sources: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    confidence: float = 0.5
    reason: str = ""
    concept: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "domain": self.domain,
            "question_type": self.question_type,
            "entity": self.entity,
            "keywords": self.keywords,
            "preferred_sources": self.preferred_sources,
            "search_queries": self.search_queries,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "concept": self.concept or {},
        }


VANILLA_TERMS = {
    "原版",
    "难度",
    "合成",
    "指令",
    "命令",
    "生物群系",
    "生物",
    "村民",
    "下界",
    "末地",
    "红石",
    "附魔",
    "酿造",
    "维度",
    "方块",
    "物品",
    "创造模式",
    "生存模式",
    "冒险模式",
    "旁观模式",
}

PROJECT_TERMS = {
    "mod",
    "mods",
    "modpack",
    "模组",
    "整合包",
    "资源包",
    "材质包",
    "光影",
    "拔刀剑",
    "落幕曲",
    "乌托邦",
    "暮色森林",
    "暮色",
    "shader",
    "forge",
    "fabric",
    "quilt",
    "curseforge",
    "modrinth",
}

PROJECT_ANCHORS = tuple(
    sorted(
        [
            term
            for term in PROJECT_TERMS
            if re.fullmatch(r"[\u4e00-\u9fff]{2,}", term)
            and term not in {"模组", "整合包", "资源包", "材质包", "光影"}
        ],
        key=len,
        reverse=True,
    )
)

DOMAIN_SYNONYMS = {
    "拔刀剑": ["SlashBlade", "Slash Blade", "slashblade"],
}

GUIDE_MODIFIERS = (
    "新手",
    "萌新",
    "入门",
    "开局",
    "前期",
    "中期",
    "后期",
    "该",
    "应该",
)

GUIDE_QUERY_TERMS = (
    "新手",
    "萌新",
    "入门",
    "开局",
    "攻略",
    "玩法",
    "教程",
)

QUESTION_PATTERNS = {
    "list": ("有哪些", "包含哪些", "包括什么", "列出", "列表", "所有"),
    "guide": ("怎么玩", "玩法", "攻略", "流程", "详细介绍", "介绍一下", "讲讲"),
    "recipe": ("合成", "配方", "怎么做", "制作", "如何合成", "如何制作"),
    "mechanic": ("机制", "原理", "怎么用", "有什么用", "如何"),
    "boss": ("boss", "Boss", "BOSS", "首领"),
}

STOP_PHRASES = (
    "详细介绍一下",
    "详细介绍",
    "介绍一下",
    "介绍",
    "讲讲",
    "请问",
    "一下",
    "呢",
    "有哪些玩法",
    "有什么玩法",
    "有什么好玩的",
    "怎么玩",
    "玩法",
    "攻略",
    "流程",
    "如何合成",
    "如何制作",
    "怎么合成",
    "怎么制作",
    "怎么做",
    "合成",
    "配方",
    "制作",
    "有哪些",
    "是什么",
    "内容",
    "boss",
    "Boss",
    "BOSS",
    "首领",
)


def analyze_query(question: str, concepts: list[dict[str, Any]] | None = None) -> QueryIntent:
    text = question.strip()
    lowered = text.lower()
    concepts = concepts or []
    concept = _match_concept(lowered, concepts)
    question_type = _question_type(text)
    topic_anchor = _project_anchor_from_question(text)
    entity = topic_anchor or _entity_from_question(text)
    keywords = _keywords(text, entity)

    if concept:
        entity = str(concept.get("canonical") or entity)
        sources = _concept_sources(concept)
        queries = [str(task[1]) for task in concept.get("tasks", []) if len(task) >= 2]
        return QueryIntent(
            question=text,
            domain="known_mod",
            question_type=question_type,
            entity=entity,
            keywords=_dedupe([entity, *keywords]),
            preferred_sources=sources,
            search_queries=_dedupe(queries or [entity]),
            confidence=0.9,
            reason="命中已知 MC 模组/专题概念。",
            concept=concept,
        )

    has_project_term = bool(topic_anchor) or any(term in lowered for term in PROJECT_TERMS)
    has_vanilla_term = any(term in text for term in VANILLA_TERMS)
    has_non_vanilla_entity = bool(entity and entity not in VANILLA_TERMS and not any(term in entity for term in VANILLA_TERMS))

    if has_project_term or (has_non_vanilla_entity and question_type in {"guide", "list", "boss", "mechanic", "unknown"} and not has_vanilla_term):
        guide_queries = _guide_search_queries(entity, text, question_type)
        guide_keywords = _guide_keywords(text, question_type)
        queries = _dedupe([entity, *_synonyms_for(entity), text, *guide_queries, *keywords, *[syn for keyword in keywords for syn in _synonyms_for(keyword)]])
        return QueryIntent(
            question=text,
            domain="project",
            question_type=question_type,
            entity=entity or text,
            keywords=_dedupe([entity, *keywords, *guide_keywords]),
            preferred_sources=["modrinth", "followup"],
            search_queries=queries,
            confidence=0.72 if entity else 0.6,
            reason="问题像某个模组/整合包/资源包项目，需要先做项目发现，再跟进公开文档。",
        )

    if has_vanilla_term:
        return QueryIntent(
            question=text,
            domain="vanilla",
            question_type=question_type,
            entity=entity or text,
            keywords=keywords,
            preferred_sources=["mediawiki"],
            search_queries=[text],
            confidence=0.78,
            reason="问题包含原版 Minecraft 机制/物品/模式线索。",
        )

    return QueryIntent(
        question=text,
        domain="ambiguous",
        question_type=question_type,
        entity=entity or text,
        keywords=keywords,
        preferred_sources=["modrinth", "mediawiki", "followup"],
        search_queries=_dedupe([entity, text, *keywords]),
        confidence=0.45,
        reason="无法确定是原版还是项目，先项目发现，再检查原版 Wiki。",
    )


def _match_concept(lowered: str, concepts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for concept in concepts:
        aliases = concept.get("aliases") or []
        if any(str(alias).lower() in lowered for alias in aliases):
            return concept
    return None


def _concept_sources(concept: dict[str, Any]) -> list[str]:
    sources = [str(concept.get("primary_source") or "")]
    for task in concept.get("tasks", []):
        if len(task) >= 1:
            sources.append(str(task[0]))
    return _dedupe([source for source in sources if source])


def _question_type(question: str) -> str:
    if any(pattern in question for pattern in QUESTION_PATTERNS.get("boss", ())):
        return "boss"
    for name, patterns in QUESTION_PATTERNS.items():
        if name == "boss":
            continue
        if any(pattern in question for pattern in patterns):
            return name
    return "unknown"


def _project_anchor_from_question(question: str) -> str:
    lowered = question.lower()
    for anchor in PROJECT_ANCHORS:
        if anchor.lower() in lowered:
            return anchor
    return ""


def _entity_from_question(question: str) -> str:
    text = question
    for phrase in STOP_PHRASES:
        text = text.replace(phrase, " ")
    for phrase in GUIDE_MODIFIERS:
        text = text.replace(phrase, " ")
    text = _strip_quantity_words(text)
    text = re.sub(r"\s+", " ", text).strip(" ？?，,。.")
    if text:
        return text
    terms = _keywords(question)
    return terms[0] if terms else question.strip()


def _keywords(question: str, entity: str = "") -> list[str]:
    cleaned = question
    for phrase in STOP_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    for phrase in GUIDE_MODIFIERS:
        cleaned = cleaned.replace(phrase, " ")
    cleaned = _strip_quantity_words(cleaned)
    terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", cleaned)
    if entity:
        terms.insert(0, entity)
        terms.extend(_split_entity_terms(entity))
        remainder = cleaned.replace(entity, " ")
        terms.extend(re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", remainder))
    filtered = []
    stop = {phrase.lower() for phrase in STOP_PHRASES}
    stop.update({"minecraft", "mc", "详细", "介绍", "一下", "什么", "哪些"})
    for term in terms:
        value = term.strip()
        if not value:
            continue
        if value.lower() in stop:
            continue
        if any(value in phrase or phrase in value for phrase in STOP_PHRASES if len(value) <= 2):
            continue
        filtered.append(value)
    return _dedupe(filtered)


def _guide_keywords(question: str, question_type: str) -> list[str]:
    if question_type not in {"guide", "mechanic", "unknown"}:
        return []
    hits = [term for term in GUIDE_QUERY_TERMS if term in question]
    if any(term in question for term in ("怎么玩", "如何玩", "玩法")):
        hits.extend(["玩法", "攻略"])
    if any(term in question for term in ("新手", "萌新", "入门", "开局")):
        hits.extend(["新手", "萌新", "入门", "开局"])
    return _dedupe(hits)


def _guide_search_queries(entity: str, question: str, question_type: str) -> list[str]:
    entity = entity.strip()
    if not entity or question_type not in {"guide", "mechanic", "unknown"}:
        return []
    guide_terms = _guide_keywords(question, question_type) or ["攻略", "玩法"]
    queries = [f"{entity} {term}" for term in guide_terms[:5]]
    if any(term in question for term in ("新手", "萌新", "入门")):
        queries.append(f"{entity} 新手 攻略")
        queries.append(f"{entity} 萌新 攻略")
    if any(term in question for term in ("怎么玩", "玩法")):
        queries.append(f"{entity} 玩法 攻略")
    return _dedupe(queries)


def _split_entity_terms(entity: str) -> list[str]:
    parts: list[str] = []
    known_suffixes = ("拔刀剑", "整合包", "模组", "资源包", "材质包", "光影", "维度", "世界")
    for suffix in known_suffixes:
        if entity.endswith(suffix) and entity != suffix:
            prefix = entity[: -len(suffix)].strip()
            if prefix:
                parts.extend([prefix, suffix])
    ascii_parts = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}", entity)
    parts.extend(ascii_parts)
    return parts


def _strip_quantity_words(text: str) -> str:
    text = re.sub(r"(?:前|top\s*)\d+\s*(?:个|项|条|种)?", " ", text, flags=re.I)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\d+\s*(?:个|项|条|种)?", " ", text)
    text = re.sub(r"\b\d+\s*(?:个|项|条|种)?\b", " ", text)
    return text


def _synonyms_for(value: str) -> list[str]:
    synonyms: list[str] = []
    lowered = value.lower()
    for key, items in DOMAIN_SYNONYMS.items():
        if key.lower() in lowered:
            synonyms.extend(items)
    return synonyms


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output
