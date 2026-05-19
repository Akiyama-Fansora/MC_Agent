from __future__ import annotations

import argparse
from datetime import datetime
from html import unescape
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl_ledger import append_ledger, build_global_indexes, content_fingerprint, content_hash, global_url_key, ledger_record, load_ledger, make_key, same_record_content
from mcagent.cleaners import _HTMLTextExtractor, html_to_text, normalize_text


DEFAULT_USER_AGENT = "MC_Agent/0.1 (public discovery; D:/magic/MC_Agent)"
DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
SKIP_HOST_PARTS = (
    "bing.com",
    "google.",
    "baidu.",
    "duckduckgo.",
    "youtube.",
    "youtu.be",
    "bilibili.com",
    "discord.",
    "curseforge.com",
    "maven.",
    "patreon.",
    "ko-fi.",
)

TEXT_EXTENSIONS = (".md", ".txt", ".rst", ".json", ".html", ".htm")


def slugify(value: str, fallback: str = "page") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:90] or fallback


def request_text(url: str, user_agent: str, timeout: int = 30, retries: int = 1) -> tuple[str, str, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,text/plain,application/json,application/rss+xml,application/xml,*/*",
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


def bing_rss_search(query: str, user_agent: str, limit: int) -> list[dict[str, Any]]:
    url = "https://www.bing.com/search?format=rss&mkt=zh-CN&setlang=zh-Hans&q=" + quote(query, safe="")
    content, _content_type, status = request_text(url, user_agent=user_agent, timeout=25, retries=1)
    root = ET.fromstring(content)
    results: list[dict[str, Any]] = []
    for rank, item in enumerate(root.findall("./channel/item"), start=1):
        title = "".join(item.findtext("title") or "").strip()
        link = "".join(item.findtext("link") or "").strip()
        description = normalize_text(unescape(item.findtext("description") or ""))
        if not link:
            continue
        results.append(
            {
                "engine": "bing_rss",
                "status": status,
                "rank": rank,
                "title": title,
                "url": link,
                "snippet": description,
                "query": query,
            }
        )
        if len(results) >= limit:
            break
    return results


def bing_html_search(query: str, user_agent: str, limit: int) -> list[dict[str, Any]]:
    url = "https://www.bing.com/search?mkt=zh-CN&setlang=zh-Hans&q=" + quote(query, safe="")
    try:
        content, _content_type, status = request_text(url, user_agent=user_agent, timeout=25, retries=1)
    except RuntimeError:
        return []
    results: list[dict[str, Any]] = []
    for rank, match in enumerate(re.finditer(r'<a\s+href="(https?://[^"]+)"[^>]*>(.*?)</a>', content, flags=re.I | re.S), start=1):
        url_value = unescape(match.group(1))
        title = normalize_text(re.sub(r"<[^>]+>", " ", unescape(match.group(2))))
        if not title or any(item["url"] == url_value for item in results):
            continue
        if "bing.com" in urlparse(url_value).netloc.lower():
            continue
        results.append(
            {
                "engine": "bing_html",
                "status": status,
                "rank": rank,
                "title": title,
                "url": url_value,
                "snippet": "",
                "query": query,
            }
        )
        if len(results) >= limit:
            break
    return results


def github_repo_search(query: str, user_agent: str, limit: int) -> list[dict[str, Any]]:
    url = "https://api.github.com/search/repositories?" + urlencode({"q": f"{query} minecraft", "per_page": min(limit, 10)})
    try:
        content, _content_type, status = request_text(url, user_agent=user_agent, timeout=25, retries=0)
    except RuntimeError:
        return []
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    items = data.get("items") if isinstance(data, dict) else []
    output: list[dict[str, Any]] = []
    for rank, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "engine": "github_repo_search",
                "status": status,
                "rank": rank,
                "title": item.get("full_name") or item.get("name") or "",
                "url": item.get("html_url") or "",
                "snippet": item.get("description") or "",
                "query": query,
            }
        )
    return output


def query_variants(query: str) -> list[str]:
    base = normalize_text(query).strip()
    variants = [base]
    tokens = query_tokens(base)
    if tokens:
        variants.append(" ".join(tokens[:4]))
    for token in tokens:
        variants.extend(split_cjk_compound(token))
    if "minecraft" not in base.lower():
        variants.append(f"{base} Minecraft")
    if "mod" not in base.lower() and "模组" not in base:
        variants.append(f"{base} Minecraft mod")
    if len(tokens) >= 2:
        variants.append(" ".join(tokens[:2]))
        variants.append(" ".join(tokens[:2] + ["Minecraft"]))
        variants.append(" ".join(tokens[-2:] + ["Minecraft"]))
    return dedupe(variants)[:8]


def query_tokens(query: str) -> list[str]:
    stop = {"minecraft", "mc", "mod", "mods", "玩法", "介绍", "详细", "一下", "哪些", "什么", "怎么"}
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", query.lower())
    output: list[str] = []
    for token in tokens:
        if token in stop:
            continue
        output.append(token)
    return dedupe(output)


def split_cjk_compound(token: str) -> list[str]:
    suffixes = ("拔刀剑", "整合包", "资源包", "材质包", "光影", "模组", "维度", "世界")
    output: list[str] = []
    for suffix in suffixes:
        if token.endswith(suffix) and token != suffix:
            prefix = token[: -len(suffix)].strip()
            if prefix:
                output.append(prefix)
            output.append(suffix)
    return output


def relevance_terms(query: str) -> list[str]:
    tokens = query_tokens(query)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        expanded.extend(split_cjk_compound(token))
    return dedupe([item for item in expanded if len(item) >= 2])


def relevance_score(query: str, title: str, snippet: str, text: str = "", url: str = "") -> float:
    terms = relevance_terms(query)
    if not terms:
        return 0.1
    haystack = f"{title}\n{snippet}\n{url}\n{text[:6000]}".lower()
    hits = sum(1 for term in terms if term.lower() in haystack)
    phrase_bonus = 1.0 if query.strip().lower() in haystack else 0.0
    mc_bonus = 0.5 if any(mark in haystack for mark in ("minecraft", "mc百科", "modrinth", "github", "wiki", "forge", "fabric")) else 0.0
    return hits / max(1, len(terms)) + phrase_bonus + mc_bonus


def should_skip_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not parsed.scheme.startswith("http"):
        return True
    if any(part in host for part in SKIP_HOST_PARTS):
        return True
    suffix = Path(parsed.path).suffix.lower()
    if suffix and suffix not in TEXT_EXTENSIONS and suffix in {".zip", ".jar", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".pdf"}:
        return True
    return False


def github_repo_parts(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    if len(parts) >= 3 and parts[2].lower() in {"issues", "pulls", "actions", "releases"}:
        return None
    return parts[0], parts[1]


def github_readme_candidates(url: str) -> list[tuple[str, str, str]]:
    repo = github_repo_parts(url)
    if not repo:
        return []
    owner, name = repo
    candidates: list[tuple[str, str, str]] = []
    for branch in ("main", "master"):
        for filename in ("README.md", "README.txt", "README.rst"):
            candidates.append(
                (
                    f"{owner}/{name} {branch} {filename}",
                    f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{filename}",
                    "github_readme",
                )
            )
    candidates.append((f"{owner}/{name} wiki Home", f"https://raw.githubusercontent.com/wiki/{owner}/{name}/Home.md", "github_wiki"))
    return candidates


def reader_url(url: str) -> str:
    return "https://r.jina.ai/http://" + url.removeprefix("http://").removeprefix("https://")


def page_to_markdown(result: dict[str, Any], url: str, content: str, content_type: str, fetched_at: str) -> tuple[str, str]:
    title = str(result.get("title") or url)
    tables = ""
    images = ""
    if "html" in content_type.lower() or re.search(r"<html|<body|<article|<main", content[:2000], flags=re.I):
        parser = _HTMLTextExtractor()
        parser.feed(content)
        parser.close()
        text = parser.text
        if parser.title:
            title = parser.title
        tables = tables_to_markdown(parser.tables)
        images = images_to_markdown(parser.images, url)
    elif content_type.lower().startswith("application/json"):
        try:
            text = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            text = content
    else:
        text = content
    text = normalize_text(unescape(text))
    lines = [
        f"# {title}",
        "",
        "<!-- source: web_discovery -->",
        "",
        "## Metadata",
        "",
        f"- **URL:** {url}",
        f"- **Search engine:** {result.get('engine', '')}",
        f"- **Search query:** {result.get('query', '')}",
        f"- **Search rank:** {result.get('rank', '')}",
        f"- **Snippet:** {result.get('snippet', '')}",
        f"- **Fetched at:** {fetched_at}",
        "",
        "## Content",
        "",
        text,
    ]
    if tables:
        lines.extend(["", "## Extracted Tables", "", tables])
    if images:
        lines.extend(["", "## Extracted Images", "", images])
    return title, "\n".join(lines).strip() + "\n"


def tables_to_markdown(tables: list[list[list[str]]]) -> str:
    blocks: list[str] = []
    for index, table in enumerate(tables, start=1):
        rows = [[cell.strip() for cell in row] for row in table if any(cell.strip() for cell in row)]
        if not rows:
            continue
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        header = normalized[0]
        body = normalized[1:] or [[""] * width]
        block = [f"### Table {index}", "", "| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
        block.extend("| " + " | ".join(row) + " |" for row in body[:80])
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def images_to_markdown(images: list[dict[str, str]], page_url: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for image in images:
        src = urljoin(page_url, image.get("src", ""))
        if not src or src in seen:
            continue
        seen.add(src)
        alt = image.get("alt") or "image"
        lines.append(f"- ![{alt}]({src})")
    return "\n".join(lines)


def unique_output_path(run_dir: Path, filename: str, digest: str) -> Path:
    path = run_dir / filename
    if not path.exists():
        return path
    return run_dir / f"{path.stem}_{digest[:8]}{path.suffix}"


def dedupe(items: list[str]) -> list[str]:
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


def fetch_web_discovery(
    dest_root: Path,
    query: str,
    user_agent: str,
    max_results: int,
    max_pages: int,
    delay: float,
    force: bool,
) -> dict[str, Any]:
    run_dir = dest_root / "web_discovery" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    ledger = load_ledger()
    global_urls, global_contents = build_global_indexes(ledger)
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    search_results: list[dict[str, Any]] = []
    for variant in query_variants(query):
        try:
            search_results.extend(bing_rss_search(variant, user_agent=user_agent, limit=max_results))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "search", "engine": "bing_rss", "query": variant, "error": str(exc)})
        search_results.extend(bing_html_search(variant, user_agent=user_agent, limit=max_results))
        search_results.extend(github_repo_search(variant, user_agent=user_agent, limit=min(max_results, 6)))
        time.sleep(delay)

    seen_urls: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for item in search_results:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        key = url.lower().rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)
        if should_skip_url(url):
            skipped.append({"url": url, "reason": "skip_host_or_non_text", "title": item.get("title", "")})
            continue
        score = relevance_score(query, str(item.get("title") or ""), str(item.get("snippet") or ""), url=url)
        if score < 0.34:
            skipped.append({"url": url, "reason": "search_result_low_relevance", "score": round(score, 3), "title": item.get("title", "")})
            continue
        candidates.append(item | {"search_relevance": round(score, 3)})

    expanded_candidates: list[dict[str, Any]] = []
    seen_fetch_urls: set[str] = set()
    for item in candidates:
        raw_candidates = github_readme_candidates(str(item["url"]))
        if raw_candidates:
            for title, fetch_url, kind in raw_candidates:
                key = fetch_url.lower()
                if key in seen_fetch_urls:
                    continue
                seen_fetch_urls.add(key)
                expanded_candidates.append(item | {"fetch_url": fetch_url, "fetch_title": title, "fetch_kind": kind})
            continue
        key = str(item["url"]).lower()
        if key not in seen_fetch_urls:
            seen_fetch_urls.add(key)
            expanded_candidates.append(item | {"fetch_url": item["url"], "fetch_title": item.get("title", ""), "fetch_kind": "generic"})

    for item in expanded_candidates[: max(1, max_pages)]:
        url = str(item["fetch_url"])
        fetch_url = reader_url(url)
        content = ""
        content_type = ""
        status_code = 0
        raw_html = ""
        try:
            content, content_type, status_code = request_text(fetch_url, user_agent=user_agent, timeout=40, retries=1)
            try:
                direct_content, direct_type, _direct_status = request_text(url, user_agent=user_agent, timeout=30, retries=1)
                if "html" in direct_type.lower() or re.search(r"<html|<body|<article|<main", direct_content[:2000], flags=re.I):
                    raw_html = direct_content
            except Exception:
                raw_html = ""
        except Exception as reader_exc:  # noqa: BLE001
            try:
                content, content_type, status_code = request_text(url, user_agent=user_agent, timeout=30, retries=1)
                if "html" in content_type.lower() or re.search(r"<html|<body|<article|<main", content[:2000], flags=re.I):
                    raw_html = content
            except Exception as direct_exc:  # noqa: BLE001
                if "HTTP Error 404" not in str(direct_exc):
                    errors.append({"stage": "fetch", "url": url, "reader_error": str(reader_exc), "direct_error": str(direct_exc)})
                continue
        if len(content.strip()) < 200:
            skipped.append({"url": url, "reason": "too_short", "status_code": status_code})
            continue
        page_score = relevance_score(query, str(item.get("title") or ""), str(item.get("snippet") or ""), text=content, url=url)
        if page_score < 0.55:
            skipped.append({"url": url, "reason": "page_low_relevance", "score": round(page_score, 3), "status_code": status_code})
            continue
        display_item = item | {"title": item.get("fetch_title") or item.get("title") or url}
        title, markdown = page_to_markdown(display_item, url, content, content_type, fetched_at)
        digest = content_hash(markdown)
        item_id = url.lower().rstrip("/")
        key = make_key("web_discovery", item_id)
        previous = ledger.get(key)
        url_key = global_url_key(url)
        content_key = "global_content:" + content_fingerprint(markdown)
        global_previous = global_urls.get(url_key) or global_contents.get(content_key)
        if global_previous and not force:
            skipped.append(
                {
                    "url": url,
                    "reason": "url_or_content_duplicate",
                    "previous_source": global_previous.get("source", ""),
                    "previous_path": global_previous.get("path", ""),
                    "score": round(page_score, 3),
                }
            )
            append_ledger(
                ledger_record(
                    source="web_discovery",
                    item_id=item_id,
                    title=title,
                    url=url,
                    text=markdown,
                    path=str(global_previous.get("path", "")),
                    query=query,
                    status="skipped_global_duplicate",
                    previous=global_previous,
                )
            )
            continue
        if previous and (previous.get("content_hash") == digest or same_record_content(previous, markdown)) and not force:
            skipped.append({"url": url, "reason": "unchanged", "previous_path": previous.get("path", ""), "score": round(page_score, 3)})
            append_ledger(
                ledger_record(
                    source="web_discovery",
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
        filename = f"web_{slugify(title, 'page')}.md"
        path = unique_output_path(run_dir, filename, digest)
        raw_path = raw_dir / f"{path.stem}.html"
        path.write_text(markdown, encoding="utf-8")
        if raw_html:
            raw_path.write_text(raw_html, encoding="utf-8")
        record = ledger_record(
            source="web_discovery",
            item_id=item_id,
            title=title,
            url=url,
            text=markdown,
            path=str(path),
            query=query,
            status="updated" if previous else "new",
            previous=previous,
        )
        append_ledger(record)
        global_urls[url_key] = record
        global_contents[content_key] = record
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
                "engine": item.get("engine", ""),
                "kind": item.get("fetch_kind", ""),
            }
        )
        time.sleep(delay)

    manifest = {
        "manifest_type": "web_discovery_export",
        "created_at": fetched_at,
        "export_dir": str(run_dir),
        "query": query,
        "query_variants": query_variants(query),
        "search_results": search_results[:100],
        "candidates": len(candidates),
        "expanded_candidates": len(expanded_candidates),
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover public MC-related pages from web search, convert them to Markdown, and save for MCagent RAG.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_web_discovery(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        user_agent=args.user_agent,
        max_results=max(1, min(args.max_results, 30)),
        max_pages=max(1, min(args.max_pages, 30)),
        delay=max(0.0, args.delay),
        force=args.force,
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
