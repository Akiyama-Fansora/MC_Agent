from __future__ import annotations

import argparse
from datetime import datetime
from html import unescape
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse
import urllib.error
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key
from mcagent.cleaners import _HTMLTextExtractor, normalize_text


DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (tavily discovery; D:/magic/MC_Agent)"
DEFAULT_API_URL = "https://api.tavily.com/search"
SKIP_HOST_PARTS = (
    "youtube.",
    "youtu.be",
    "discord.",
    "curseforge.com",
    "maven.",
    "patreon.",
    "ko-fi.",
)


def slugify(value: str, fallback: str = "page") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:90] or fallback


def unique_output_path(run_dir: Path, filename: str, digest: str) -> Path:
    path = run_dir / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    return run_dir / f"{stem}_{digest[:8]}{suffix}"


def request_text(url: str, user_agent: str, timeout: int = 30, retries: int = 1) -> tuple[str, str, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,text/plain,application/json,*/*",
            "User-Agent": user_agent,
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), content_type, int(response.status)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.7 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def request_json(url: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(raw.decode(charset, errors="replace"))


def read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.strip().lstrip("\ufeff")] = value.strip().strip('"').strip("'")
    return values


def tavily_api_key() -> str:
    return (
        os.environ.get("TAVILY_API_KEY", "").strip()
        or read_dotenv(PROJECT_ROOT / ".env").get("TAVILY_API_KEY", "").strip()
    )


def should_skip_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not parsed.scheme.startswith("http"):
        return True
    return any(part in host for part in SKIP_HOST_PARTS)


def query_tokens(query: str) -> list[str]:
    stop = {
        "minecraft",
        "mc",
        "mod",
        "mods",
        "玩法",
        "介绍",
        "详细",
        "哪些",
        "什么",
        "怎么",
        "如何",
        "合成",
        "配方",
    }
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", query.lower())
    return list(dict.fromkeys(token for token in tokens if token not in stop))


def query_variants(query: str) -> list[str]:
    base = normalize_text(query).strip()
    variants = [base]
    tokens = query_tokens(base)
    if tokens:
        variants.append(" ".join(tokens[:5]))
    if "minecraft" not in base.lower():
        variants.append(f"{base} Minecraft")
    if "mc百科" not in base and "mcmod" not in base.lower():
        variants.append(f"{base} MC百科")
    if "mod" not in base.lower():
        variants.append(f"{base} Minecraft mod")
    return list(dict.fromkeys(item for item in variants if item.strip()))[:8]


def relevance_score(query: str, title: str, snippet: str, text: str = "", url: str = "") -> float:
    terms = query_tokens(query)
    if not terms:
        return 0.2
    haystack = f"{title}\n{snippet}\n{url}\n{text[:9000]}".lower()
    hits = sum(1 for term in terms if term.lower() in haystack)
    phrase_bonus = 1.0 if query.strip().lower() in haystack else 0.0
    mc_bonus = 0.4 if any(mark in haystack for mark in ("minecraft", "mc百科", "mcmod", "modrinth", "wiki", "forge", "fabric")) else 0.0
    return hits / max(1, len(terms)) + phrase_bonus + mc_bonus


def extract_tables_images(content: str, url: str) -> tuple[str, str, str]:
    if not re.search(r"<html|<body|<article|<main|<table|<img", content[:4000], flags=re.I):
        return normalize_text(unescape(content)), "", ""
    parser = _HTMLTextExtractor()
    parser.feed(content)
    parser.close()
    tables = tables_to_markdown(parser.tables)
    images = images_to_markdown(parser.images, url)
    return parser.text, tables, images


def tables_to_markdown(tables: list[list[list[str]]]) -> str:
    chunks: list[str] = []
    for table_index, rows in enumerate(tables, start=1):
        cleaned_rows = [[normalize_text(cell) for cell in row] for row in rows if any(normalize_text(cell) for cell in row)]
        if not cleaned_rows:
            continue
        width = max(len(row) for row in cleaned_rows)
        normalized = [row + [""] * (width - len(row)) for row in cleaned_rows]
        chunks.append(f"### Table {table_index}")
        header = normalized[0]
        chunks.append("| " + " | ".join(header) + " |")
        chunks.append("| " + " | ".join(["---"] * width) + " |")
        for row in normalized[1:]:
            chunks.append("| " + " | ".join(row) + " |")
        chunks.append("")
    return "\n".join(chunks).strip()


def images_to_markdown(images: list[dict[str, str]], base_url: str) -> str:
    rows: list[str] = []
    for image in images[:80]:
        src = image.get("src", "").strip()
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            parsed = urlparse(base_url)
            src = f"{parsed.scheme}://{parsed.netloc}{src}"
        alt = normalize_text(image.get("alt", ""))
        rows.append(f"- image: {src}" + (f" alt={alt}" if alt else ""))
    return "\n".join(rows)


def page_to_markdown(item: dict[str, Any], query: str, content: str, fetched_at: str, source: str) -> tuple[str, str]:
    title = normalize_text(str(item.get("title") or item.get("url") or "Tavily result"))
    url = str(item.get("url") or "")
    text, tables, images = extract_tables_images(content, url)
    text = normalize_text(text)
    lines = [
        f"# {title}",
        "",
        "<!-- source: tavily -->",
        "",
        "## Metadata",
        "",
        f"- **URL:** {url}",
        f"- **Tavily source:** {source}",
        f"- **Query:** {query}",
        f"- **Fetched at:** {fetched_at}",
        f"- **Score:** {item.get('score', '')}",
        "",
        "## Search Snippet",
        "",
        normalize_text(str(item.get("content") or "")),
        "",
        "## Content",
        "",
        text,
    ]
    if tables:
        lines.extend(["", "## Extracted Tables", "", tables])
    if images:
        lines.extend(["", "## Images", "", images])
    return title, "\n".join(lines).strip() + "\n"


def tavily_search(api_key: str, query: str, max_results: int, search_depth: str) -> dict[str, Any]:
    api_url = os.environ.get("TAVILY_API_URL", "").strip() or DEFAULT_API_URL
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_depth,
        "max_results": max(1, min(max_results, 20)),
        "include_answer": False,
        "include_raw_content": "markdown",
    }
    return request_json(api_url, payload, timeout=80)


def fetch_tavily(
    dest_root: Path,
    query: str,
    max_results: int,
    max_pages: int,
    search_depth: str,
    force: bool,
    user_agent: str,
) -> dict[str, Any]:
    api_key = tavily_api_key()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured. Put it in D:\\magic\\MC_Agent\\.env or the process environment.")

    run_dir = dest_root / "tavily" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    ledger = load_ledger()
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    all_search_results: list[dict[str, Any]] = []

    seen_urls: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for variant in query_variants(query):
        try:
            response = tavily_search(api_key, variant, max_results=max_results, search_depth=search_depth)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "search", "query": variant, "error": str(exc)})
            continue
        results = response.get("results") if isinstance(response, dict) else []
        if not isinstance(results, list):
            continue
        for rank, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            key = url.lower().rstrip("/")
            all_search_results.append(
                {
                    "engine": "tavily",
                    "rank": rank,
                    "query": variant,
                    "title": item.get("title") or "",
                    "url": url,
                    "snippet": item.get("content") or "",
                    "score": item.get("score"),
                }
            )
            if key in seen_urls:
                continue
            seen_urls.add(key)
            if should_skip_url(url):
                skipped.append({"url": url, "reason": "skip_host", "title": item.get("title", "")})
                continue
            text = str(item.get("raw_content") or item.get("content") or "")
            score = relevance_score(query, str(item.get("title") or ""), str(item.get("content") or ""), text=text, url=url)
            if score < 0.45:
                skipped.append({"url": url, "reason": "low_relevance", "score": round(score, 3), "title": item.get("title", "")})
                continue
            candidates.append(item | {"query_variant": variant, "search_relevance": round(score, 3)})

    for item in candidates[: max(1, max_pages)]:
        url = str(item.get("url") or "")
        content = str(item.get("raw_content") or "")
        raw_html = ""
        status_code = 0
        if len(content.strip()) < 200:
            content = str(item.get("content") or "")
        try:
            direct_content, direct_type, status_code = request_text(url, user_agent=user_agent, timeout=30, retries=1)
            if "html" in direct_type.lower() or re.search(r"<html|<body|<article|<main", direct_content[:2000], flags=re.I):
                raw_html = direct_content
                if len(content.strip()) < 500:
                    content = direct_content
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "raw_html", "url": url, "error": str(exc)})
        if len(content.strip()) < 200:
            skipped.append({"url": url, "reason": "too_short", "status_code": status_code})
            continue
        page_score = relevance_score(query, str(item.get("title") or ""), str(item.get("content") or ""), text=content, url=url)
        if page_score < 0.55:
            skipped.append({"url": url, "reason": "page_low_relevance", "score": round(page_score, 3), "status_code": status_code})
            continue
        title, markdown = page_to_markdown(item, query=query, content=content, fetched_at=fetched_at, source=str(item.get("query_variant") or query))
        digest = content_hash(markdown)
        item_id = url.lower().rstrip("/")
        key = make_key("tavily", item_id)
        previous = ledger.get(key)
        if previous and previous.get("content_hash") == digest and not force:
            skipped.append({"url": url, "reason": "unchanged", "previous_path": previous.get("path", ""), "score": round(page_score, 3)})
            append_ledger(
                ledger_record(
                    source="tavily",
                    item_id=item_id,
                    title=title,
                    url=url,
                    text=markdown,
                    path=str(previous.get("path", "")),
                    query=query,
                    status="skipped_unchanged",
                    previous=previous,
                )
            )
            continue
        path = unique_output_path(run_dir, f"tavily_{slugify(title, 'page')}.md", digest)
        raw_path = raw_dir / f"{path.stem}.html"
        path.write_text(markdown, encoding="utf-8")
        if raw_html:
            raw_path.write_text(raw_html, encoding="utf-8")
        append_ledger(
            ledger_record(
                source="tavily",
                item_id=item_id,
                title=title,
                url=url,
                text=markdown,
                path=str(path),
                query=query,
                status="updated" if previous else "new",
                previous=previous,
            )
        )
        records.append(
            {
                "title": title,
                "url": url,
                "path": str(path),
                "raw_html_path": str(raw_path) if raw_html else "",
                "chars": len(markdown),
                "raw_html_chars": len(raw_html),
                "score": round(page_score, 3),
                "status_code": status_code,
                "engine": "tavily",
            }
        )

    manifest = {
        "manifest_type": "tavily_export",
        "created_at": fetched_at,
        "export_dir": str(run_dir),
        "query": query,
        "query_variants": query_variants(query),
        "search_depth": search_depth,
        "search_results": all_search_results[:120],
        "candidates": len(candidates),
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search Tavily, save result Markdown/raw HTML, and prepare it for MCagent RAG.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--search-depth", choices=("basic", "advanced"), default="advanced")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_tavily(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        max_results=max(1, min(args.max_results, 20)),
        max_pages=max(1, min(args.max_pages, 20)),
        search_depth=args.search_depth,
        force=args.force,
        user_agent=args.user_agent,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Search results: {len(manifest['search_results'])}")
    print(f"Candidates: {manifest['candidates']}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
