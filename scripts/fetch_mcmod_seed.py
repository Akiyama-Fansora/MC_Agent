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
from urllib.parse import quote, urljoin, urlparse
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


BASE_URL = "https://www.mcmod.cn"
SEARCH_BASE = "https://search.mcmod.cn"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (mcmod crawler; D:/magic/MC_Agent)"


def slugify(value: str, fallback: str = "mcmod") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:90] or fallback


def request_text(url: str, user_agent: str, cookie: str = "", retries: int = 2) -> tuple[str, str, int, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=35) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), response.headers.get("Content-Type", ""), int(response.status), response.geturl()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"MC百科 request failed: {last_error}")


def follow_cookie_gate(html: str, final_url: str, user_agent: str, cookie: str) -> tuple[str, str, int, str]:
    """MC百科 sometimes returns a small JS page that sets yxd_token then redirects."""
    cookie_match = re.search(r"document\.cookie\s*=\s*['\"]([^'\"]+)['\"]", html)
    href_match = re.search(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", html)
    if not cookie_match or not href_match:
        return html, "", 200, final_url
    next_cookie = cookie_match.group(1).strip() or cookie
    next_url = urljoin(final_url or BASE_URL + "/", href_match.group(1).strip())
    return request_text(next_url, user_agent, cookie=next_cookie, retries=1)


def bootstrap_cookie(user_agent: str) -> str:
    try:
        html, _content_type, _status, _url = request_text(BASE_URL + "/", user_agent, retries=1)
    except Exception:
        return ""
    match = re.search(r"document\.cookie\s*=\s*['\"]([^'\"]+)['\"]", html)
    return match.group(1).strip() if match else ""


def search_url(query: str) -> str:
    return f"{SEARCH_BASE}/s?key={quote(query, safe='')}"


def is_mcmod_content_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host not in {"www.mcmod.cn", "mcmod.cn"}:
        return False
    if "/class/category/" in path:
        return False
    return bool(re.match(r"/(?:class|item|post|modpack|course)/[^/?#]+\.html$", path))


def clean_html_fragment(fragment: str) -> str:
    fragment = re.sub(r"<em>(.*?)</em>", r"\1", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return normalize_text(unescape(fragment))


def parse_search_results(html: str, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for block in re.findall(r'<div class="result-item">(.*?)(?=<div class="result-item">|</div>\s*<div class="pages"|$)', html, flags=re.I | re.S):
        head = re.search(r'<div class="head">(.*?)</div>\s*<div class="body">', block, flags=re.I | re.S)
        body = re.search(r'<div class="body">(.*?)</div>', block, flags=re.I | re.S)
        link = re.search(r'href="(https?://www\.mcmod\.cn/(?:class|item|post|modpack|course)/[^"#?]+?\.html)"[^>]*>(.*?)</a>', block, flags=re.I | re.S)
        if not link:
            continue
        url = link.group(1)
        parsed_path = urlparse(url).path.lower()
        if "/class/category/" in parsed_path:
            continue
        title = clean_html_fragment(link.group(2))
        snippet = clean_html_fragment(body.group(1)) if body else ""
        if not title:
            title = url
        if any(item["url"] == url for item in results):
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def parse_bing_rss_results(xml_text: str, query: str, limit: int) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    results: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = clean_html_fragment(item.findtext("title") or "")
        url = (item.findtext("link") or "").strip()
        snippet = clean_html_fragment(item.findtext("description") or "")
        if not url or not is_mcmod_content_url(url):
            continue
        if any(existing["url"] == url for existing in results):
            continue
        results.append({"title": title or url, "url": url, "snippet": snippet, "matched_query": query, "engine": "bing_site_mcmod"})
        if len(results) >= limit:
            break
    return results


def external_mcmod_discovery(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    variants: list[str] = []
    for search_query in fallback_queries(query):
        variants.extend(
            [
                f"site:mcmod.cn {search_query}",
                f"site:www.mcmod.cn {search_query} MC百科",
            ]
        )
    for variant in list(dict.fromkeys(item for item in variants if item)):
        if len(results) >= limit:
            break
        url = "https://www.bing.com/search?format=rss&mkt=zh-CN&setlang=zh-Hans&q=" + quote(variant, safe="")
        try:
            xml_text, _content_type, _status, _final_url = request_text(url, user_agent, retries=1)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "external_search", "query": variant, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for item in parse_bing_rss_results(xml_text, variant, limit=max(1, limit - len(results))):
            if any(existing["url"] == item["url"] for existing in results):
                continue
            results.append(item)
            if len(results) >= limit:
                break
    return results, errors


def fallback_queries(query: str) -> list[str]:
    cleaned = normalize_text(query)
    variants = [cleaned]
    stop_terms = {
        "minecraft",
        "mc",
        "mod",
        "mods",
        "玩法",
        "攻略",
        "教程",
        "介绍",
        "详细",
        "资料",
        "获取",
        "获得",
        "步骤",
        "合成",
        "配方",
        "制作",
        "哪些",
        "有什么",
        "怎么",
        "如何",
        "一下",
        "完整",
        "数据",
        "入库",
        "索引",
        "切分",
        "mcagent",
        "rag",
    }
    tokens = [item for item in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", cleaned) if item.lower() not in stop_terms]
    generic_single_tokens = {
        "boss",
        "bosses",
        "BOSS",
        "Boss",
        "列表",
        "清单",
        "攻略",
        "打法",
        "教程",
        "介绍",
        "资料",
    }
    distinctive_tokens = [item for item in tokens if item not in generic_single_tokens and item.lower() not in generic_single_tokens]
    standalone_tokens = distinctive_tokens if distinctive_tokens else tokens
    if tokens:
        variants.append(" ".join(tokens[:4]))
        variants.append(" ".join(tokens[:2]))
        variants.extend(standalone_tokens)
        if len(tokens) >= 2:
            variants.append(f"{tokens[0]} {tokens[1]}")
            variants.append(f"{tokens[-2]} {tokens[-1]}")
        for token in standalone_tokens:
            variants.append(f"{token} MC百科")
            variants.append(f"{token} Minecraft")
    known_terms = [
        "拔刀剑",
        "暮色森林",
        "暮色",
        "机械动力",
        "应用能源",
        "通用机械",
        "植物魔法",
        "落幕曲",
        "乌托邦",
    ]
    for term in known_terms:
        if term in cleaned and cleaned != term:
            prefix = cleaned.replace(term, " ").strip()
            if prefix:
                if prefix not in generic_single_tokens and prefix.lower() not in generic_single_tokens:
                    variants.append(prefix)
                variants.append(f"{prefix} {term}")
            variants.append(term)
    for suffix in ("玩法", "攻略", "教程", "有什么", "有哪些", "介绍一下", "详细介绍一下", "获取步骤", "如何获取", "怎么获取", "合成配方"):
        stripped = cleaned.replace(suffix, " ").strip()
        if stripped and stripped != cleaned:
            variants.append(stripped)
    anchors = [item for item in standalone_tokens if item]
    if not anchors:
        anchors = [term for term in known_terms if term in cleaned]

    def has_anchor(item: str) -> bool:
        if not anchors:
            return True
        lowered = item.lower()
        return any(anchor in item or anchor.lower() in lowered for anchor in anchors)

    return list(dict.fromkeys(item for item in variants if item and has_anchor(item)))


def page_to_markdown(title: str, url: str, html: str, fetched_at: str, query: str, snippet: str) -> tuple[str, str]:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    text, html_title = parser.text, parser.title
    title = html_title or title
    text = normalize_text(text)
    tables = tables_to_markdown(parser.tables)
    images = images_to_markdown(parser.images, url)
    lines = [
        f"# {title}",
        "",
        "<!-- source: mcmod_search -->",
        "",
        "## Metadata",
        "",
        f"- **MC百科 URL:** {url}",
        f"- **Search query:** {query}",
        f"- **Search snippet:** {snippet}",
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


def fetch_mcmod(dest_root: Path, query: str, limit: int, user_agent: str, delay: float, force: bool) -> dict[str, Any]:
    run_dir = dest_root / "mcmod" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    ledger = load_ledger()
    global_urls, global_contents = build_global_indexes(ledger)
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    cookie = bootstrap_cookie(user_agent)
    search_results: list[dict[str, str]] = []
    external_search_results: list[dict[str, str]] = []
    search_status = 0
    final_url = search_url(query)
    searched_queries: list[str] = []
    for search_query in fallback_queries(query):
        if len(search_results) >= limit:
            break
        searched_queries.append(search_query)
        try:
            search_html, _content_type, status, final_url = request_text(search_url(search_query), user_agent, cookie=cookie)
            if "document.cookie" in search_html and "window.location.href" in search_html:
                search_html, _content_type, status, final_url = follow_cookie_gate(search_html, final_url, user_agent, cookie)
            search_status = status
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "search", "query": search_query, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for item in parse_search_results(search_html, limit=limit):
            if any(existing["url"] == item["url"] for existing in search_results):
                continue
            item["matched_query"] = search_query
            search_results.append(item)
            if len(search_results) >= limit:
                break
    if len(search_results) < limit:
        external_search_results, external_errors = external_mcmod_discovery(query, user_agent, limit=max(1, limit - len(search_results)))
        errors.extend(external_errors)
        for item in external_search_results:
            if any(existing["url"] == item["url"] for existing in search_results):
                continue
            search_results.append(item)
            if len(search_results) >= limit:
                break
    for result in search_results:
        url = result["url"]
        try:
            html, content_type, status_code, final_page_url = request_text(url, user_agent, cookie=cookie)
            if "document.cookie" in html and "window.location.href" in html:
                html, content_type, status_code, final_page_url = follow_cookie_gate(html, final_page_url, user_agent, cookie)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "page", "url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if len(html.strip()) < 300:
            skipped.append({"url": url, "reason": "too_short", "status_code": status_code})
            continue
        title, markdown = page_to_markdown(result["title"], final_page_url or url, html, fetched_at, result.get("matched_query") or query, result.get("snippet", ""))
        digest = content_hash(markdown)
        item_id = final_page_url.lower().rstrip("/") if final_page_url else url.lower().rstrip("/")
        key = make_key("mcmod", item_id)
        previous = ledger.get(key)
        url_key = global_url_key(final_page_url or url)
        content_key = "global_content:" + content_fingerprint(markdown)
        global_previous = global_urls.get(url_key) or global_contents.get(content_key)
        if global_previous and not force:
            skipped.append(
                {
                    "title": title,
                    "url": final_page_url or url,
                    "reason": "url_or_content_duplicate",
                    "previous_source": global_previous.get("source", ""),
                    "previous_path": global_previous.get("path", ""),
                }
            )
            append_ledger(
                ledger_record(
                    source="mcmod",
                    item_id=item_id,
                    title=title,
                    url=final_page_url or url,
                    text=markdown,
                    path=str(global_previous.get("path", "")),
                    query=query,
                    status="skipped_global_duplicate",
                    previous=global_previous,
                )
            )
            continue
        if previous and (previous.get("content_hash") == digest or same_record_content(previous, markdown)) and not force:
            skipped.append({"title": title, "url": url, "reason": "unchanged", "previous_path": previous.get("path", "")})
            append_ledger(
                ledger_record(
                    source="mcmod",
                    item_id=item_id,
                    title=title,
                    url=final_page_url or url,
                    text=markdown,
                    path=str(previous.get("path", "")),
                    query=query,
                    status="skipped_unchanged",
                    previous=previous,
                )
            )
            continue
        prefix = "mcmod_class" if "/class/" in url else "mcmod_page"
        path = run_dir / f"{prefix}_{slugify(title)}_{digest[:8]}.md"
        raw_path = raw_dir / f"{prefix}_{slugify(title)}_{digest[:8]}.html"
        path.write_text(markdown, encoding="utf-8")
        raw_path.write_text(html, encoding="utf-8")
        record = ledger_record(
            source="mcmod",
            item_id=item_id,
            title=title,
            url=final_page_url or url,
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
                "url": final_page_url or url,
                "path": str(path),
                "raw_html_path": str(raw_path),
                "chars": len(markdown),
                "raw_html_chars": len(html),
                "status_code": status_code,
            }
        )
        time.sleep(delay)

    manifest = {
        "manifest_type": "mcmod_search_export",
        "created_at": fetched_at,
        "export_dir": str(run_dir),
        "query": query,
        "search_url": final_url,
        "search_status": search_status,
        "searched_queries": searched_queries,
        "search_results": search_results,
        "external_search_results": external_search_results,
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search MC百科, fetch result pages, and save Markdown for MCagent RAG.")
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "data" / "crawler_exports"))
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_mcmod(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        limit=max(1, min(args.limit, 30)),
        user_agent=args.user_agent,
        delay=max(0.0, args.delay),
        force=args.force,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Search results: {len(manifest['search_results'])}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0 if not manifest["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
