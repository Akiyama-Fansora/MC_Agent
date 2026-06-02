from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from .config import load_config
from .llm import OpenAICompatibleClient
from .llm_profiles import client_for_agent
from .query_intent import analyze_query
from .crawler_planner import CONCEPTS


@dataclass(slots=True)
class RetrievalPlan:
    topic: str
    subqueries: list[str] = field(default_factory=list)
    required_terms: list[str] = field(default_factory=list)
    optional_terms: list[str] = field(default_factory=list)
    negative_terms: list[str] = field(default_factory=list)
    reason: str = ""
    planner: str = "fallback"

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "subqueries": self.subqueries,
            "required_terms": self.required_terms,
            "optional_terms": self.optional_terms,
            "negative_terms": self.negative_terms,
            "reason": self.reason,
            "planner": self.planner,
        }


QUESTION_WORDS = {
    "什么", "哪些", "有什么", "有哪些", "怎么", "怎样", "如何", "为啥", "为什么",
    "介绍", "讲讲", "详细", "一下", "这个", "那个", "这些", "它们", "上述", "刚才",
    "玩法", "攻略", "教程", "流程", "合成", "配方", "获取", "获得", "制作", "推荐",
    "新手", "萌新", "入门", "开局", "前期", "中期", "后期", "该", "应该",
    "是什么", "有什么用", "用法", "作用", "里的", "里面", "中",
    "可以打", "怎么打", "哪里打", "掉落什么",
    "minecraft", "mc", "mod", "mods", "modpack",
    "archive", "source", "manifest", "version", "versions", "loader", "loaders", "forge", "fabric", "neoforge",
    "quest", "quests", "chapter", "chapters", "kubejs",
    "for", "the", "from", "local", "evidence", "answer", "what", "exists", "internal", "and", "or",
}

INTENT_EXPANSIONS = {
    "怎么玩": ["玩法", "攻略", "教程", "流程"],
    "玩法": ["玩法", "攻略", "教程"],
    "攻略": ["攻略", "教程", "流程"],
    "新手": ["新手", "萌新", "入门", "开局", "前期"],
    "萌新": ["新手", "萌新", "入门", "开局"],
    "合成": ["合成", "配方", "制作", "获取"],
    "配方": ["配方", "合成", "制作"],
    "获取": ["获取", "获得", "来源", "掉落"],
    "获得": ["获取", "获得", "来源", "掉落"],
    "有哪些": ["列表", "大全", "一览"],
    "Boss": ["Boss", "BOSS", "首领", "打法", "掉落", "位置"],
    "BOSS": ["Boss", "BOSS", "首领", "打法", "掉落", "位置"],
    "boss": ["Boss", "BOSS", "首领", "打法", "掉落", "位置"],
    "首领": ["Boss", "首领", "打法", "掉落", "位置"],
    "推荐": ["推荐", "搭配", "配置"],
}


def plan_retrieval(
    question: str,
    *,
    session_summary: dict[str, Any] | None = None,
    max_queries: int = 8,
    use_llm: bool = True,
) -> RetrievalPlan:
    if use_llm and _should_use_fast_plan(question, session_summary):
        return _plan_fallback(question, session_summary=session_summary, max_queries=max_queries)
    if use_llm:
        try:
            return _plan_with_llm(question, session_summary=session_summary, max_queries=max_queries)
        except Exception as exc:  # noqa: BLE001
            fallback = _plan_fallback(question, session_summary=session_summary, max_queries=max_queries)
            fallback.reason = f"LLM retrieval planner failed, used fallback: {type(exc).__name__}: {exc}"
            return fallback
    return _plan_fallback(question, session_summary=session_summary, max_queries=max_queries)


def _should_use_fast_plan(question: str, session_summary: dict[str, Any] | None) -> bool:
    if session_summary and _is_followup(question):
        return False
    ascii_terms = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", question)
    if len(ascii_terms) >= 3:
        return True
    if re.search(r"(整合包|modpack|minecraft|loader|version|quest|kubejs|manifest|archive)", question, flags=re.I):
        return True
    return False


def _planner_client() -> tuple[OpenAICompatibleClient, str]:
    config = load_config()
    return client_for_agent(config, "mcagent_rag", temperature=0.0, timeout_seconds=60)


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.S)
    if match:
        stripped = match.group(0)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("retrieval planner did not return a JSON object")
    return value


