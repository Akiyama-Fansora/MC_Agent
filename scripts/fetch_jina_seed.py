from __future__ import annotations

import argparse
from html import unescape
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote, quote_plus, urlparse
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key
from mcagent.cleaners import normalize_text
from mcagent.provider_registry import (
    ProviderResult,
    export_provider_results,
    query_variants,
    relevance_score,
    request_text,
)


DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (jina reader discovery; D:/magic/MC_Agent)"
SKIP_HOST_PARTS = ("youtube.", "youtu.be", "discord.", "curseforge.com", "maven.", "patreon.", "ko-fi.")


def should_skip_url(url: str) -> bool:
    parsed = urlparse(url)
    return not parsed.scheme.startswith("http") or any(part in parsed.netloc.lower() for part in SKIP_HOST_PARTS)


def parse_jina_search(text: str, query: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        title_match = re.match(r"^\[(\d+)\]\s+(.+)$", stripped)
        if title_match:
            if current.get("url"):
                results.append(current)
            current = {"rank": int(title_match.group(1)), "title": title_match.group(2), "query": query}
            continue
        if stripped.lower().startswith("url:"):
            current["url"] = stripped.split(":", 1)[1].strip()
            continue
        if stripped.lower().startswith("content:"):
            current["snippet"] = stripped.split(":", 1)[1].strip()
            continue
        markdown_match = re.match(r"^\[([^\]]+)\]\((https?://[^)]+)\)", stripped)
        if markdown_match:
            if current.get("url"):
                results.append(current)
            current = {"rank": len(results) + 1, "title": markdown_match.group(1), "url": markdown_match.group(2), "snippet": "", "query": query}
    if current.get("url"):
        results.append(current)
    return results


def reader_url(url: str) -> str:
    return "https://r.jina.ai/http://" + url.removeprefix("http://").removeprefix("https://") if url.startswith(("http://", "https://")) else "https://r.jina.ai/http://" + url


def search_jina(query: str, user_agent: str, max_results: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    search_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for variant in query_variants(query):
        url = "https://s.jina.ai/" + quote_plus(variant)
        try:
            text, _content_type, status = request_text(url, user_agent=user_agent, timeout=40, retries=1)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "search", "query": variant, "error": str(exc)})
            continue
        for item in parse_jina_search(text, variant):
            item["engine"] = "jina_search"
            item["status"] = status
            search_results.append(item)
            if len(search_results) >= max_results * 4:
                break
    if not search_results:
        for variant in query_variants(query):
            url = "https://www.bing.com/search?format=rss&mkt=zh-CN&setlang=zh-Hans&q=" + quote(variant, safe="")
            try:
                text, _content_type, status = request_text(url, user_agent=user_agent, timeout=35, retries=1)
                root = ET.fromstring(text)
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "bing_rss_fallback", "query": variant, "error": str(exc)})
                continue
            for rank, item in enumerate(root.findall("./channel/item"), start=1):
                title = "".join(item.findtext("title") or "").strip()
                link = "".join(item.findtext("link") or "").strip()
                snippet = normalize_text(unescape(item.findtext("description") or ""))
                if link:
                    search_results.append({"engine": "bing_rss_for_jina", "status": status, "rank": rank, "title": title, "url": link, "snippet": snippet, "query": variant})
                if len(search_results) >= max_results * 4:
                    break
    return search_results, errors


def fetch_jina(dest_root: Path, query: str, max_results: int, max_pages: int, force: bool, user_agent: str) -> dict[str, Any]:
    if query.startswith(("http://", "https://")):
        search_results, errors = ([{"engine": "direct_url", "rank": 1, "title": query, "url": query, "snippet": "", "query": query}], [])
    else:
        search_results, errors = search_jina(query, user_agent=user_agent, max_results=max_results)
    skipped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in search_results:
        url = str(item.get("url") or "").strip()
        key = url.lower().rstrip("/")
        if not url or key in seen:
            continue
        seen.add(key)
        if should_skip_url(url):
            skipped.append({"url": url, "reason": "skip_host", "title": item.get("title", "")})
            continue
        score = relevance_score(query, str(item.get("title") or ""), str(item.get("snippet") or ""), url=url)
        if score < 0.18:
            skipped.append({"url": url, "reason": "low_relevance", "score": round(score, 3), "title": item.get("title", "")})
            continue
        candidates.append(item | {"search_relevance": round(score, 3)})

    results: list[ProviderResult] = []
    for item in candidates[: max(1, max_pages)]:
        url = str(item["url"])
        try:
            content, content_type, status = request_text(reader_url(url), user_agent=user_agent, timeout=50, retries=1)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "reader", "url": url, "error": str(exc)})
            continue
        content = normalize_text(unescape(content))
        if len(content.strip()) < 200:
            skipped.append({"url": url, "reason": "too_short", "status_code": status})
            continue
        page_score = relevance_score(query, str(item.get("title") or ""), str(item.get("snippet") or ""), text=content, url=url)
        if page_score < 0.5:
            skipped.append({"url": url, "reason": "page_low_relevance", "score": round(page_score, 3), "status_code": status})
            continue
        results.append(
            ProviderResult(
                provider="jina",
                stage="search_extract",
                query=query,
                title=str(item.get("title") or url),
                url=url,
                markdown=content,
                score=page_score,
                metadata={"engine": "jina", "content_type": content_type, "status_code": status, "source_query": item.get("query", "")},
            )
        )

    return export_provider_results(
        dest_root=dest_root,
        provider="jina",
        query=query,
        results=results,
        search_results=search_results,
        skipped=skipped,
        errors=errors,
        force=force,
        content_hash=content_hash,
        ledger_record=ledger_record,
        append_ledger=append_ledger,
        load_ledger=load_ledger,
        make_key=make_key,
        extra_manifest={"candidates": len(candidates), "query_variants": query_variants(query)},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search Jina and save Reader Markdown for MCagent RAG.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_jina(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        max_results=max(1, min(args.max_results, 20)),
        max_pages=max(1, min(args.max_pages, 20)),
        force=args.force,
        user_agent=args.user_agent,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Search results: {len(manifest['search_results'])}")
    print(f"Candidates: {manifest.get('candidates', 0)}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
