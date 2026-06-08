from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .llm import OpenAICompatibleClient


@dataclass(slots=True)
class CrawlerModelPriorService:
    """Build CrawlerAgent's model-prior hypotheses for planning only.

    The prior is deliberately not evidence. It is a hypothesis map that helps
    Crawler choose better tools and queries before objective collection starts.
    """

    client: OpenAICompatibleClient | None = None
    model_label: str = ""

    def build(
        self,
        *,
        question: str,
        target_hint: str,
        context_text: str,
        session_summary: dict[str, Any] | None = None,
        learned_memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = self.rule_prior(
            question=question,
            target_hint=target_hint,
            context_text=context_text,
            session_summary=session_summary,
            learned_memory=learned_memory,
        )
        if not self.client:
            return fallback
        prompt = self._prompt(
            question=question,
            target_hint=target_hint,
            context_text=context_text,
            session_summary=session_summary,
            learned_memory=learned_memory,
            fallback=fallback,
        )
        try:
            text = self.client.chat(
                [
                    {"role": "system", "content": "Return valid JSON only. Do not claim unverified facts as evidence."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=900,
                response_format={"type": "json_object"},
            )
            raw = json.loads(_first_json_object(text))
        except Exception as exc:  # noqa: BLE001 - prior must never block planning.
            fallback["error"] = f"{type(exc).__name__}: {exc}"
            return fallback
        prior = self._normalize(raw, fallback=fallback)
        prior["planner"] = self.model_label or "crawler_model_prior"
        return prior

    def rule_prior(
        self,
        *,
        question: str,
        target_hint: str,
        context_text: str,
        session_summary: dict[str, Any] | None = None,
        learned_memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = "\n".join(
            [
                str(question or ""),
                str(target_hint or ""),
                str(context_text or ""),
                _current_session_prior_text(session_summary),
            ]
        )
        target = str(target_hint or "").strip() or _short_subject(question)
        alias_text = "\n".join([str(question or ""), target])
        aliases = _aliases_for_text(target, alias_text)
        source_graph = _source_graph_for_text(text)
        lead_aliases = aliases[:4] or ([target] if target else [])
        leads: list[str] = list(lead_aliases)
        for alias in lead_aliases:
            if not alias:
                continue
            leads.extend(query for query in _lead_queries_for_alias(alias, text) if query != alias)
        source_specific = _source_specific_leads_for_aliases(lead_aliases, text)
        candidate_urls = _candidate_urls_for_source_leads(source_specific)
        return self._normalize(
            {
                "target": target,
                "aliases": aliases,
                "likely_source_graph": source_graph,
                "search_leads": leads,
                "source_specific_leads": source_specific,
                "candidate_urls": candidate_urls,
                "verification_questions": _verification_questions_for_text(target, text),
                "risk_notes": [
                    "Model prior is unverified and may be wrong; use it only to choose tools and queries.",
                    "Do not ingest or cite prior claims unless an objective tool result verifies them.",
                ],
                "planner": "rule_prior",
                "evidence_status": "hypothesis_only",
            },
            fallback={},
        )

    def _prompt(
        self,
        *,
        question: str,
        target_hint: str,
        context_text: str,
        session_summary: dict[str, Any] | None,
        learned_memory: dict[str, Any] | None,
        fallback: dict[str, Any],
    ) -> str:
        return (
            "You are CrawlerAgent before tool execution. Use your model knowledge only as unverified planning prior.\n"
            "The prior may suggest aliases, source ecosystems, likely docs/wiki/project pages, and verification questions.\n"
            "It must not become evidence, citations, accepted sources, or RAG ingest content until objective tools verify it.\n"
            "Return JSON with keys: target, aliases, likely_source_graph, search_leads, source_specific_leads, candidate_urls, verification_questions, risk_notes.\n"
            "Keep every list short and actionable. Search leads must be short query strings, not long sentences.\n"
            "source_specific_leads may include unverified source-targeted hints such as 'modrinth: project-slug' or 'github: owner/repo'. candidate_urls may include likely public project URLs, but they are still hypotheses until fetched.\n"
            f"Question: {question}\n"
            f"Target hint: {target_hint}\n"
            f"Context: {context_text[:1600]}\n"
            f"Session summary: {json.dumps(session_summary or {}, ensure_ascii=False, default=str)[:1200]}\n"
            f"Learned memory: {json.dumps(learned_memory or {}, ensure_ascii=False, default=str)[:900]}\n"
            f"Rule fallback prior: {json.dumps(fallback, ensure_ascii=False, default=str)[:900]}"
        )

    def _normalize(self, raw: dict[str, Any], *, fallback: dict[str, Any]) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        target = _clean_text(raw.get("target") or fallback.get("target") or "", limit=120)
        aliases = _clean_list(_merged_list(raw.get("aliases"), fallback.get("aliases")), limit=8, item_limit=80)
        source_graph = _clean_list(_merged_list(raw.get("likely_source_graph"), fallback.get("likely_source_graph")), limit=10, item_limit=120)
        search_leads = _clean_list(_merged_list(raw.get("search_leads"), fallback.get("search_leads")), limit=14, item_limit=100)
        source_specific = _clean_list(_merged_list(raw.get("source_specific_leads"), fallback.get("source_specific_leads")), limit=10, item_limit=160)
        candidate_urls = _clean_list(_merged_list(raw.get("candidate_urls"), fallback.get("candidate_urls")), limit=10, item_limit=240)
        verification = _clean_list(_merged_list(raw.get("verification_questions"), fallback.get("verification_questions")), limit=10, item_limit=140)
        risks = _clean_list(_merged_list(raw.get("risk_notes"), fallback.get("risk_notes")), limit=8, item_limit=160)
        return {
            "target": target,
            "aliases": aliases,
            "likely_source_graph": source_graph,
            "search_leads": search_leads,
            "source_specific_leads": source_specific,
            "candidate_urls": candidate_urls,
            "verification_questions": verification,
            "risk_notes": risks,
            "evidence_status": "hypothesis_only",
            "allowed_use": "planning_only",
            "forbidden_use": "Do not cite, ingest, or mark as accepted evidence until objective tools verify it.",
            "planner": str(raw.get("planner") or fallback.get("planner") or "crawler_model_prior"),
        }


def _first_json_object(text: str) -> str:
    value = str(text or "").strip()
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end >= start:
        return value[start : end + 1]
    return value


def _clean_text(value: Any, *, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _merged_list(primary: Any, fallback: Any) -> list[Any]:
    output: list[Any] = []
    for value in (primary, fallback):
        if isinstance(value, list):
            output.extend(value)
        elif isinstance(value, str) and value.strip():
            output.append(value)
    return output


def _clean_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []
    output: list[str] = []
    for item in items:
        text = _clean_text(item, limit=item_limit)
        if _blocked_prior_item(text):
            continue
        if text and text.lower() not in {existing.lower() for existing in output}:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _blocked_prior_item(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    compact = re.sub(r"[^a-z0-9_/]+", "_", text.lower()).strip("_")
    blocked = {
        "received_agent_message",
        "agent_message",
        "from_agent",
        "to_agent",
        "from_agent_id",
        "to_agent_id",
        "message_id",
        "reply_to",
        "requires_reply",
        "metadata",
        "selected_action_plan",
        "step",
        "tool",
        "goal",
        "delivery_target",
        "requested_by",
        "task_goal",
        "collection_target",
    }
    if compact in blocked:
        return True
    if re.fullmatch(r"(?:from|to|message|metadata|tool|step|goal)(?:[_ -]agent|[_ -]id)?", compact):
        return True
    return False


def _short_subject(question: str) -> str:
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    text = re.sub(r"^(?:请|帮我|让|叫|CrawlerAgent|Crawler|采集|收集|获取|补充|整理)\s*", "", text, flags=re.I)
    return text[:80]


def _current_session_prior_text(session_summary: dict[str, Any] | None) -> str:
    if not isinstance(session_summary, dict):
        return ""
    parts: list[str] = []
    for key in ("authoritative_task_goal", "task_goal", "collection_target", "original_user_message", "original_question", "source_question", "mcagent_gap_summary"):
        value = str(session_summary.get(key) or "").strip()
        if value:
            parts.append(value[:400])
    return "\n".join(parts)[:1200]


def _aliases_for_text(target: str, text: str) -> list[str]:
    aliases = [target.strip()] if target.strip() else []
    if re.search(r"农夫乐事|farmer'?s delight", text, flags=re.I):
        aliases.extend(["Farmer's Delight", "农夫乐事"])
    if re.search(r"乌托邦|utopian journey|utopia journey", text, flags=re.I):
        aliases.extend(["乌托邦探险之旅", "Utopian Journey", "Utopia Journey"])
    if re.search(r"create|机械动力", text, flags=re.I):
        aliases.extend(["Create", "机械动力"])
    quoted = re.findall(r"[\"'“‘《「『]([^\"'”’》」』]{2,80})[\"'”’》」』]", text)
    aliases.extend(quoted[:4])
    blocked = {
        "delivery_target",
        "requested_by",
        "task_goal",
        "collection_target",
        "mcagent/rag",
        "user",
        "mcagent",
        "crawleragent",
        "crawler",
    }
    return [alias for alias in _clean_list(aliases, limit=12, item_limit=80) if alias.lower() not in blocked][:8]


def _source_graph_for_text(text: str) -> list[str]:
    lowered = str(text or "").lower()
    graph = ["official/project page", "documentation/wiki", "repository/readme", "package index", "changelog/release notes"]
    if re.search(r"minecraft|mc|整合包|模组|modpack|mod|curseforge|modrinth|mc百科", text, flags=re.I):
        graph.extend(["MC百科 page", "Modrinth project/files", "CurseForge project/files", "community forum/guide", "download/archive route"])
    if any(term in lowered for term in ("tutorial", "guide", "beginner", "progression", "教程", "攻略", "新手", "入门", "玩法")):
        graph.extend(["beginner guide", "tutorial/progression page", "community walkthrough"])
    if re.search(r"https?://", text):
        graph.insert(0, "user-provided exact URL")
    return _clean_list(graph, limit=10, item_limit=120)


def _lead_queries_for_alias(alias: str, text: str) -> list[str]:
    lowered = str(text or "").lower()
    leads = [alias]
    if re.search(r"minecraft|mc|整合包|模组|modpack|mod", text, flags=re.I):
        leads.extend([f"{alias} MC百科", f"{alias} Modrinth", f"{alias} CurseForge"])
    if any(term in lowered for term in ("tutorial", "guide", "beginner", "progression", "教程", "攻略", "新手", "入门", "玩法")):
        leads.extend([f"{alias} wiki", f"{alias} guide", f"{alias} beginner guide", f"{alias} tutorial", f"{alias} 玩法 攻略"])
    if any(term in lowered for term in ("download", "archive", ".mrpack", ".zip", "下载", "包体")):
        leads.extend([f"{alias} download", f"{alias} .mrpack .zip"])
    return leads


def _slug_candidates(alias: str) -> list[str]:
    text = re.sub(r"\s*/\s*", " ", str(alias or ""))
    parts = re.findall(r"[A-Za-z][A-Za-z0-9'’_.-]*(?:\s+[A-Za-z][A-Za-z0-9'’_.-]*)*", text)
    candidates: list[str] = []
    for part in parts:
        if re.search(r"\b(?:MCagent|MCAgent|CrawlerAgent|Crawler|RAG|manifest|config|download|version)\b", part, flags=re.I):
            continue
        cleaned = part.replace("’", "'")
        cleaned = re.sub(r"'s\b", "s", cleaned, flags=re.I)
        slug = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")
        if 3 <= len(slug) <= 80 and slug not in {"minecraft", "modpack", "mod", "guide", "wiki"}:
            candidates.append(slug)
    return _clean_list(candidates, limit=4, item_limit=80)


def _source_specific_leads_for_aliases(aliases: list[str], text: str) -> list[str]:
    if not re.search(r"minecraft|mc|整合包|模组|modpack|mod|curseforge|modrinth|mc百科", text, flags=re.I):
        return []
    leads: list[str] = []
    for alias in aliases[:4]:
        for slug in _slug_candidates(alias):
            leads.extend(
                [
                    f"modrinth: {slug}",
                    f"curseforge: {slug}",
                    f"github: {slug}",
                ]
            )
    return _clean_list(leads, limit=10, item_limit=160)


def _candidate_urls_for_source_leads(source_specific_leads: list[str]) -> list[str]:
    urls: list[str] = []
    for lead in source_specific_leads:
        if ":" not in lead:
            continue
        source, value = [part.strip() for part in lead.split(":", 1)]
        source = source.lower()
        slug = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")
        if not slug:
            continue
        if source == "modrinth":
            urls.append(f"https://modrinth.com/mod/{slug}")
            urls.append(f"https://www.minecraft-guides.com/wiki/{slug}/")
        elif source == "curseforge":
            urls.append(f"https://www.curseforge.com/minecraft/mc-mods/{slug}")
        elif source == "github":
            urls.append(f"https://github.com/search?q={slug}+minecraft+mod")
    return _clean_list(urls, limit=10, item_limit=240)


def _verification_questions_for_text(target: str, text: str) -> list[str]:
    target_label = target or "target"
    questions = [
        f"What official/project page verifies {target_label}?",
        f"What source verifies aliases and current scope for {target_label}?",
    ]
    if re.search(r"minecraft|mc|整合包|模组|modpack|mod", text, flags=re.I):
        questions.extend(
            [
                "Which page verifies loader/game version and project type?",
                "Which page verifies mod list, dependencies, or files?",
                "Which source verifies beginner/progression gameplay claims?",
            ]
        )
    return questions
