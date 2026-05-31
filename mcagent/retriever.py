from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any, Iterable

from .config import AppConfig
from .crawler_planner import CONCEPTS
from .embeddings import make_embedder
from .query_intent import analyze_query
from .retrieval_planner import RetrievalPlan, plan_retrieval
from .schema import SearchResult
from .storage import connect, fetch_chunks_by_ids


# 模块级向量索引缓存，避免每次检索都重新 np.load
_vector_cache: dict[str, tuple[tuple[int, int], Any]] = {}


def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required for vector search. Install it with: pip install -r requirements.txt"
        ) from exc
    return np


QUERY_EXPANSIONS = {
    "难度": "difficulty peaceful easy normal hard",
    "和平": "peaceful difficulty",
    "简单": "easy difficulty",
    "普通": "normal difficulty",
    "困难": "hard difficulty",
    "玩法": "gameplay survival creative adventure hardcore spectator multiplayer",
    "生存": "survival gameplay",
    "创造": "creative gameplay",
    "冒险": "adventure gameplay",
    "旁观": "spectator gameplay",
    "极限": "hardcore gameplay",
    "合成": "crafting recipe crafting table",
    "配方": "crafting recipe",
    "红石": "redstone circuits redstone components",
    "附魔": "enchanting enchantment",
    "酿造": "brewing potion",
    "村民": "villager trading",
    "交易": "trading villager",
    "生物": "mob animal monster",
    "怪物": "monster mob",
    "维度": "dimension nether end overworld",
    "下界": "nether dimension",
    "末地": "end dimension",
    "服务器": "server multiplayer",
    "指令": "commands command",
    "命令": "commands command",
    "机械动力": "create mod automation mechanical power contraption train belt press mixer crushing wheel encased fan deployer",
    "机械力": "create mod automation mechanical power stress rotation shaft cogwheel",
    "create": "create mod automation mechanical power contraption train belt press mixer",
    "火车": "train track schedule station conductor create mod",
    "列车": "train track schedule station conductor create mod",
    "轨道": "train track railway schedule station create mod",
    "暮色": "twilight forest bosses naga lich hydra ur-ghast minoshroom snow queen knight phantom alpha yeti quest ram",
    "暮色森林": "twilight forest bosses naga lich hydra ur-ghast minoshroom snow queen knight phantom alpha yeti quest ram",
    "蜜蜂世界": "bumblezone bee dimension honey crystal queen bee beehemoth",
    "蜜蜂维度": "bumblezone bee dimension honey crystal queen bee beehemoth",
    "蜂巢世界": "bumblezone bee dimension honey crystal queen bee beehemoth",
    "乌托邦": "utopia modpack included mods mrpack fabric forge",
    "应用能源": "applied energistics ae2 storage network autocrafting me system",
    "通用机械": "mekanism machinery ore processing energy gas reactor",
    "植物魔法": "botania mana flowers terrasteel alfheim",
    "拔刀剑": "slashblade katana blade sword japanese sword",
    "落幕曲": "luomuqu slashblade minecraft",
}

GUIDE_QUERY_EXPANSIONS = {
    "新手": "新手 萌新 入门 开局 攻略 教程 玩法 beginner guide tips",
    "萌新": "新手 萌新 入门 开局 攻略 教程 玩法 beginner guide tips",
    "入门": "新手 萌新 入门 开局 攻略 教程 玩法 beginner guide tips",
    "开局": "新手 萌新 入门 开局 攻略 教程 玩法 early game guide",
    "怎么玩": "玩法 攻略 教程 guide tips progression",
}


def _expand_query(query: str) -> str:
    additions = [value for key, value in QUERY_EXPANSIONS.items() if key in query]
    additions.extend(value for key, value in GUIDE_QUERY_EXPANSIONS.items() if key in query)
    ascii_terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", query)
    additions.extend(ascii_terms)
    if not additions:
        return query
    return f"{query} {' '.join(additions)}"


def _tokenize_sparse(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_:+.-]{1,}", text)
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(segment) <= 6:
            tokens.append(segment)
        tokens.extend(segment[index : index + 2] for index in range(max(len(segment) - 1, 0)))
        tokens.extend(segment[index : index + 3] for index in range(max(len(segment) - 2, 0)))
    return tokens


