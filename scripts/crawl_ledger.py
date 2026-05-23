from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_PATH = PROJECT_ROOT / "data" / "crawl_ledger.jsonl"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "spm",
    "from",
    "share",
    "share_source",
    "timestamp",
}


def normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower().rstrip("/")
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    if host.endswith(":80") and scheme == "http":
        host = host[:-3]
    if host.endswith(":443") and scheme == "https":
        host = host[:-4]
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def content_fingerprint(text: str) -> str:
    stable = str(text or "")
    stable = re.sub(r"<!--\s*source:\s*[^>]+-->", "", stable, flags=re.I)
    stable = re.sub(r"\n## Metadata\n.*?(?=\n## |\Z)", "\n", stable, flags=re.S)
    stable = re.sub(r"\n+- \*\*(?:Fetched at|Query|Search query|Provider|Stage|Score|URL|MC百科 URL|Web source|Search rank):\*\*.*", "", stable, flags=re.I)
    stable = re.sub(r"\s+", " ", stable).strip().lower()
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def global_url_key(url: str) -> str:
    return "global_url:" + normalize_url(url)


def global_content_key(text: str) -> str:
    return "global_content:" + content_fingerprint(text)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_ledger(path: Path = DEFAULT_LEDGER_PATH) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(record.get("key") or "")
            if key:
                records[key] = record
    return records


def append_ledger(record: dict[str, Any], path: Path = DEFAULT_LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def make_key(source: str, item_id: str) -> str:
    return f"{source}:{item_id}".lower()


def build_global_indexes(records: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    url_index: dict[str, dict[str, Any]] = {}
    content_index: dict[str, dict[str, Any]] = {}
    for record in records.values():
        status = str(record.get("status") or "")
        if status.startswith("skipped"):
            continue
        url = str(record.get("url") or "")
        if url:
            url_index.setdefault(str(record.get("url_key") or global_url_key(url)), record)
        fingerprint = str(record.get("content_fingerprint") or "")
        if fingerprint:
            content_index.setdefault("global_content:" + fingerprint, record)
        elif record.get("content_hash"):
            content_index.setdefault("global_content_hash:" + str(record.get("content_hash")), record)
    return url_index, content_index


def record_content_fingerprint(record: dict[str, Any]) -> str:
    fingerprint = str(record.get("content_fingerprint") or "")
    if fingerprint:
        return fingerprint
    path = Path(str(record.get("path") or ""))
    if path.exists() and path.is_file():
        try:
            return content_fingerprint(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""
    return ""


def same_record_content(record: dict[str, Any] | None, text: str) -> bool:
    if not record:
        return False
    fingerprint = record_content_fingerprint(record)
    return bool(fingerprint) and fingerprint == content_fingerprint(text)


def ledger_record(
    *,
    source: str,
    item_id: str,
    title: str,
    url: str,
    text: str,
    path: str,
    query: str,
    status: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = now_iso()
    digest = content_hash(text)
    url_key = global_url_key(url)
    fingerprint = content_fingerprint(text)
    return {
        "key": make_key(source, item_id),
        "source": source,
        "item_id": item_id,
        "title": title,
        "url": url,
        "canonical_url": normalize_url(url),
        "url_key": url_key,
        "content_hash": digest,
        "content_fingerprint": fingerprint,
        "content_key": "global_content:" + fingerprint,
        "path": path,
        "query": query,
        "status": status,
        "first_seen_at": previous.get("first_seen_at") if previous else timestamp,
        "last_seen_at": timestamp,
        "previous_hash": previous.get("content_hash") if previous else "",
    }