def _plan_with_llm(question: str, *, session_summary: dict[str, Any] | None, max_queries: int) -> RetrievalPlan:
    client, label = _planner_client()
    fallback = _plan_fallback(question, session_summary=session_summary, max_queries=max_queries).to_dict()
    prompt = (
        "你是 RAG 检索规划器，不回答用户问题，只把问题拆成适合本地资料库检索的短查询。\n"
        "目标：尽量找到本地已有资料；不要把整句原封不动当唯一查询；把主题实体、动作意图、物品名、阶段词拆开组合。\n"
        "如果用户说这些/它们/上述/刚才，要结合会话摘要补全主题。\n"
        "subqueries 应该短，通常 2 到 8 个词；多个物品或 Boss 要逐个拆分。\n"
        "required_terms 是回答必须围绕的实体词；negative_terms 是明显应排除的其他主题。\n"
        "只输出 JSON，不要 Markdown，不要解释隐藏思考。\n"
        f"用户问题: {question}\n"
        f"会话摘要: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
        f"规则兜底计划: {json.dumps(fallback, ensure_ascii=False)}\n"
        "JSON schema: {\"topic\":\"短主题\", \"subqueries\":[\"短查询\"], "
        "\"required_terms\":[\"必须词\"], \"optional_terms\":[\"扩展词\"], "
        "\"negative_terms\":[\"排除词\"], \"reason\":\"一句可展示理由\"}"
    )
    raw_text = client.chat(
        [
            {"role": "system", "content": "只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=1000,
    )
    raw = _json_from_text(raw_text)
    plan = _sanitize_plan(raw, question, planner=label, max_queries=max_queries)
    if not plan.subqueries:
        return _plan_fallback(question, session_summary=session_summary, max_queries=max_queries)
    return plan


def _sanitize_plan(raw: dict[str, Any], question: str, *, planner: str, max_queries: int) -> RetrievalPlan:
    topic = _clean_term(str(raw.get("topic") or "")) or _infer_topic(question)
    subqueries = _string_list(raw.get("subqueries"), max_len=80)
    required_terms = _string_list(raw.get("required_terms"), max_len=40)
    optional_terms = _string_list(raw.get("optional_terms"), max_len=40)
    negative_terms = _string_list(raw.get("negative_terms"), max_len=40)
    merged = _dedupe([*subqueries, *_compose_queries(required_terms, optional_terms), topic, question])
    return RetrievalPlan(
        topic=topic,
        subqueries=merged[:max_queries],
        required_terms=_dedupe(required_terms or [topic])[:8],
        optional_terms=_dedupe(optional_terms)[:16],
        negative_terms=_dedupe(negative_terms)[:8],
        reason=str(raw.get("reason") or "LLM planned retrieval queries.").strip()[:240],
        planner=planner,
    )


def _plan_fallback(question: str, *, session_summary: dict[str, Any] | None, max_queries: int) -> RetrievalPlan:
    intent = analyze_query(question, CONCEPTS)
    relation_terms = _extract_relation_terms(question)
    intent_candidates = _candidate_terms_from_intent(intent.entity, intent.keywords, intent.search_queries)
    terms = _dedupe([*relation_terms, *intent_candidates, *_extract_terms(question)])
    summary_terms = _summary_terms(session_summary or {})
    if _is_followup(question) and summary_terms:
        terms = _dedupe([*summary_terms[:6], *terms])
    intent_terms = _intent_terms(question)
    entities = [term for term in terms if term and term.lower() not in QUESTION_WORDS]
    topic = relation_terms[1] if len(relation_terms) >= 2 else (_preferred_topic_entity(entities, question) or intent.entity or question)
    required = _dedupe([
        *(_relation_required_terms(relation_terms) if relation_terms else []),
        topic,
        *entities[:4],
    ])
    optional = _dedupe([*intent_terms, *entities[1:8]])
    queries = _dedupe([
        *_relation_queries(relation_terms, intent_terms),
        *_compose_queries(required[:3], optional[:8]),
        *entities[:6],
        *intent.search_queries[:4],
        question,
    ])
    return RetrievalPlan(
        topic=topic,
        subqueries=queries[:max_queries],
        required_terms=required[:8],
        optional_terms=optional[:16],
        negative_terms=[],
        reason="Fallback query decomposition from entity and intent terms.",
        planner="fallback",
    )


def _candidate_terms_from_intent(entity: str, keywords: list[str], search_queries: list[str]) -> list[str]:
    output: list[str] = []
    for item in [entity, *keywords, *search_queries]:
        value = _clean_term(str(item))
        if not value:
            continue
        output.extend(_extract_relation_terms(value))
        output.extend(_extract_terms(value))
        if _looks_like_atomic_term(value):
            output.append(value)
    return _dedupe(output)


def _preferred_topic_entity(entities: list[str], question: str) -> str:
    generic = {str(item).lower() for item in QUESTION_WORDS}
    for term in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", question):
        lowered = term.lower()
        if lowered not in generic:
            return term
    for entity in entities:
        if entity.lower() not in generic:
            return entity
    return ""


def _looks_like_atomic_term(value: str) -> bool:
    if any(mark in value for mark in "？?，,。；;：:！! "):
        return False
    if any(word in value for word in QUESTION_WORDS):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,20}", value))


def _relation_required_terms(relation_terms: list[str]) -> list[str]:
    if len(relation_terms) < 2:
        return relation_terms
    parent, child = relation_terms[0], relation_terms[1]
    return [child, parent, f"{parent} {child}"]


def _extract_terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", text)
    terms: list[str] = []
    for item in raw:
        value = item.strip()
        if not value:
            continue
        split = _split_cjk_phrase(value)
        terms.extend(split or [value])
    return _dedupe(terms)


def _extract_relation_terms(text: str) -> list[str]:
    terms: list[str] = []
    patterns = [
        r"(?P<parent>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})(?:里面的|里面|里的|里|中的|中)(?P<child>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})",
        r"(?P<parent>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})\s+(?P<child>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            parent = _clean_relation_term(match.group("parent"))
            child = _clean_relation_term(match.group("child"))
            if parent and child and parent != child:
                terms.extend([parent, child, f"{parent} {child}"])
    return _dedupe(terms)


def _clean_relation_term(value: str) -> str:
    value = _clean_term(value)
    value = re.sub(r"^(?:的|之|里面的|里面|里的|里|中的|中)+", "", value)
    value = re.sub(r"(是什么|有什么用|有哪些用法|有哪些|有什么|怎么|如何|玩法|攻略|教程)$", "", value)
    value = re.sub(r"^(这个|那个|这些|上述|刚才)", "", value)
    value = _clean_term(value)
    parts = [part for part in _split_cjk_phrase(value) if part not in QUESTION_WORDS]
    if parts:
        return parts[-1]
    return "" if value in QUESTION_WORDS else value


def _relation_queries(terms: list[str], intent_terms: list[str]) -> list[str]:
    if len(terms) < 2:
        return []
    parent = terms[0]
    child = terms[1]
    queries = [child, f"{parent} {child}"]
    for intent in intent_terms[:4]:
        queries.append(f"{child} {intent}")
        queries.append(f"{parent} {child} {intent}")
    return queries


def _split_cjk_phrase(value: str) -> list[str]:
    if not re.fullmatch(r"[\u4e00-\u9fff]{3,}", value):
        return []
    working = value
    found: list[str] = []
    for word in sorted(QUESTION_WORDS | set(INTENT_EXPANSIONS), key=len, reverse=True):
        if word in working:
            found.append(word)
            working = working.replace(word, " ")
    for part in re.findall(r"[\u4e00-\u9fff]{2,}", working):
        found.append(part)
    return _dedupe([item for item in found if item not in {"该", "应该"}])


def _intent_terms(question: str) -> list[str]:
    terms: list[str] = []
    for key, values in INTENT_EXPANSIONS.items():
        if key in question:
            terms.extend(values)
    return _dedupe(terms)


def _compose_queries(required: list[str], optional: list[str]) -> list[str]:
    queries: list[str] = []
    for entity in required:
        if not entity:
            continue
        queries.append(entity)
        for term in optional[:6]:
            if term and term != entity and term not in entity:
                queries.append(f"{entity} {term}")
    return queries


def _summary_terms(summary: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for key in ("topics", "entities"):
        value = summary.get(key)
        if isinstance(value, list):
            output.extend(str(item) for item in value[:12])
    return _dedupe([_clean_term(item) for item in output if item])


def _is_followup(question: str) -> bool:
    return any(token in question for token in ("这些", "它们", "上述", "刚才", "这个", "那个", "如何", "怎么", "合成", "配方", "Boss", "BOSS", "boss", "首领", "掉落", "哪里"))


def _infer_topic(question: str) -> str:
    terms = [term for term in _extract_terms(question) if term not in QUESTION_WORDS]
    return terms[0] if terms else question[:40]


def _string_list(value: Any, *, max_len: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe([_clean_term(str(item)) for item in value if _clean_term(str(item)) and len(_clean_term(str(item))) <= max_len])


def _clean_term(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n，,。；;：:？?！!\"'")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = _clean_term(str(item))
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output