def _important_query_terms(query: str) -> set[str]:
    expanded = _expand_query(query)
    terms = set(_tokenize_sparse(expanded))
    for key in QUERY_EXPANSIONS:
        if key.lower() in query.lower():
            terms.update(_tokenize_sparse(key))
    return {term for term in terms if len(term) >= 2}


def _exact_recall_terms(query: str) -> list[str]:
    terms: list[str] = []
    try:
        intent = analyze_query(query, CONCEPTS)
    except Exception:  # noqa: BLE001 - retrieval should stay available if intent parsing changes.
        intent = None
    if intent:
        terms.extend([intent.entity, *intent.keywords, *intent.search_queries])
        if intent.domain in {"project", "known_mod"}:
            for keyword in intent.keywords:
                if keyword and keyword in query:
                    terms.append(keyword)
            entity = str(intent.entity or "").strip()
            if entity:
                if any(marker in query for marker in ("新手", "萌新", "入门", "开局", "怎么玩", "玩法", "攻略")):
                    terms.extend([
                        entity,
                        f"{entity} 新手",
                        f"{entity} 萌新",
                        f"{entity} 攻略",
                        f"{entity} 玩法",
                        f"{entity} 教程",
                    ])
                for suffix in ("新手", "萌新", "入门", "开局", "前期", "中期", "后期", "该", "应该"):
                    if entity.endswith(suffix) and len(entity) > len(suffix) + 1:
                        terms.append(entity[: -len(suffix)])
    cleaned = re.sub(r"(详细介绍一下|详细介绍|介绍一下|介绍|玩法|怎么玩|有哪些|有什么|攻略|流程)", " ", query)
    for term in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", cleaned):
        terms.append(term)
        for suffix in ("拔刀剑", "整合包", "资源包", "材质包", "光影", "模组", "维度", "世界"):
            if term.endswith(suffix) and term != suffix:
                prefix = term[: -len(suffix)].strip()
                if prefix:
                    terms.append(prefix)
                terms.append(suffix)
    seen: set[str] = set()
    output: list[str] = []
    for term in terms:
        key = term.lower()
        if key in seen or len(term) < 2:
            continue
        seen.add(key)
        output.append(term)
    return output[:8]


def _literal_recall_chunk_ids(conn: Any, query: str, limit: int) -> list[int]:
    terms = _exact_recall_terms(query)
    if not terms:
        return []
    sql = """
        SELECT
            chunks.id AS chunk_id,
            chunks.text AS text,
            documents.title AS title,
            documents.source_path AS source_path
        FROM chunks
        JOIN documents ON documents.id = chunks.document_id
        WHERE {where_clause}
        LIMIT ?
    """
    clauses: list[str] = []
    params: list[str] = []
    for term in terms:
        like = f"%{term}%"
        clauses.append("(documents.title LIKE ? OR documents.source_path LIKE ? OR chunks.text LIKE ?)")
        params.extend([like, like, like])
    rows = conn.execute(sql.format(where_clause=" OR ".join(clauses)), [*params, max(limit, 1)]).fetchall()
    scored: list[tuple[float, int]] = []
    for row in rows:
        haystack = f"{row['title']}\n{row['source_path']}\n{row['text'][:1600]}".lower()
        score = 0.0
        for index, term in enumerate(terms):
            weight = 1.0 if index == 0 else 0.55
            if term.lower() in haystack:
                score += weight
        path = str(row["source_path"]).lower().replace("\\", "/")
        if any(marker in path for marker in ("web_discovery", "modrinth_agent", "mcmod", "followup", "ftbwiki", "createwiki", "manual_research")):
            score += 0.6
        scored.append((score, int(row["chunk_id"])))
    scored.sort(key=lambda item: item[0], reverse=True)
    seen: set[int] = set()
    output: list[int] = []
    for _score, chunk_id in scored:
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        output.append(chunk_id)
        if len(output) >= limit:
            break
    return output


def _is_modpack_fact_query(query: str) -> bool:
    lowered = query.lower()
    return any(
        term in lowered
        for term in (
            "minecraft 版本",
            "mc 版本",
            "game version",
            "minecraft version",
            "loader",
            "modloader",
            "mod loader",
            "forge",
            "fabric",
            "neoforge",
            "sha256",
            "hash",
            "checksum",
            "bytes",
            "content-range",
            "downloaded_archive_evidence",
            "direct_archive_url",
        )
    ) or any(term in query for term in ("版本", "加载器", "模组加载器", "整合包版本", "包体", "来源", "大小", "校验", "哈希", "直链", "下载地址"))


