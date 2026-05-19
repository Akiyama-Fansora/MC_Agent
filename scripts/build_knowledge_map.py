from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcagent.config import load_config
from mcagent.crawler_planner import CONCEPTS, TOOLSETS
from mcagent.storage import connect


def source_bucket(source_path: str) -> str:
    lowered = source_path.lower()
    if "createwiki" in lowered:
        return "createwiki"
    if "ftbwiki" in lowered:
        return "ftbwiki"
    if "modrinth_agent" in lowered:
        return "modrinth"
    if "mcmod" in lowered:
        return "mcmod"
    if "followup" in lowered:
        return "followup"
    if "mediawiki" in lowered:
        return "mediawiki"
    return "other"


def concept_coverage(title: str, source_path: str) -> list[str]:
    haystack = f"{title} {source_path}".lower()
    matched = []
    for concept in CONCEPTS:
        if any(alias.lower() in haystack for alias in concept["aliases"]):
            matched.append(str(concept["canonical"]))
    return matched


def build_map(db_path: Path, out_path: Path) -> dict[str, Any]:
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(db_path)
        rows = conn.execute("SELECT id, title, source_path, url, imported_at FROM documents ORDER BY id").fetchall()
    finally:
        if conn is not None:
            conn.close()

    by_source = Counter()
    recent_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    coverage: dict[str, dict[str, Any]] = {
        str(concept["canonical"]): {
            "primary_source": concept["primary_source"],
            "documents": 0,
            "sources": Counter(),
            "examples": [],
        }
        for concept in CONCEPTS
    }

    for row in rows:
        title = str(row["title"])
        source_path = str(row["source_path"])
        bucket = source_bucket(source_path)
        by_source[bucket] += 1
        if len(recent_by_source[bucket]) < 12:
            recent_by_source[bucket].append({"title": title, "path": source_path, "url": row["url"]})
        for canonical in concept_coverage(title, source_path):
            item = coverage[canonical]
            item["documents"] += 1
            item["sources"][bucket] += 1
            if len(item["examples"]) < 8:
                item["examples"].append({"title": title, "source": bucket, "path": source_path})

    normalized_coverage = {}
    for key, value in coverage.items():
        normalized_coverage[key] = {
            "primary_source": value["primary_source"],
            "documents": value["documents"],
            "sources": dict(value["sources"]),
            "examples": value["examples"],
        }

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(db_path),
        "documents": len(rows),
        "sources": dict(by_source),
        "toolsets": {key: asdict(value) for key, value in TOOLSETS.items()},
        "concept_coverage": normalized_coverage,
        "recent_by_source": dict(recent_by_source),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a lightweight knowledge map for MCagent's local RAG corpus.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--out", default=str(PROJECT_ROOT / "data" / "knowledge_map.json"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    payload = build_map(config.paths.db_path, Path(args.out).resolve())
    print(f"Knowledge map written: {args.out}")
    print(f"Documents: {payload['documents']}")
    print(f"Sources: {json.dumps(payload['sources'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
