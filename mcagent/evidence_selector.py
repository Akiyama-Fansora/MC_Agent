from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable

from .crawler_planner import CONCEPTS
from .query_intent import QueryIntent, analyze_query
from .retrieval_planner import RetrievalPlan
from .schema import SearchResult


@dataclass(slots=True)
class EvidenceReport:
    verdict: str
    topic_detected: str
    confidence: float
    selected_count: int
    candidate_count: int
    reasons: list[str] = field(default_factory=list)
    final_context_k: int = 8

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "topic_detected": self.topic_detected,
            "confidence": round(self.confidence, 3),
            "selected_count": self.selected_count,
            "candidate_count": self.candidate_count,
            "reasons": self.reasons,
            "final_context_k": self.final_context_k,
        }


class EvidenceSelector:
    """Select answer-grade evidence from rough retriever candidates.

    The retriever is allowed to be broad. This selector is deliberately strict:
    if the top candidates are source-polluted or only loosely related, MCagent
    should delegate to Crawler instead of asking the model to improvise.
    """

    def __init__(self, final_context_k: int = 8) -> None:
        self.final_context_k = final_context_k

    def select(self, question: str, candidates: Iterable[SearchResult], plan: RetrievalPlan | None = None) -> tuple[list[SearchResult], EvidenceReport]:
        rows = list(candidates)
        intent = analyze_query(question, CONCEPTS)
        scoring_question = self._question_with_plan_terms(question, plan)
        topic = self._topic(question, intent)
        scored = [(self._score(scoring_question, topic, item, intent), item) for item in rows]
        scored.sort(key=lambda item: (item[0], item[1].score), reverse=True)

        threshold = 0.25 if topic == "vanilla" else 0.38
        selected = [item for score, item in scored if score >= threshold]
        selected = self._dedupe(selected)[: self.final_context_k]
        for index, item in enumerate(selected, start=1):
            item.rank = index

        confidence = 0.0
        if scored:
            confidence = max(0.0, min(1.0, scored[0][0]))
        min_needed = self._min_needed(question, topic)
        reasons: list[str] = []
        if not selected:
            reasons.append("没有通过主题/来源门控的证据。")
        elif len(selected) < min_needed:
            reasons.append(f"证据数量不足：需要至少 {min_needed} 条，通过 {len(selected)} 条。")
        if topic != "vanilla" and not any(self._strong_source(topic, item) for item in selected):
            reasons.append(f"缺少 {topic} 对应的强来源。")
        if topic in {"modrinth_topic", "project"} and not self._allows_body_only_project_evidence(question, intent):
            title_match = any(self._title_matches_question(scoring_question, item.title, intent) for item in selected)
            component_match = any(self._title_matches_component_focus(scoring_question, item.title) for item in selected)
            body_match = any(self._body_matches_question_focus(scoring_question, item, intent) for item in selected)
            if not title_match and not component_match and not body_match:
                reasons.append("没有找到标题匹配用户主题的模组/整合包资料。")
        if self._is_list_question(question) and len(selected) < min_needed:
            reasons.append("列表类问题需要更完整的来源，避免模型只凭少量碎片回答。")

        verdict = "ok" if not reasons else "insufficient"
        return selected, EvidenceReport(
            verdict=verdict,
            topic_detected=topic,
            confidence=confidence,
            selected_count=len(selected),
            candidate_count=len(rows),
            reasons=reasons,
            final_context_k=self.final_context_k,
        )

    def _question_with_plan_terms(self, question: str, plan: RetrievalPlan | None) -> str:
        if plan is None:
            return question
        terms = [question, plan.topic, *plan.required_terms, *plan.optional_terms[:6], *plan.subqueries[:6]]
        return " ".join(_dedupe_strings([str(term) for term in terms if str(term).strip()]))

    def _topic(self, question: str, intent: QueryIntent | None = None) -> str:
        if intent and intent.domain == "project":
            return "project"
        if intent and intent.domain == "vanilla":
            return "vanilla"
        lower = question.lower()
        if any(token in lower for token in ("机械动力", "create")):
            return "create"
        if any(token in lower for token in ("暮色", "twilight", "naga", "hydra", "ur-ghast", "lich")):
            return "twilight_forest"
        if any(token in lower for token in ("蜜蜂世界", "蜜蜂维度", "蜂巢世界", "bumblezone")):
            return "bumblezone"
        if "乌托邦" in lower or "utopia" in lower:
            return "utopia"
        if any(token in lower for token in ("应用能源", "ae2", "applied energistics")):
            return "ae2"
        if any(token in lower for token in ("通用机械", "mekanism")):
            return "mekanism"
        if any(token in lower for token in ("植物魔法", "botania")):
            return "botania"
        if any(token in lower for token in ("整合包", "模组", "modpack", " mod", "mods")):
            return "modrinth_topic"
        return "vanilla"

    def _score(self, question: str, topic: str, item: SearchResult, intent: QueryIntent | None = None) -> float:
        title = item.title.lower()
        path = item.source_path.lower().replace("\\", "/")
        text = item.text.lower()
        score = min(float(item.score) / 1.25, 0.55)

        if topic == "create":
            score += self._contains_any(path, ("crawler_exports/createwiki/",)) * 0.45
            score += self._contains_any(title, ("mechanical", "create", "contraption", "train", "belt", "press", "mixer", "crushing", "fan")) * 0.20
            score -= (1 if "modrinth_agent" in path and "/modpack_" in path else 0) * 0.30
            score -= self._contains_any(title, ("creative", "nanbin", "defaultgamemode")) * 0.20
        elif topic == "twilight_forest":
            score += self._contains_any(path, ("crawler_exports/ftbwiki/",)) * 0.45
            score += self._contains_any(title, ("naga", "hydra", "lich", "ur-ghast", "snow queen", "minoshroom", "knight phantom", "twilight forest")) * 0.25
            score -= self._contains_any(title, ("recipe", "sound fix", "crafting", "villages", "origins fix")) * 0.25
            score -= self._contains_any(path, ("resourcepack_", "modpack_")) * 0.20
        elif topic == "bumblezone":
            score += self._contains_any(title + " " + path, ("bumblezone", "bee dimension", "蜂")) * 0.35
            score += (1 if ("modrinth_agent" in path and "/mod_the-bumblezone" in path) or "ftbwiki" in path else 0) * 0.20
            score -= self._contains_any(path, ("modpack_", "resourcepack_", "shader_")) * 0.15
        elif topic == "utopia":
            score += self._contains_any(title, ("utopia", "乌托邦")) * 0.35
            score += self._contains_any(text, ("included mods", "included files", "modpack contents")) * 0.35
            score += (1 if "modrinth_agent" in path and "/modpack_" in path else 0) * 0.20
            score -= self._contains_any(path, ("resourcepack_", "shader_", "mod_")) * 0.25
        elif topic == "modrinth_topic":
            score += (1 if ("modrinth_agent" in path or "crawler_exports/mcmod/" in path) else 0) * 0.20
            score += self._question_term_overlap(question, title) * 0.45
            score += self._question_term_overlap(question, text[:1200]) * 0.12
            score -= (1 if "mediawiki" in path else 0) * 0.20
        elif topic == "project":
            entity = (intent.entity if intent else "") or question
            keywords = list(intent.keywords if intent else [])
            score += (1 if "modrinth_agent" in path else 0) * 0.35
            score += (1 if "crawler_exports/mcmod/" in path else 0) * 0.42
            score += (1 if "crawler_exports/manual_research/" in path else 0) * 0.50
            score += self._contains_any(path, ("pack_internal", "pack_internals", "pack_high_signal", "ftbquests", "kubejs", "openloader", "raw_text")) * 0.30
            score += self._contains_any(path, ("crawler_exports/followup/", "crawler_exports/web_discovery/", "github", "readme")) * 0.24
            score += self._question_term_overlap(entity, title) * 0.50
            score += self._question_term_overlap(" ".join(keywords), title + "\n" + text[:1600]) * 0.20
            score -= (1 if "crawler_exports/mediawiki/" in path else 0) * 0.65
        elif topic == "ae2":
            score += self._contains_any(path, ("crawler_exports/ftbwiki/", "modrinth_agent")) * 0.35
            score += self._contains_any(title, ("applied energistics", "ae2", "me system", "channel", "storage", "autocrafting")) * 0.22
        elif topic == "mekanism":
            score += self._contains_any(path, ("crawler_exports/ftbwiki/", "modrinth_agent")) * 0.35
            score += self._contains_any(title, ("mekanism", "reactor", "turbine", "boiler", "factory", "infuser", "crusher")) * 0.22
        elif topic == "botania":
            score += self._contains_any(path, ("crawler_exports/ftbwiki/", "modrinth_agent")) * 0.35
            score += self._contains_any(title, ("botania", "mana", "terrasteel", "alfheim", "rune", "flower")) * 0.22
        else:
            score += self._contains_any(path, ("crawler_exports/mediawiki/",)) * 0.25
            score += self._question_term_overlap(question, title + "\n" + text) * 0.20

        return max(0.0, min(1.0, score))

    def _strong_source(self, topic: str, item: SearchResult) -> bool:
        path = item.source_path.lower().replace("\\", "/")
        text = item.text.lower()
        if topic == "create":
            return "crawler_exports/createwiki/" in path
        if topic == "twilight_forest":
            return "crawler_exports/ftbwiki/" in path
        if topic == "utopia":
            return "modrinth_agent" in path and "/modpack_" in path and ("included mods" in text or "modpack contents" in text)
        if topic == "bumblezone":
            return "bumblezone" in path or "bumblezone" in item.title.lower()
        if topic == "modrinth_topic":
            return ("modrinth_agent" in path or "crawler_exports/mcmod/" in path) and self._question_term_overlap(item.title, item.title + "\n" + item.text[:1200]) > 0
        if topic == "project":
            strong_channels = (
                "modrinth_agent",
                "crawler_exports/mcmod/",
                "crawler_exports/manual_research/",
                "crawler_exports/followup/",
                "crawler_exports/web_discovery/",
                "crawler_exports/fetch_url/",
                "crawler_exports/playwright/",
            )
            haystack = item.title.lower() + "\n" + item.text[:1800].lower()
            has_topic_overlap = (
                self._question_term_overlap(item.title, haystack) > 0
                or self._question_term_overlap(item.title, item.source_path.lower()) > 0
                or self._question_term_overlap(" ".join(self._question_terms(item.title)), haystack) > 0
            )
            return (
                any(channel in path for channel in strong_channels)
                and has_topic_overlap
            )
        if topic in {"ae2", "mekanism", "botania"}:
            return "crawler_exports/ftbwiki/" in path or "modrinth_agent" in path
        return True

    def _min_needed(self, question: str, topic: str) -> int:
        if topic == "utopia":
            return 1
        if self._is_list_question(question):
            return 3
        return 2 if topic != "vanilla" else 1

    def _is_list_question(self, question: str) -> bool:
        """Only explicit enumeration requests are list questions.

        Avoid treating operational questions such as "机械动力怎么玩" or
        "Mekanism 这个 mod 怎么玩" as list requests.
        """
        if re.search(r"有哪些(?!.*(?:怎么|如何|怎样))", question):
            return True
        if re.search(r"包含哪些|包括什么|什么.*列表|列出所有|所有.*列表", question):
            return True
        if "有什么" in question and not re.search(r"怎么|如何|怎样|做|合成|打|挖|造|获得", question):
            return True
        if re.search(r"所有.*boss|boss.*列表|哪些.*boss|boss.*有哪些", question, re.I):
            return True
        if re.search(r"有哪些.*(?:模组|mod|整合包|boss|维度|生物|玩法)", question, re.I):
            return True
        return False

    def _dedupe(self, items: list[SearchResult]) -> list[SearchResult]:
        seen: set[str] = set()
        deduped: list[SearchResult] = []
        for item in items:
            key = item.source_path
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _contains_any(self, text: str, needles: tuple[str, ...]) -> int:
        return 1 if any(needle in text for needle in needles) else 0

    def _question_terms(self, question: str, intent: QueryIntent | None = None) -> list[str]:
        raw_terms = re.findall(r"[a-zA-Z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", question)
        if intent:
            raw_terms.extend([intent.entity, *intent.keywords, *intent.search_queries])
        stop = {"minecraft", "mc", "整合包", "怎么玩", "有哪些", "有什么", "什么", "玩法", "介绍", "一下", "模组", "mods", "modpack"}
        terms: list[str] = []
        seen: set[str] = set()
        for term in raw_terms:
            value = term.strip().lower()
            if len(value) < 2 or value in stop or value in seen:
                continue
            seen.add(value)
            terms.append(value)
        return terms

    def _question_term_overlap(self, question: str, haystack: str, intent: QueryIntent | None = None) -> float:
        terms = self._question_terms(question, intent)
        if not terms:
            return 0.0
        haystack = haystack.lower()
        hits = sum(1 for term in terms if term in haystack)
        return min(hits / max(len(terms), 1), 1.0)

    def _title_matches_question(self, question: str, title: str, intent: QueryIntent | None = None) -> bool:
        haystack = title.lower()
        if self._question_term_overlap(question, haystack, intent) >= 0.20:
            return True
        if not intent:
            return False
        return any(term in haystack for term in self._question_terms(question, intent))

    def _body_matches_question_focus(self, question: str, item: SearchResult, intent: QueryIntent | None = None) -> bool:
        haystack = f"{item.title}\n{item.text[:2200]}".lower()
        terms = [term for term in self._question_terms(question, intent) if term not in {"列表", "大全", "一览"}]
        if not terms:
            return False
        hits = [term for term in terms if term in haystack]
        if self._is_list_question(question):
            return len(hits) >= min(2, len(terms)) and any(token in haystack for token in ("boss", "首领", "亚波伦", "下亚", "炎魔"))
        return len(hits) >= min(2, len(terms))

    def _title_matches_component_focus(self, question: str, title: str) -> bool:
        haystack = title.lower()
        for term in self._component_focus_terms(question):
            if term in haystack:
                return True
        return False

    def _component_focus_terms(self, question: str) -> list[str]:
        patterns = [
            r"[\u4e00-\u9fffA-Za-z0-9_+-]{2,40}(?:里面的|里面|里的|里|中的|中)(?P<child>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})",
            r"[\u4e00-\u9fffA-Za-z0-9_+-]{2,40}(?:的|之)(?P<child>[\u4e00-\u9fffA-Za-z0-9_+-]{2,40})",
        ]
        output: list[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, question):
                child = self._clean_component_term(match.group("child"))
                if child:
                    output.append(child.lower())
        return _dedupe_strings(output)

    def _clean_component_term(self, value: str) -> str:
        value = re.sub(r"(是什么|有什么用|有哪些用法|有哪些|有什么|怎么|如何|玩法|攻略|教程)$", "", value)
        value = value.strip(" \t\r\n，,。；;：:？?！!")
        parts = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", value)
        stop = {"什么", "有哪些", "有什么", "用法", "作用", "玩法", "攻略", "教程"}
        parts = [part for part in parts if part not in stop]
        return parts[-1] if parts else ""

    def _allows_body_only_project_evidence(self, question: str, intent: QueryIntent | None = None) -> bool:
        """Allow short item or mechanic lookups to be answered from guide body text."""
        if not intent:
            return False
        if intent.question_type in {"list", "boss"}:
            return False
        if intent.question_type in {"recipe", "mechanic"}:
            return True
        entity = str(intent.entity or question).strip()
        if re.fullmatch(r"[一-鿿]{2,8}", entity):
            return True
        terms = self._question_terms(question, intent)
        return bool(terms) and len(terms) <= 2 and len(question.strip()) <= 16


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = str(item).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output