def _modpack_manifest_fact_chunk_ids(conn: Any, query: str, limit: int) -> list[int]:
    if not _is_modpack_fact_query(query):
        return []
    rows = conn.execute(
        """
        SELECT
            chunks.id AS chunk_id,
            chunks.text AS text,
            documents.title AS title,
            documents.source_path AS source_path
        FROM chunks
        JOIN documents ON documents.id = chunks.document_id
        WHERE documents.metadata_json LIKE '%modpack_manifest_facts%'
           OR chunks.text LIKE '%整合包 manifest 结构化事实%'
           OR documents.source_path LIKE '%modpack_manifests%'
           OR documents.source_path LIKE '%modpack_archive_summary%'
           OR chunks.text LIKE '%source: modpack_download_evidence%'
           OR documents.source_path LIKE '%downloaded_archive_evidence%'
        LIMIT 240
        """
    ).fetchall()
    if not rows:
        return []
    query_tokens = set(_tokenize_sparse(query))
    scored: list[tuple[float, int]] = []
    for row in rows:
        haystack = f"{row['title']}\n{row['source_path']}\n{row['text']}".lower()
        row_tokens = set(_tokenize_sparse(haystack))
        overlap = len(query_tokens & row_tokens)
        score = float(overlap)
        if "minecraft 版本" in str(row["text"]) or "minecraft.version" in str(row["text"]).lower():
            score += 4.0
        if "加载器" in str(row["text"]) or "modloaders" in str(row["text"]).lower():
            score += 3.0
        lower_text = str(row["text"]).lower()
        lower_path = str(row["source_path"]).lower().replace("\\", "/")
        if "source: modpack_download_evidence" in lower_text or "downloaded_archive_evidence" in lower_path:
            score += 6.0
        if "sha256:" in lower_text or "direct_archive_url:" in lower_text:
            score += 4.0
        scored.append((score, int(row["chunk_id"])))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk_id for score, chunk_id in scored[: max(limit, 1)] if score > 0]


def _fts_query(terms: list[str]) -> str:
    parts: list[str] = []
    for term in terms:
        cleaned = re.sub(r'["\s]+', " ", term).strip()
        if not cleaned:
            continue
        tokens = _tokenize_sparse(cleaned)
        if not tokens:
            continue
        longest = sorted(set(tokens), key=len, reverse=True)[:4]
        parts.extend(f'"{token}"' for token in longest if len(token) >= 2)
    return " OR ".join(parts[:24])


def _fts_recall_chunk_ids(conn: Any, query: str, limit: int) -> list[int]:
    terms = _exact_recall_terms(query)
    fts_query = _fts_query(terms or [query])
    if not fts_query:
        return []
    try:
        rows = conn.execute(
            """
            SELECT
                rowid AS chunk_id,
                bm25(chunks_fts, 4.0, 2.0, 1.0) AS rank_score
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY rank_score
            LIMIT ?
            """,
            (fts_query, max(limit, 1)),
        ).fetchall()
    except Exception:
        return []
    return [int(row["chunk_id"]) for row in rows]


def _lexical_boost(row: Any, query: str) -> float:
    terms = _important_query_terms(query)
    title = str(row["title"]).lower()
    path = str(row["source_path"]).lower()
    text = str(row["text"]).lower()
    boost = 0.0
    for token in terms:
        if token in title:
            boost += 0.20
        elif token in path:
            boost += 0.10
        elif token in text:
            boost += 0.035
    return min(boost, 0.60)


def _source_intent_boost(row: Any, query: str) -> float:
    try:
        intent = analyze_query(query, CONCEPTS)
    except Exception:  # noqa: BLE001
        intent = None
    if not intent:
        return 0.0
    path = str(row["source_path"]).lower().replace("\\", "/")
    title = str(row["title"]).lower()
    text = str(row["text"][:1600]).lower()
    boost = 0.0
    if intent.domain in {"project", "known_mod"}:
        if "crawler_exports/mediawiki/" in path:
            boost -= 0.85
        recall_hits = 0
        for term in _exact_recall_terms(query):
            lowered = term.lower()
            if lowered and lowered in title:
                recall_hits += 1
                boost += 0.45
            elif lowered and lowered in path:
                recall_hits += 1
                boost += 0.28
            elif lowered and lowered in text:
                recall_hits += 1
                boost += 0.18
        if any(marker in path for marker in ("modrinth_agent", "crawler_exports/mcmod/", "crawler_exports/followup/", "crawler_exports/web_discovery/", "crawler_exports/ftbwiki/", "crawler_exports/createwiki/", "crawler_exports/manual_research/")):
            boost += 0.45 if recall_hits else -0.35
    elif intent.domain == "vanilla":
        if "crawler_exports/mediawiki/" in path:
            boost += 0.25
    return max(-1.0, min(boost, 1.5))


