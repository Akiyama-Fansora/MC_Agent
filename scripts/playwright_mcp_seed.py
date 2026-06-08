from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote
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
    relevance_score,
    request_text,
)


DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (playwright-mcp-style browser tool; D:/magic/MC_Agent)"


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def search_candidates(query: str, user_agent: str, max_results: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if query.startswith(("http://", "https://")):
        return ([{"engine": "direct_url", "rank": 1, "title": query, "url": query, "snippet": "", "query": query}], [])
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for variant in neutral_query_variants(query):
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
                    "engine": "bing_rss_for_playwright_mcp",
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


def safe_slug(value: str, fallback: str = "page") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return cleaned[:80] or fallback


def compact_text(value: str, limit: int = 260) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def neutral_query_variants(query: str) -> list[str]:
    base = compact_text(query, 500)
    variants = [base]
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", base.lower())
    if tokens:
        variants.append(" ".join(dict.fromkeys(tokens[:8])))
    return list(dict.fromkeys(item for item in variants if item.strip()))[:4]


def element_label(role: str, name: str, text: str, href: str = "") -> str:
    pieces = [role]
    if name:
        pieces.append(f'"{compact_text(name, 90)}"')
    elif text:
        pieces.append(f'"{compact_text(text, 90)}"')
    if href:
        pieces.append(f"href={href[:140]}")
    return " ".join(pieces)


def page_snapshot(page: Any, *, depth: int = 3, max_nodes: int = 140) -> tuple[str, list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    lines: list[str] = []

    def add(selector: str, role: str, index: int) -> None:
        if len(actions) >= max_nodes:
            return
        locator = page.locator(selector).nth(index)
        try:
            visible = locator.is_visible(timeout=300)
        except Exception:
            visible = False
        if not visible:
            return
        try:
            text = locator.inner_text(timeout=500)
        except Exception:
            text = ""
        try:
            name = locator.get_attribute("aria-label", timeout=300) or locator.get_attribute("title", timeout=300) or ""
        except Exception:
            name = ""
        try:
            href = locator.get_attribute("href", timeout=300) or ""
        except Exception:
            href = ""
        try:
            placeholder = locator.get_attribute("placeholder", timeout=300) or ""
        except Exception:
            placeholder = ""
        try:
            box = locator.bounding_box(timeout=500) or {}
        except Exception:
            box = {}
        ref = f"{role}[{index}]"
        label = element_label(role, name or placeholder, text, href)
        actions.append(
            {
                "ref": ref,
                "role": role,
                "selector": selector,
                "index": index,
                "name": compact_text(name or placeholder, 160),
                "text": compact_text(text, 240),
                "href": href,
                "box": box,
            }
        )
        lines.append(f"- {ref}: {label}")

    selectors = [
        ("a[href]", "link"),
        ("button", "button"),
        ("input:not([type=hidden])", "textbox"),
        ("textarea", "textbox"),
        ("select", "combobox"),
        ("summary", "summary"),
        ("[role=button]", "button"),
        ("[role=link]", "link"),
        ("[role=tab]", "tab"),
    ]
    for selector, role in selectors:
        try:
            count = min(page.locator(selector).count(), 35)
        except Exception:
            continue
        for index in range(count):
            add(selector, role, index)

    headings: list[str] = []
    for selector in ("h1", "h2", "h3"):
        try:
            count = min(page.locator(selector).count(), 20)
        except Exception:
            continue
        for index in range(count):
            try:
                text = compact_text(page.locator(selector).nth(index).inner_text(timeout=500), 160)
            except Exception:
                text = ""
            if text:
                headings.append(f"- {selector}: {text}")
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        pass
    excerpt_lines = [compact_text(line, 220) for line in str(body_text).splitlines() if compact_text(line, 220)][:80]
    snapshot = "\n".join(
        [
            f"# Page Snapshot: {page.title()}",
            "",
            f"- url: {page.url}",
            "",
            "## Headings",
            *(headings[:40] or ["- none"]),
            "",
            "## Interactive Elements",
            *(lines[:max_nodes] or ["- none"]),
            "",
            "## Visible Text Excerpt",
            *[f"- {line}" for line in excerpt_lines[: max(20, depth * 20)]],
        ]
    ).strip()
    return snapshot + "\n", actions


def apply_actions(page: Any, actions: list[dict[str, Any]], errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    executed: list[dict[str, Any]] = []
    for step_index, action in enumerate(actions[:20], start=1):
        if not isinstance(action, dict):
            continue
        op = str(action.get("action") or action.get("op") or "").strip().lower()
        selector = str(action.get("selector") or action.get("target") or "").strip()
        value = str(action.get("text") or action.get("value") or action.get("url") or "").strip()
        record = {"step": step_index, "action": op, "selector": selector, "value": value[:200], "status": "ok"}
        try:
            if op in {"navigate", "goto"} and value:
                page.goto(value, wait_until="domcontentloaded", timeout=30000)
            elif op in {"click", "double_click"} and selector:
                locator = page.locator(selector).first
                locator.dblclick(timeout=5000) if op == "double_click" else locator.click(timeout=5000)
            elif op in {"type", "fill"} and selector:
                page.locator(selector).first.fill(value, timeout=5000)
            elif op == "press" and selector and value:
                page.locator(selector).first.press(value, timeout=5000)
            elif op == "wait_for_text" and value:
                page.get_by_text(value).first.wait_for(timeout=10000)
            elif op == "wait":
                page.wait_for_timeout(max(0, min(int(action.get("ms") or action.get("time_ms") or 1000), 10000)))
            else:
                record["status"] = "skipped"
                record["reason"] = "unsupported_or_missing_selector"
        except Exception as exc:  # noqa: BLE001
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            errors.append({"stage": "action", "step": str(step_index), "error": record["error"]})
        executed.append(record)
    return executed


def render_page(url: str, *, output_dir: Path, timeout_ms: int, headful: bool, actions: list[dict[str, Any]], snapshot_depth: int) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_page.html"
    snapshot_path = output_dir / "snapshot.md"
    screenshot_path = output_dir / "page.png"
    console_path = output_dir / "console.json"
    network_path = output_dir / "network.json"
    actions_path = output_dir / "actions.json"
    errors: list[dict[str, str]] = []
    console_messages: list[dict[str, Any]] = []
    network_requests: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1365, "height": 900},
        )
        page = context.new_page()
        page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text[:1000]}))
        page.on(
            "request",
            lambda req: network_requests.append({"method": req.method, "url": req.url, "resource_type": req.resource_type}),
        )
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
        except Exception:
            pass
        executed_actions = apply_actions(page, actions, errors)
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
        except Exception:
            pass
        for _ in range(2):
            page.mouse.wheel(0, 1000)
            page.wait_for_timeout(250)
        title = page.title()
        html = page.content()
        try:
            text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", html)
        snapshot, action_targets = page_snapshot(page, depth=snapshot_depth)
        raw_path.write_text(html, encoding="utf-8", errors="replace")
        snapshot_path.write_text(snapshot, encoding="utf-8")
        page.screenshot(path=str(screenshot_path), full_page=True)
        console_path.write_text(json.dumps(console_messages[-80:], ensure_ascii=False, indent=2), encoding="utf-8")
        network_path.write_text(json.dumps(network_requests[-180:], ensure_ascii=False, indent=2), encoding="utf-8")
        actions_path.write_text(
            json.dumps({"executed_actions": executed_actions, "action_targets": action_targets[:140]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        browser.close()
    return {
        "title": title,
        "url": url,
        "final_url": page.url if "page" in locals() else url,
        "text": text,
        "html": html,
        "snapshot": snapshot,
        "paths": {
            "raw_html": str(raw_path),
            "snapshot": str(snapshot_path),
            "screenshot": str(screenshot_path),
            "console": str(console_path),
            "network": str(network_path),
            "actions": str(actions_path),
        },
        "console_count": len(console_messages),
        "network_count": len(network_requests),
        "errors": errors,
    }


def parse_actions(value: str) -> list[dict[str, Any]]:
    if not value:
        return []
    path = Path(value)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(value)
    except Exception:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("actions"), list):
        return [item for item in data["actions"] if isinstance(item, dict)]
    return []


def fetch_playwright_mcp(
    dest_root: Path,
    query: str,
    max_results: int,
    max_pages: int,
    force: bool,
    user_agent: str,
    timeout_ms: int,
    headful: bool,
    actions: list[dict[str, Any]],
    snapshot_depth: int,
) -> dict[str, Any]:
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
        candidates.append(item | {"search_relevance": round(score, 3)})

    run_root = dest_root.resolve() / "playwright" / now_slug()
    results: list[ProviderResult] = []
    for page_index, item in enumerate(candidates[: max(1, max_pages)], start=1):
        url = str(item["url"])
        page_dir = run_root / f"page_{page_index}_{safe_slug(str(item.get('title') or url))}"
        try:
            rendered = render_page(
                url,
                output_dir=page_dir,
                timeout_ms=timeout_ms,
                headful=headful,
                actions=actions,
                snapshot_depth=snapshot_depth,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "render", "url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue
        text = str(rendered.get("text") or "")
        page_score = relevance_score(query, str(rendered.get("title") or item.get("title") or ""), str(item.get("snippet") or ""), text=text, url=url)
        metadata = {
            "engine": "playwright_mcp_style",
            "source_query": item.get("query", ""),
            "search_relevance": item.get("search_relevance"),
            "final_url": rendered.get("final_url"),
            "raw_page_path": rendered["paths"]["raw_html"],
            "snapshot_path": rendered["paths"]["snapshot"],
            "screenshot_path": rendered["paths"]["screenshot"],
            "console_path": rendered["paths"]["console"],
            "network_path": rendered["paths"]["network"],
            "actions_path": rendered["paths"]["actions"],
            "console_count": rendered.get("console_count", 0),
            "network_count": rendered.get("network_count", 0),
            "render_errors": rendered.get("errors", []),
        }
        markdown = "\n\n".join(
            [
                str(rendered.get("snapshot") or ""),
                "## Extracted Body Text",
                text,
                "## Browser Evidence",
                "\n".join(f"- {key}: {value}" for key, value in rendered["paths"].items()),
            ]
        )
        results.append(
            ProviderResult(
                provider="playwright",
                stage="mcp_style_browser_snapshot",
                query=query,
                title=str(rendered.get("title") or item.get("title") or url),
                url=str(rendered.get("final_url") or url),
                markdown=markdown,
                raw_html=str(rendered.get("html") or ""),
                score=page_score,
                metadata=metadata,
            )
        )

    manifest = export_provider_results(
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
        extra_manifest={
            "provider_mode": "playwright_mcp_style",
            "mcp_reference": "D:/magic/Crawler/playwright-mcp-main/playwright-mcp-main",
            "candidates": len(candidates),
            "query_variants": neutral_query_variants(query),
            "snapshot_depth": snapshot_depth,
            "actions_requested": len(actions),
        },
    )
    _relocate_browser_evidence(manifest)
    return manifest


def _relocate_browser_evidence(manifest: dict[str, Any]) -> None:
    export_dir = Path(str(manifest.get("export_dir") or ""))
    if not export_dir:
        return
    evidence_root = export_dir / "browser_evidence"
    replacements: dict[str, str] = {}
    for record_index, record in enumerate(manifest.get("records") or [], start=1):
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        page_dir = evidence_root / f"page_{record_index}_{safe_slug(str(record.get('title') or 'page'))}"
        for key in ("raw_page_path", "snapshot_path", "screenshot_path", "console_path", "network_path", "actions_path"):
            old_value = str(metadata.get(key) or "").strip()
            if not old_value:
                continue
            old_path = Path(old_value)
            if not old_path.exists() or not old_path.is_file():
                continue
            page_dir.mkdir(parents=True, exist_ok=True)
            new_path = page_dir / old_path.name
            if old_path.resolve() != new_path.resolve():
                shutil.copy2(old_path, new_path)
            metadata[key] = str(new_path)
            replacements[old_value] = str(new_path)
        record["metadata"] = metadata
        markdown_path = Path(str(record.get("path") or ""))
        if markdown_path.exists() and replacements:
            text = markdown_path.read_text(encoding="utf-8", errors="replace")
            for old, new in replacements.items():
                text = text.replace(old, new)
            markdown_path.write_text(text, encoding="utf-8")
    manifest_path = export_dir / "manifest.json"
    if manifest_path.exists():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Playwright MCP-style browser snapshot/extraction tool for CrawlerAgent.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=6)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--snapshot-depth", type=int, default=3)
    parser.add_argument("--actions-json", default="", help="Optional JSON list or path with navigate/click/type/press/wait actions.")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_playwright_mcp(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        max_results=max(1, min(args.max_results, 12)),
        max_pages=max(1, min(args.max_pages, 8)),
        force=args.force,
        user_agent=args.user_agent,
        timeout_ms=max(5000, min(args.timeout_ms, 90000)),
        headful=bool(args.headful),
        actions=parse_actions(args.actions_json),
        snapshot_depth=max(1, min(int(args.snapshot_depth), 6)),
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Mode: {manifest.get('provider_mode') or 'playwright_mcp_style'}")
    print(f"Search results: {len(manifest['search_results'])}")
    print(f"Candidates: {manifest.get('candidates', 0)}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0 if manifest["records"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
