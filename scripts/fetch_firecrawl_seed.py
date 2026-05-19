from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key
from mcagent.provider_registry import (
    ProviderResult,
    env_value,
    export_provider_results,
    query_variants,
    relevance_score,
    request_json,
    request_text,
)


DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (firecrawl discovery; D:/magic/MC_Agent)"
DEFAULT_API_URL = "https://api.firecrawl.dev"


def firecrawl_base_url() -> str:
    return (env_value("FIRECRAWL_API_URL") or DEFAULT_API_URL).rstrip("/")


def firecrawl_headers() -> dict[str, str]:
    key = env_value("FIRECRAWL_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def firecrawl_request(path: str, payload: dict[str, Any], timeout: int = 90) -> dict[str, Any]:
    return request_json(f"{firecrawl_base_url()}{path}", payload, headers=firecrawl_headers(), timeout=timeout)


def extract_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data", response)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        web = data.get("web")
        if isinstance(web, list):
            return [item for item in web if isinstance(item, dict)]
        results = data.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        if data.get("url"):
            return [data]
    results = response.get("results")
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def search_firecrawl(query: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    all_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for variant in query_variants(query):
        payload = {
            "query": variant,
            "limit": max(1, min(limit, 10)),
            "scrapeOptions": {"formats": ["markdown", "html"], "onlyMainContent": True},
        }
        try:
            response = firecrawl_request("/v2/search", payload)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "search", "query": variant, "error": str(exc)})
            continue
        for rank, item in enumerate(extract_results(response), start=1):
            item["rank"] = rank
            item["query"] = variant
            item["engine"] = "firecrawl"
            all_results.append(item)
    return all_results, errors


def scrape_firecrawl(url: str) -> tuple[str, str]:
    response = firecrawl_request("/v2/scrape", {"url": url, "formats": ["markdown", "html"], "onlyMainContent": True})
    data = response.get("data", response)
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("markdown") or ""), str(data.get("html") or "")


def fetch_firecrawl(dest_root: Path, query: str, max_results: int, max_pages: int, force: bool, user_agent: str) -> dict[str, Any]:
    if firecrawl_base_url() == DEFAULT_API_URL and not env_value("FIRECRAWL_API_KEY"):
        raise RuntimeError("FIRECRAWL_API_KEY is required for Firecrawl Cloud. For self-hosted Firecrawl, set FIRECRAWL_API_URL.")

    search_results, errors = search_firecrawl(query, max_results)
    skipped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in search_results:
        url = str(item.get("url") or item.get("metadata", {}).get("sourceURL") or "").strip()
        if not url:
            continue
        key = url.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        markdown = str(item.get("markdown") or item.get("content") or "")
        score = relevance_score(query, str(item.get("title") or ""), str(item.get("description") or item.get("content") or ""), text=markdown, url=url)
        if score < 0.45:
            skipped.append({"url": url, "reason": "low_relevance", "score": round(score, 3), "title": item.get("title", "")})
            continue
        candidates.append(item | {"url": url, "search_relevance": round(score, 3)})

    results: list[ProviderResult] = []
    for item in candidates[: max(1, max_pages)]:
        url = str(item["url"])
        markdown = str(item.get("markdown") or item.get("content") or "")
        raw_html = str(item.get("html") or "")
        if len(markdown.strip()) < 200:
            try:
                markdown, raw_html = scrape_firecrawl(url)
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "scrape", "url": url, "error": str(exc)})
        if len(markdown.strip()) < 200:
            try:
                raw_html, _content_type, _status = request_text(url, user_agent=user_agent, timeout=30, retries=1)
                markdown = raw_html
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "direct_fetch", "url": url, "error": str(exc)})
        if len(markdown.strip()) < 200:
            skipped.append({"url": url, "reason": "too_short"})
            continue
        page_score = relevance_score(query, str(item.get("title") or ""), str(item.get("description") or item.get("content") or ""), text=markdown, url=url)
        if page_score < 0.5:
            skipped.append({"url": url, "reason": "page_low_relevance", "score": round(page_score, 3)})
            continue
        results.append(
            ProviderResult(
                provider="firecrawl",
                stage="search_scrape",
                query=query,
                title=str(item.get("title") or url),
                url=url,
                markdown=markdown,
                raw_html=raw_html if re.search(r"<html|<body|<article|<main", raw_html[:4000], flags=re.I) else "",
                score=page_score,
                metadata={"engine": "firecrawl", "source_query": item.get("query", ""), "base_url": firecrawl_base_url()},
            )
        )

    return export_provider_results(
        dest_root=dest_root,
        provider="firecrawl",
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
        extra_manifest={"candidates": len(candidates), "query_variants": query_variants(query), "base_url": firecrawl_base_url()},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search/scrape with Firecrawl and save Markdown/raw HTML for MCagent RAG.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_firecrawl(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        max_results=max(1, min(args.max_results, 10)),
        max_pages=max(1, min(args.max_pages, 10)),
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