def _source_channel(row: Any) -> str:
    path = str(row["source_path"]).lower().replace("\\", "/")
    filename = path.rsplit("/", 1)[-1]
    if "crawler_exports/" in path and (
        filename.startswith("modpack_archive_summary")
        or filename.startswith("modpack_manifests")
        or filename.startswith("modpack_text_evidence")
        or filename.startswith("modpack_route_evidence")
        or filename.startswith("route_draft")
    ):
        return "pack_internal"
    if "crawler_exports/manual_research/" in path:
        if any(marker in path for marker in ("pack_internal", "pack_internals", "pack_high_signal", "ftbquests", "kubejs", "openloader", "raw_text")):
            return "pack_internal"
        return "manual_research"
    if "crawler_exports/mediawiki/" in path:
        return "mediawiki"
    if "crawler_exports/modrinth_agent/" in path:
        return "modrinth"
    if "crawler_exports/mcmod/" in path:
        return "mcmod"
    if "crawler_exports/followup/" in path:
        return "followup"
    if "crawler_exports/web_discovery/" in path:
        return "web_discovery"
    if "crawler_exports/ftbwiki/" in path:
        return "ftbwiki"
    if "crawler_exports/createwiki/" in path:
        return "createwiki"
    return "other"


def _project_channel_gate(row: Any, query: str, intent: Any | None) -> float:
    if not intent or intent.domain not in {"project", "known_mod"}:
        return 0.0
    channel = _source_channel(row)
    if channel == "mediawiki":
        return -1.25
    haystack = f"{row['title']}\n{row['source_path']}\n{str(row['text'])[:2400]}".lower()
    terms = [term.lower() for term in _exact_recall_terms(query)]
    lexical_hits = sum(1 for term in terms if term and term in haystack)
    if channel == "pack_internal":
        return 1.25 if lexical_hits else 0.15
    if channel == "manual_research":
        return 0.95 if lexical_hits else 0.05
    if channel in {"modrinth", "mcmod", "followup", "web_discovery", "ftbwiki", "createwiki"}:
        return 0.85 if lexical_hits else -0.45
    return 0.0


def _source_authority_boost(row: Any, query: str, intent: Any | None) -> float:
    if not intent or intent.domain not in {"project", "known_mod"}:
        return 0.0
    channel = _source_channel(row)
    if channel not in {"pack_internal", "manual_research"}:
        return 0.0
    haystack = f"{row['title']}\n{row['source_path']}\n{str(row['text'])[:4200]}".lower()
    anchors = _topic_anchor_terms(query, intent)
    anchor_hits = sum(1 for term in anchors if term and term.lower() in haystack)
    if channel == "pack_internal":
        return 1.45 if anchor_hits else 0.35
    return 0.85 if anchor_hits else 0.15


def _topic_anchor_terms(query: str, intent: Any | None) -> list[str]:
    terms: list[str] = []
    if intent:
        terms.extend([str(getattr(intent, "entity", "") or "")])
        terms.extend(str(item) for item in getattr(intent, "keywords", []) or [])
    terms.extend(_exact_recall_terms(query))
    generic = {
        "minecraft",
        "mc",
        "mod",
        "mods",
        "modpack",
        "模组",
        "整合包",
        "资源包",
        "材质包",
        "光影",
        "玩法",
        "攻略",
        "教程",
        "列表",
        "boss",
        "Boss",
        "BOSS",
        "首领",
        "主世界",
        "下界",
        "末地",
    }
    output: list[str] = []
    for term in terms:
        value = str(term).strip()
        if not value or value in generic or value.lower() in generic:
            continue
        if len(value) < 2:
            continue
        if value.lower() in {item.lower() for item in output}:
            continue
        output.append(value)
    return output[:8]


