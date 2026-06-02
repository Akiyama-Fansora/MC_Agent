from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.cleaners import _HTMLTextExtractor, normalize_text  # noqa: E402
from mcagent.provider_registry import request_text, slugify  # noqa: E402


URL_RE = re.compile(r"https?://[^\s<>'\"，。；、）\])]+", flags=re.I)
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; MC_Agent fetch_url; +https://github.com/Akiyama-Fansora/MC_Agent)"
ARCHIVE_EXTENSIONS = (".mrpack", ".zip")


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def extract_url(value: str) -> str:
    match = URL_RE.search(str(value or ""))
    if not match:
        return ""
    return match.group(0).rstrip(".,;:")


def html_to_markdown(raw: str, content_type: str, url: str) -> tuple[str, str]:
    if "html" not in str(content_type or "").lower() and "<html" not in raw[:1000].lower():
        text = normalize_text(raw)
        return url, text
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    title = normalize_text(parser.title) or url
    text = normalize_text(parser.text)
    return title, text


def is_archive_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(ARCHIVE_EXTENSIONS)


def archive_redirect_manifest(url: str, output_root: Path) -> dict[str, object]:
    run_dir = output_root / "fetch_url" / now_slug()
    run_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    failure_reason = "URL points to a binary modpack archive; fetch_url only extracts readable text. Use modpack_download for objective archive probing/downloading."
    report_path = run_dir / "archive_url_redirect.md"
    report_path.write_text(
        "\n".join(
            [
                "# Archive URL passed to fetch_url",
                "",
                "<!-- source: fetch_url_archive_redirect -->",
                "",
                "## Objective Facts",
                "",
                f"- URL: {url}",
                "- detected_type: modpack_archive_url",
                "- accepted_by_tool: false",
                "- recommended_source: modpack_download",
                f"- failure_reason: {failure_reason}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = {
        "manifest_type": "fetch_url_export",
        "provider": "fetch_url",
        "created_at": fetched_at,
        "export_dir": str(run_dir),
        "query": url,
        "records": [],
        "skipped": [
            {
                "url": url,
                "reason": "binary_modpack_archive_url",
                "recommended_source": "modpack_download",
                "report_path": str(report_path),
            }
        ],
        "errors": [],
        "status": "blocked",
        "archive_url_detected": True,
        "failure_reason": failure_reason,
        "next_action": "CrawlerAgent should schedule modpack_download for this exact URL, then modpack_internal after a real local archive is downloaded.",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def save_url(url: str, output_root: Path, *, timeout: int, user_agent: str) -> dict[str, object]:
    if is_archive_url(url):
        return archive_redirect_manifest(url, output_root)
    run_dir = output_root / "fetch_url" / now_slug()
    raw_dir = run_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    errors: list[dict[str, str]] = []
    records: list[dict[str, object]] = []
    try:
        raw, content_type, status_code = request_text(url, user_agent=user_agent, timeout=timeout, retries=1)
        title, text = html_to_markdown(raw, content_type, url)
        if len(text) < 80:
            errors.append({"url": url, "error": "extracted_text_too_short"})
        digest = hashlib.sha256((url + "\n" + text).encode("utf-8", errors="ignore")).hexdigest()
        parsed = urlparse(url)
        stem = slugify(title or parsed.netloc or "page", "page")[:80]
        raw_path = raw_dir / f"{stem}_{digest[:8]}.html"
        page_path = run_dir / f"{stem}_{digest[:8]}.md"
        raw_path.write_text(raw, encoding="utf-8", errors="replace")
        markdown = (
            f"# {title}\n\n"
            "<!-- source: fetch_url -->\n\n"
            "## Metadata\n\n"
            f"- **URL:** {url}\n"
            f"- **Fetched at:** {fetched_at}\n"
            f"- **Status:** {status_code}\n"
            f"- **Content-Type:** {content_type}\n\n"
            "## Content\n\n"
            f"{text}\n"
        )
        page_path.write_text(markdown, encoding="utf-8")
        records.append(
            {
                "title": title,
                "url": url,
                "path": str(page_path),
                "raw_html_path": str(raw_path),
                "status_code": status_code,
                "content_type": content_type,
                "chars": len(text),
                "content_hash": digest,
            }
        )
    except Exception as exc:  # noqa: BLE001
        errors.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
    manifest = {
        "manifest_type": "fetch_url_export",
        "provider": "fetch_url",
        "created_at": fetched_at,
        "export_dir": str(run_dir),
        "query": url,
        "records": records,
        "skipped": [],
        "errors": errors,
        "status": "ok" if records and not errors else "failed",
        "failure_reason": "; ".join(item["error"] for item in errors),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a public URL with local HTTP and extract readable text.")
    parser.add_argument("--query", required=True, help="URL or natural text containing a URL.")
    parser.add_argument("--output-root", default=str(ROOT / "data" / "crawler_exports"))
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args()

    url = extract_url(args.query)
    if not url:
        print(json.dumps({"provider": "fetch_url", "status": "failed", "failure_reason": "No URL found in query."}, ensure_ascii=False, indent=2))
        return 2
    manifest = save_url(url, Path(args.output_root), timeout=args.timeout, user_agent=args.user_agent)
    print(json.dumps({"export_dir": manifest["export_dir"], "records": len(manifest["records"]), "errors": len(manifest["errors"])}, ensure_ascii=False, indent=2))
    print(f"Exported to: {manifest['export_dir']}")
    return 0 if manifest["records"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
