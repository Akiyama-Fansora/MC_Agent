from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote
from html import unescape
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key
from mcagent.provider_registry import (
    ProviderResult,
    export_provider_results,
    query_variants,
    relevance_score,
    request_text,
)


DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (playwright fallback; D:/magic/MC_Agent)"


def search_candidates(query: str, user_agent: str, max_results: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for variant in query_variants(query):
        url = "https://www.bing.com/search?format=rss&mkt=zh-CN&setlang=zh-Hans&q=" + quote(variant, safe="")
        try:
            text, _content_type, status = request_text(url, user_agent=user_agent, timeout=35, retries=1)
            root = ET.fromstring(text)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "search", "query": variant, "error": str(exc)})
            continue
        for rank, item in enumerate(root.findall("./channel/item"), start=1):
            link = "".join(item.findtext("link") or "").strip()
            if not link:
                continue
            results.append(
                {
                    "engine": "bing_rss_for_playwright",
                    "status": status,
                    "rank": rank,
                    "title": "".join(item.findtext("title") or "").strip(),
                    "url": link,
                    "snippet": unescape(item.findtext("description") or ""),
                    "query": variant,
                }
            )
            if len(results) >= max_results * 3:
                break
    return results, errors


def render_page(url: str, timeout_ms: int) -> tuple[str, str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=DEFAULT_USER_AGENT)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
        except Exception:
            pass
        title = page.title()
        html = page.content()
        try:
            text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", html)
        browser.close()
    return title, text, html


def fetch_playwright(dest_root: Path, query: str, max_results: int, max_pages: int, force: bool, user_agent: str, timeout_ms: int) -> dict[str, Any]:
    if query.startswith(("http://", "https://")):
        search_results, errors = ([{"engine": "direct_url", "rank": 1, "title": query, "url": query, "snippet": "", "query": query}], [])
    else:
        search_results, errors = search_candidates(query, user_agent=user_agent, max_results=max_results)
    skipped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in search_results:
        url = str(item.get("url") or "").strip()
        key = url.lower().rstrip("/")
        if not url or key in seen:
            continue
        seen.add(key)
        score = relevance_score(query, str(item.get("title") or ""), str(item.get("snippet") or ""), url=url)
        if score < 0.35:
            skipped.append({"url": url, "reason": "low_relevance", "score": round(score, 3), "title": item.get("title", "")})
            continue
        candidates.append(item | {"search_relevance": round(score, 3)})

    results: list[ProviderResult] = []
    for item in candidates[: max(1, max_pages)]:
        url = str(item["url"])
        try:
            title, text, html = render_page(url, timeout_ms=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "render", "url": url, "error": str(exc)})
            continue
        if len(text.strip()) < 200:
            skipped.append({"url": url, "reason": "too_short"})
            continue
        page_score = relevance_score(query, title or str(item.get("title") or ""), str(item.get("snippet") or ""), text=text, url=url)
        if page_score < 0.5:
            skipped.append({"url": url, "reason": "page_low_relevance", "score": round(page_score, 3)})
            continue
        results.append(
            ProviderResult(
                provider="playwright",
                stage="browser_extract",
                query=query,
                title=title or str(item.get("title") or url),
                url=url,
                markdown=text,
                raw_html=html,
                score=page_score,
                metadata={"engine": "playwright", "source_query": item.get("query", "")},
            )
        )

    return export_provider_results(
        dest_root=dest_root,
        provider="playwright",
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
    parser = argparse.ArgumentParser(description="Render candidate pages with Playwright and save text/raw HTML for MCagent RAG.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=6)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--timeout-ms", type=int, default=25000)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_playwright(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        max_results=max(1, min(args.max_results, 12)),
        max_pages=max(1, min(args.max_pages, 8)),
        force=args.force,
        user_agent=args.user_agent,
        timeout_ms=max(5000, min(args.timeout_ms, 60000)),
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