def _multi_term_coverage_boost(row: Any, query: str, intent: Any | None) -> float:
    if not intent or intent.domain not in {"project", "known_mod"}:
        return 0.0
    raw_terms = [str(intent.entity or ""), *[str(item) for item in intent.keywords], *[str(item) for item in intent.search_queries]]
    terms: list[str] = []
    for term in raw_terms:
        for part in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", term):
            if part.lower() in {"minecraft", "mc", "mod", "mods", "modpack"}:
                continue
            if part not in terms:
                terms.append(part)
    if len(terms) < 2:
        return 0.0
    haystack = f"{row['title']}\n{row['source_path']}\n{str(row['text'])[:3200]}".lower()
    hits = [term for term in terms if term.lower() in haystack]
    if len(hits) < 2:
        return 0.0
    boost = 0.55 + min(len(hits), 4) * 0.18
    if "crawler_exports/mcmod/" in str(row["source_path"]).lower().replace("\\", "/"):
        boost += 0.18
    if _source_channel(row) == "pack_internal":
        boost += 0.22
    return min(boost, 1.35)


def _primary_entity_boost(row: Any, query: str, intent: Any | None) -> float:
    if not intent or intent.domain not in {"project", "known_mod"}:
        return 0.0
    candidates = [str(item) for item in getattr(intent, "keywords", []) if str(item).strip()]
    if not candidates:
        candidates = [str(getattr(intent, "entity", "") or "")]
    chinese_terms = [term for term in candidates if re.fullmatch(r"[\u4e00-\u9fff]{2,}", term)]
    if not chinese_terms:
        return 0.0
    primary = min(chinese_terms, key=len)
    haystack = f"{row['title']}\n{row['source_path']}\n{str(row['text'])[:3200]}".lower()
    if primary.lower() not in haystack:
        return 0.0
    path = str(row["source_path"]).lower().replace("\\", "/")
    boost = 0.65
    if primary.lower() in str(row["title"]).lower():
        boost += 0.45
    if "crawler_exports/mcmod/" in path:
        boost += 0.25
    if _source_channel(row) == "pack_internal":
        boost += 0.55
    return boost


def _manifest_fact_boost(row: Any, query: str) -> float:
    if not _is_modpack_fact_query(query):
        return 0.0
    raw_metadata = str(row["document_metadata_json"] or "")
    text = str(row["text"])
    path = str(row["source_path"]).lower().replace("\\", "/")
    if "downloaded_archive_evidence" in path or "source: modpack_download_evidence" in text.lower():
        return 6.0
    if "modpack_manifest_facts" in raw_metadata or "整合包 manifest 结构化事实" in text:
        return 5.0
    if "modpack_manifests" in path or "modpack_archive_summary" in path:
        return 1.6
    return 0.0


def _bm25_scores(rows: Iterable[Any], query: str) -> dict[int, float]:
    query_terms = _important_query_terms(query)
    if not query_terms:
        return {}

    documents: list[tuple[int, list[str]]] = []
    document_frequency = {term: 0 for term in query_terms}
    for row in rows:
        chunk_id = int(row["chunk_id"])
        searchable = f"{row['title']}\n{row['source_path']}\n{row['text']}"
        tokens = _tokenize_sparse(searchable)
        token_set = set(tokens)
        for term in query_terms:
            if term in token_set:
                document_frequency[term] += 1
        documents.append((chunk_id, tokens))

    if not documents:
        return {}

    avgdl = sum(len(tokens) for _, tokens in documents) / len(documents)
    avgdl = max(avgdl, 1.0)
    k1 = 1.25
    b = 0.72
    total_documents = len(documents)
    raw_scores: dict[int, float] = {}
    for chunk_id, tokens in documents:
        length = max(len(tokens), 1)
        frequencies: dict[str, int] = {}
        for token in tokens:
            if token in query_terms:
                frequencies[token] = frequencies.get(token, 0) + 1
        score = 0.0
        for term, term_frequency in frequencies.items():
            df = document_frequency.get(term, 0)
            idf = math.log(1 + (total_documents - df + 0.5) / (df + 0.5))
            denominator = term_frequency + k1 * (1 - b + b * length / avgdl)
            score += idf * (term_frequency * (k1 + 1)) / denominator
        raw_scores[chunk_id] = score

    max_score = max(raw_scores.values(), default=0.0)
    if max_score <= 0:
        return {chunk_id: 0.0 for chunk_id, _ in documents}
    return {chunk_id: score / max_score for chunk_id, score in raw_scores.items()}


