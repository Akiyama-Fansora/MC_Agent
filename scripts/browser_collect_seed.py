from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_USER_AGENT = ""


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def detect_start_url(query: str, start_url: str = "") -> str:
    query = query.strip()
    if start_url:
        return start_url
    if query.startswith(("http://", "https://")):
        return query
    lowered = query.lower()
    if "taobao" in lowered or "淘宝" in query:
        term = re.sub(r"(?i)taobao", " ", query)
        for token in ("淘宝", "商品", "价格", "链接", "名称", "采集", "获取", "搜索", "50个", "50 个"):
            term = term.replace(token, " ")
        term = re.sub(r"\s+", " ", term).strip() or "热门商品"
        return "https://s.taobao.com/search?q=" + quote(term)
    return "https://www.bing.com/search?q=" + quote(query)


def parse_price(text: str) -> str:
    patterns = [
        r"[$¥￥€£]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"(?:price|价格|售价|到手价|券后)\s*[:：]?\s*[$¥￥€£]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"\b([0-9]{1,6}\.[0-9]{2})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).replace(",", "")
    return ""


def compact_name(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if parse_price(line) and len(line) <= 32:
            continue
        if any(token.lower() in line.lower() for token in ("add to cart", "reviews", "rating", "付款", "包邮", "退货", "广告")):
            continue
        lines.append(line)
    value = " ".join(lines)
    value = re.sub(r"[$¥￥€£]\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_，,。.")
    return value[:180]


def normalize_url(page_url: str, href: str) -> str:
    return urljoin(page_url, href).strip()


def add_record(records: list[dict[str, str]], seen: set[str], *, name: str, price: str, url: str, source: str, max_items: int) -> bool:
    name = compact_name(name)
    raw_price = re.sub(r"\s+", " ", str(price or "")).strip()
    price = parse_price(raw_price) or (raw_price if re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", raw_price) else "") or parse_price(name)
    if not name or not url:
        return False
    key = re.sub(r"[?#].*$", "", url).lower()
    if key in seen:
        return False
    seen.add(key)
    records.append({"name": name, "price": price, "url": url, "source": source})
    return len(records) >= max_items


def collect_known_market_links(page: Any, max_items: int, seen: set[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    selector = 'a[href*="item.taobao.com"], a[href*="detail.tmall.com"], a[href*="world.taobao.com/item"], a[href*="detail.1688.com"]'
    anchors = page.locator(selector)
    count = min(anchors.count(), max_items * 8)
    for index in range(count):
        try:
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href") or ""
            if not href:
                continue
            url = normalize_url(page.url, href)
            text = ""
            for ancestor in [
                "xpath=ancestor::*[self::div or self::li or self::article][1]",
                "xpath=ancestor::*[self::div or self::li or self::article][2]",
                "xpath=ancestor::*[self::div or self::li or self::article][3]",
            ]:
                try:
                    candidate = anchor.locator(ancestor).first.inner_text(timeout=800)
                except Exception:
                    candidate = ""
                if len(candidate) > len(text):
                    text = candidate
            if not text:
                text = anchor.inner_text(timeout=800)
            if add_record(records, seen, name=text, price=text, url=url, source=page.url, max_items=max_items):
                break
        except Exception:
            continue
    return records


def collect_generic_cards(page: Any, max_items: int, seen: set[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    selectors = [
        ".thumbnail",
        ".card",
        ".product",
        ".product-item",
        ".product-card",
        "[class*='product']",
        "article",
        "li",
    ]
    for selector in selectors:
        cards = page.locator(selector)
        try:
            count = min(cards.count(), max_items * 5)
        except Exception:
            continue
        for index in range(count):
            try:
                card = cards.nth(index)
                text = card.inner_text(timeout=800)
                price = parse_price(text)
                if not price:
                    continue
                link = card.locator("a[href]").first
                href = link.get_attribute("href", timeout=800) or ""
                if not href:
                    continue
                url = normalize_url(page.url, href)
                name = ""
                for name_selector in [".title", "[class*='title']", "h1", "h2", "h3", "h4", "a[href]"]:
                    try:
                        candidate = card.locator(name_selector).first.inner_text(timeout=500)
                    except Exception:
                        candidate = ""
                    candidate = compact_name(candidate)
                    if candidate and len(candidate) > len(name):
                        name = candidate
                if not name:
                    name = text
                if add_record(records, seen, name=name, price=price, url=url, source=page.url, max_items=max_items):
                    return records
            except Exception:
                continue
    return records


def collect_from_json_text(text: str, page_url: str, max_items: int, seen: set[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    url_pattern = r"https?:\\/\\/(?:item\\.taobao\\.com|detail\\.tmall\\.com)[^\"'\\\s<]+|https?://(?:item\.taobao\.com|detail\.tmall\.com)[^\"'\s<]+"
    for match in re.finditer(url_pattern, text):
        item_url = match.group(0).replace("\\/", "/")
        window = text[max(0, match.start() - 800) : match.end() + 800]
        name_match = re.search(r'"(?:title|raw_title|name)"\s*:\s*"([^"]{4,180})"', window)
        name = name_match.group(1) if name_match else ""
        price = parse_price(window)
        if add_record(records, seen, name=name, price=price, url=item_url, source=page_url, max_items=max_items):
            break
    return records


def write_outputs(out_dir: Path, records: list[dict[str, str]], manifest: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "items.json"
    csv_path = out_dir / "items.csv"
    report_path = out_dir / "report.md"
    manifest_path = out_dir / "manifest.json"
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "price", "url", "source"])
        writer.writeheader()
        writer.writerows(records)
    lines = [
        "# Browser Collect Report",
        "",
        f"- records: {len(records)}",
        f"- status: {manifest.get('status')}",
        f"- query: {manifest.get('query')}",
        f"- start_url: {manifest.get('start_url')}",
        f"- screenshot: {manifest.get('screenshot_path') or ''}",
        f"- raw_html: {manifest.get('raw_html_path') or ''}",
        "",
        "## Reason",
        "",
        manifest.get("failure_reason") or manifest.get("note") or "Collected successfully.",
    ]
    report_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    manifest["records"] = records
    manifest["files"] = {
        "json": str(json_path),
        "csv": str(csv_path),
        "report": str(report_path),
        "manifest": str(manifest_path),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def run_collect(args: argparse.Namespace) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    query = args.query.strip()
    start_url = detect_start_url(query, args.start_url.strip())
    run_root = Path(args.dest).resolve() / "browser_collect" / now_slug()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else run_root
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_page.html"
    screenshot_path = output_dir / "page.png"
    errors: list[dict[str, str]] = []
    skipped: list[dict[str, Any]] = []
    records: list[dict[str, str]] = []
    status = "ok"
    note = ""
    failure_reason = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context_options: dict[str, Any] = {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1365, "height": 900},
        }
        if args.user_agent:
            context_options["user_agent"] = args.user_agent
        context = browser.new_context(**context_options)
        page = context.new_page()
        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(args.timeout_ms, 10000))
            except Exception:
                pass
            for _ in range(2):
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(500)
            html = page.content()
            text = page.locator("body").inner_text(timeout=5000)
            raw_path.write_text(html, encoding="utf-8", errors="replace")
            page.screenshot(path=str(screenshot_path), full_page=True)
            block_markers = ("验证码", "安全验证", "滑块", "登录", "请登录", "访问受限", "账号登录", "captcha", "login required")
            if any(marker.lower() in text.lower() for marker in block_markers) and len(text) < 5000:
                status = "blocked_or_login_required"
                failure_reason = "The page appears to require login, captcha, or security verification. Screenshot and raw HTML were saved; the tool did not try to bypass verification."
            seen: set[str] = set()
            records.extend(collect_known_market_links(page, args.max_items, seen))
            if len(records) < args.max_items:
                records.extend(collect_generic_cards(page, args.max_items - len(records), seen))
            if len(records) < args.max_items:
                records.extend(collect_from_json_text(html, page.url, args.max_items - len(records), seen))
            if not records and status == "ok":
                status = "no_items_found"
                failure_reason = "The page was reachable, but no recognizable product rows/cards were found. Try a direct product-list URL, a different public demo site, or a site-specific selector tool."
        except Exception as exc:  # noqa: BLE001
            status = "error"
            failure_reason = f"{type(exc).__name__}: {exc}"
            errors.append({"stage": "browser_collect", "error": failure_reason})
        finally:
            browser.close()

    if records:
        note = f"Collected {len(records)} structured records."
    else:
        note = failure_reason
    if len(records) < args.max_items:
        skipped.append({"reason": "fewer_records_than_requested", "requested": args.max_items, "collected": len(records)})
    manifest = {
        "provider": "browser_collect",
        "query": query,
        "start_url": start_url,
        "output_dir": str(output_dir),
        "export_dir": str(output_dir),
        "status": status,
        "note": note,
        "failure_reason": failure_reason,
        "record_count": len(records),
        "requested_count": args.max_items,
        "errors": errors,
        "skipped": skipped,
        "raw_html_path": str(raw_path) if raw_path.exists() else "",
        "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_outputs(output_dir, records, manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="General browser-driven structured data collection for CrawlerAgent.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--query", required=True)
    parser.add_argument("--start-url", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.max_items = max(1, min(int(args.max_items), 200))
    args.timeout_ms = max(5000, min(int(args.timeout_ms), 120000))
    manifest = run_collect(args)
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Status: {manifest['status']}")
    print(f"Records: {manifest['record_count']}")
    print(f"JSON: {manifest['files']['json']}")
    print(f"CSV: {manifest['files']['csv']}")
    print(f"Report: {manifest['files']['report']}")
    if manifest.get("failure_reason"):
        print(f"Reason: {manifest['failure_reason']}")
    if manifest.get("errors"):
        print(json.dumps(manifest["errors"], ensure_ascii=False))
    return 0 if manifest["status"] in {"ok", "blocked_or_login_required", "no_items_found"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