class Retriever:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.embedder = make_embedder(config.embedding)

    def _load_index(self, index_path: Path):
        if not index_path.exists():
            raise FileNotFoundError(
                f"Vector index not found: {index_path}. Run: python -m mcagent.ingest"
            )
        stat = index_path.stat()
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        cache_key = str(index_path.resolve())
        cached = _vector_cache.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]
        np = _require_numpy()
        try:
            data = np.load(index_path, allow_pickle=False)
        except Exception as exc:  # noqa: BLE001 - surface a repair action for any unreadable npz.
            raise RuntimeError(f"Vector index is unreadable: {index_path}. Rebuild it with: python ingest.py") from exc
        chunk_ids = data["chunk_ids"]
        vectors = data["vectors"]
        if vectors.shape[0] != chunk_ids.shape[0]:
            raise RuntimeError("Vector index is corrupted: vector count does not match chunk IDs.")
        _vector_cache[cache_key] = (signature, (chunk_ids, vectors))
        return chunk_ids, vectors

    def search(
        self,
        query: str,
        top_k: int | None = None,
        *,
        plan: RetrievalPlan | None = None,
        use_planner: bool = False,
        session_summary: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        top_k = top_k or self.config.retrieval.top_k
        if top_k <= 0:
            return []

        np = _require_numpy()
        chunk_ids, vectors = self._load_index(self.config.paths.index_path)
        if vectors.shape[0] == 0:
            return []

        if plan is None and use_planner:
            plan = plan_retrieval(query, session_summary=session_summary, max_queries=8)
        search_queries = [query]
        if plan is not None:
            search_queries = _dedupe_search_queries([*plan.subqueries, query])

        alias_queries: list[str] = []
        if plan is not None:
            alias_queries = _local_alias_queries(self.config.paths.db_path, query, plan, limit=6)
            search_queries = _dedupe_search_queries([*search_queries, *alias_queries])

        query_vectors = self.embedder.embed([_expand_query(item) for item in search_queries])
        query_factor = max(1, min(len(search_queries), 8))
        candidate_count = min(max(top_k * 12, 50, query_factor * 18), min(160, len(vectors)))

        candidate_ids: list[int] = []
        vector_scores: dict[int, float] = {}
        for query_vector in query_vectors:
            scores = vectors @ query_vector
            candidate_indices = np.argpartition(scores, -candidate_count)[-candidate_count:]
            sorted_indices = candidate_indices[np.argsort(scores[candidate_indices])[::-1]]
            for index in sorted_indices:
                score = float(scores[index])
                if score < self.config.retrieval.min_score:
                    continue
                chunk_id = int(chunk_ids[index])
                if chunk_id not in vector_scores:
                    candidate_ids.append(chunk_id)
                    vector_scores[chunk_id] = score
                else:
                    vector_scores[chunk_id] = max(vector_scores[chunk_id], score)

        conn = connect(self.config.paths.db_path)
        try:
            fts_ids: list[int] = []
            for search_query in search_queries:
                fts_ids.extend(_fts_recall_chunk_ids(conn, search_query, limit=max(top_k * 6, 40)))
            fts_id_set = set(fts_ids)
            for chunk_id in fts_ids:
                if chunk_id not in vector_scores:
                    candidate_ids.append(chunk_id)
                    vector_scores[chunk_id] = 0.0
            literal_ids: list[int] = []
            for search_query in search_queries:
                literal_ids.extend(_literal_recall_chunk_ids(conn, search_query, limit=max(top_k * 5, 32)))
            literal_id_set = set(literal_ids)
            for chunk_id in literal_ids:
                if chunk_id not in vector_scores:
                    candidate_ids.append(chunk_id)
                    vector_scores[chunk_id] = 0.0
            manifest_fact_ids = _modpack_manifest_fact_chunk_ids(conn, query, limit=max(top_k * 3, 12))
            manifest_fact_id_set = set(manifest_fact_ids)
            for chunk_id in manifest_fact_ids:
                if chunk_id not in vector_scores:
                    candidate_ids.append(chunk_id)
                    vector_scores[chunk_id] = 0.0
            rows_by_id = fetch_chunks_by_ids(conn, candidate_ids)
        finally:
            conn.close()

        try:
            intent = analyze_query(query, CONCEPTS)
        except Exception:  # noqa: BLE001
            intent = None
        bm25_scores = _bm25_scores(rows_by_id.values(), " ".join(search_queries[:6]))
        reranked: list[tuple[float, int]] = []
        for chunk_id in candidate_ids:
            row = rows_by_id.get(chunk_id)
            if row is None:
                continue
            if plan is not None and not _strong_plan_term_overlap(row, plan):
                continue
            vector_weight = 0.25 if (intent and intent.domain in {"project", "known_mod"} and self.embedder.provider_name == "hashing_char_ngram") else 0.70
            score = (
                vector_weight * vector_scores[chunk_id]
                + _lexical_boost(row, query)
                + _source_intent_boost(row, query)
                + _project_channel_gate(row, query, intent)
                + _source_authority_boost(row, query, intent)
                + _multi_term_coverage_boost(row, query, intent)
                + _primary_entity_boost(row, query, intent)
                + _manifest_fact_boost(row, query)
                + _retrieval_plan_boost(row, plan)
                + (0.80 if chunk_id in fts_id_set else 0.0)
                + (0.95 if chunk_id in literal_id_set else 0.0)
                + (1.30 if chunk_id in manifest_fact_id_set else 0.0)
                + 0.42 * bm25_scores.get(chunk_id, 0.0)
            )
            reranked.append((score, chunk_id))
        reranked.sort(key=lambda item: item[0], reverse=True)
        selected_ids = [chunk_id for _, chunk_id in reranked[:top_k]]
        selected_scores = {chunk_id: score for score, chunk_id in reranked[:top_k]}

        results: list[SearchResult] = []
        for rank, chunk_id in enumerate(selected_ids, start=1):
            row = rows_by_id.get(chunk_id)
            if row is None:
                continue
            metadata: dict[str, Any] = {}
            for column in ("document_metadata_json", "chunk_metadata_json"):
                raw = row[column]
                if raw:
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        value = {}
                    if isinstance(value, dict):
                        metadata.update(value)
            results.append(
                SearchResult(
                    rank=rank,
                    score=selected_scores[chunk_id],
                    chunk_id=chunk_id,
                    document_id=int(row["document_id"]),
                    chunk_index=int(row["chunk_index"]),
                    title=str(row["title"]),
                    source_path=str(row["source_path"]),
                    url=str(row["url"]) if row["url"] else None,
                    text=str(row["text"]),
                    metadata=metadata,
                )
            )
        return results


def _dedupe_search_queries(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = re.sub(r"\s+", " ", str(item)).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output[:12]


def _retrieval_plan_boost(row: Any, plan: RetrievalPlan | None) -> float:
    if plan is None:
        return 0.0
    title = str(row["title"]).lower()
    path = str(row["source_path"]).lower().replace("\\", "/")
    text = str(row["text"])[:3600].lower()
    haystack = f"{title}\n{path}\n{text}"
    required = [term.lower() for term in plan.required_terms if term]
    optional = [term.lower() for term in plan.optional_terms if term]
    negative = [term.lower() for term in plan.negative_terms if term]
    score = 0.0
    anchors = _plan_anchor_terms(plan)
    if anchors:
        if any(term in title for term in anchors):
            score += 1.05
        elif any(term in path for term in anchors):
            score += 0.65
        elif any(term in text for term in anchors):
            score += 0.25
        if "crawler_exports/topic_discovery/" in path:
            score -= 1.10
        if _source_channel(row) == "mcmod" and any(term in title for term in anchors):
            score += 0.45
    if required:
        hits = sum(1 for term in required if term in haystack)
        if hits == 0:
            score -= 0.85
        else:
            score += min(1.20, 0.55 + hits * 0.22)
    if optional:
        hits = sum(1 for term in optional if term in haystack)
        score += min(0.65, hits * 0.12)
    if negative and any(term in haystack for term in negative):
        score -= 0.75
    return score


def _strong_plan_term_overlap(row: Any, plan: RetrievalPlan | None) -> bool:
    if plan is None:
        return True
    haystack = f"{row['title']}\n{row['source_path']}\n{str(row['text'])[:3600]}".lower()
    anchors = _plan_anchor_terms(plan)
    if anchors:
        return any(term in haystack for term in anchors)
    useful = [
        term.lower()
        for term in [*plan.required_terms, *plan.optional_terms, *plan.subqueries[:4]]
        if term and len(term) >= 2 and term not in {"什么", "有哪些", "用法", "作用", "玩法", "攻略", "教程"}
    ]
    if not useful:
        return True
    return any(term in haystack for term in useful)


def _plan_anchor_terms(plan: RetrievalPlan) -> list[str]:
    generic = {"什么", "有哪些", "用法", "作用", "玩法", "攻略", "教程", "列表", "大全", "一览"}
    anchors: list[str] = []
    candidates = [*plan.required_terms[:2], plan.topic]
    for value in candidates:
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", str(value)):
            lowered = term.lower()
            if lowered in generic or len(term) < 2:
                continue
            anchors.append(lowered)
            break
        if anchors:
            break
    return anchors


def _local_alias_queries(db_path: Path, query: str, plan: RetrievalPlan, *, limit: int = 6) -> list[str]:
    seeds = _alias_seed_terms(query, plan)
    if not seeds or not db_path.exists():
        return []
    clauses: list[str] = []
    params: list[str] = []
    for seed in seeds[:6]:
        like = f"%{seed}%"
        clauses.append("(documents.title LIKE ? OR documents.source_path LIKE ? OR chunks.text LIKE ?)")
        params.extend([like, like, like])
    sql = f"""
        SELECT documents.title AS title, documents.source_path AS source_path, chunks.text AS text
        FROM chunks
        JOIN documents ON documents.id = chunks.document_id
        WHERE {" OR ".join(clauses)}
        LIMIT 80
    """
    try:
        conn = connect(db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    aliases: dict[str, float] = {}
    for row in rows:
        haystack = f"{row['title']}\n{row['source_path']}\n{str(row['text'])[:2200]}"
        for alias in _extract_alias_candidates(haystack):
            if _bad_alias(alias):
                continue
            score = aliases.get(alias, 0.0)
            if alias.lower() in query.lower():
                score += 0.2
            if alias.lower() in str(row["title"]).lower():
                score += 1.1
            if any(marker in str(row["source_path"]).lower().replace("\\", "/") for marker in ("modrinth_agent", "mcmod", "fetch_url", "web_discovery", "playwright")):
                score += 0.35
            aliases[alias] = score + 0.25
    ranked = sorted(aliases.items(), key=lambda item: item[1], reverse=True)
    optional = [term for term in plan.optional_terms if term and not re.fullmatch(r"[\u4e00-\u9fff]{1,2}", term)]
    queries: list[str] = []
    for alias, _score in ranked:
        queries.append(alias)
        for term in optional[:3]:
            if term.lower() not in alias.lower():
                queries.append(f"{alias} {term}")
        if len(queries) >= limit:
            break
    return _dedupe_search_queries(queries)[:limit]


def _alias_seed_terms(query: str, plan: RetrievalPlan) -> list[str]:
    seeds: list[str] = []
    for item in [*plan.required_terms, plan.topic, query]:
        for term in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_+-]{2,}", str(item)):
            if term.lower() in {"minecraft", "mod", "mods", "modpack"}:
                continue
            seeds.append(term)
    return _dedupe_search_queries(seeds)[:8]


def _extract_alias_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    stop = {
        "Minecraft", "MC", "MOD", "Mods", "Mod", "Forge", "Fabric", "NeoForge",
        "Bilibili", "YouTube", "GitHub", "CurseForge", "Modrinth", "Wiki",
    }
    patterns = [
        r"[\(（]([A-Z][A-Za-z0-9_ '\-:&]{2,60})[\)）]",
        r"\b([A-Z][A-Za-z0-9_'\-]+(?:\s+[A-Z][A-Za-z0-9_'\-]+){0,4})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = re.sub(r"\s+", " ", match.group(1)).strip(" -_")
            words = value.split()
            if not value or value in stop or len(value) < 3 or len(value) > 64:
                continue
            if all(word in stop for word in words):
                continue
            if re.fullmatch(r"[A-Fa-f0-9]{6,}", value):
                continue
            candidates.append(value)
    return _dedupe_search_queries(candidates)[:20]


def _bad_alias(value: str) -> bool:
    lowered = value.lower()
    bad = {
        "mc_agent", "bbsmc", "image", "minecraft", "mod", "mods", "modpack",
        "curseforge", "modrinth", "github", "bilibili", "youtube",
    }
    if lowered in bad:
        return True
    if lowered.startswith(("image ", "search ", "query ", "fetched ", "metadata")):
        return True
    if re.search(r"\b(url|api|html|markdown|manifest|metadata)\b", lowered):
        return True
    return False
