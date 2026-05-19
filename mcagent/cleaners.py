from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .schema import RawDocument


SUPPORTED_EXTENSIONS = {".md", ".markdown", ".json", ".jsonl", ".html", ".htm", ".txt"}

TEXT_KEYS = {
    "text",
    "content",
    "body",
    "markdown",
    "md",
    "description",
    "summary",
    "article",
    "page_content",
}
HTML_KEYS = {"html", "raw_html", "body_html", "content_html"}
TITLE_KEYS = {"title", "name", "heading", "display_name"}
URL_KEYS = {"url", "link", "source_url", "page_url", "href"}
BLOCK_PAGE_MARKERS = (
    "waf active",
    "503 forbidden",
    "触发防火墙自动拦截",
    "访问被拒绝",
    "web应用防火墙",
    "web application firewall",
)
DIAGNOSTIC_FILE_PREFIXES = (
    "browser_eval_",
    "browser_render_",
    "eval_",
    "eval_analysis_",
    "eval_result_analyzer_",
    "html_extract_",
)


class _HTMLTextExtractor(HTMLParser):
    block_tags = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    skip_tags = {"script", "style", "noscript", "svg", "canvas"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._table_depth = 0
        self._current_row: list[str] = []
        self._current_cell: list[str] | None = None
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        if tag in self.skip_tags:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = []
        if tag == "tr" and self._table_depth:
            self._current_row = []
        if tag in {"td", "th"} and self._table_depth:
            self._current_cell = []
        if tag == "img" and not self._skip_depth:
            src = attrs_map.get("src") or attrs_map.get("data-src") or attrs_map.get("data-original") or attrs_map.get("data-url")
            alt = attrs_map.get("alt") or attrs_map.get("title") or attrs_map.get("data-title")
            if src:
                alt_text = normalize_text(alt or "")
                self.images.append({"src": src, "alt": alt_text})
                label = alt_text or "image"
                self.parts.append(f"\n[Image: {label}] {src}\n")
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._table_depth and self._current_cell is not None:
            self._current_row.append(normalize_text(" ".join(self._current_cell)))
            self._current_cell = None
        if tag == "tr" and self._table_depth and self._current_table is not None and self._current_row:
            self._current_table.append(self._current_row)
            self._current_row = []
        if tag == "table" and self._table_depth:
            if self._table_depth == 1 and self._current_table:
                self.tables.append(self._current_table)
            self._current_table = None
            self._table_depth -= 1
        if tag in self.skip_tags and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)
        self.parts.append(data)

    @property
    def text(self) -> str:
        return normalize_text(" ".join(self.parts))

    @property
    def title(self) -> str:
        return normalize_text(" ".join(self.title_parts)).split("\n", 1)[0].strip()


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_block_page(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in BLOCK_PAGE_MARKERS)


def markdown_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    for line in text.splitlines():
        stripped = stripped.strip()
        if stripped:
            return stripped[:80]
    return fallback


def html_to_text(html: str) -> tuple[str, str]:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text, parser.title


def read_text_file(path: Path) -> str:
    encodings = ("utf-8-sig", "utf-8", "gb18030")
    last_error: UnicodeDecodeError | None = None
    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def iter_source_files(source_dir: Path) -> Iterable[Path]:
    if not source_dir.exists():
        return []
    return (
        path
        for path in sorted(source_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def _first_string(record: dict[str, Any], keys: set[str]) -> str | None:
    for key, value in record.items():
        if key.lower() in keys and isinstance(value, str) and value.strip():
            return normalize_text(value).split("\n", 1)[0].strip()
    return None


def _looks_like_page_record(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    lowered = {str(key).lower() for key in value}
    return bool((TEXT_KEYS | HTML_KEYS | TITLE_KEYS | URL_KEYS) & lowered)


def _false_like(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "no", "0", "none"}
    return False


def _skip_json_record(record: Any, text: str) -> bool:
    if looks_like_block_page(text):
        return True
    if not isinstance(record, dict):
        return False
    lowered_keys = {str(key).lower() for key in record}
    if "manifest_type" in lowered_keys or "failure_type" in lowered_keys:
        return True
    if {"expression", "html_path", "results_path"} & lowered_keys:
        return True
    if {"input_path", "base_url", "expression"} <= lowered_keys:
        return True
    if _false_like(record.get("qa_usable")):
        return True
    qa_pages = record.get("qa_usable_pages")
    blocked_pages = record.get("blocked_pages")
    if str(qa_pages).strip() == "0" and str(blocked_pages).strip() not in {"", "0", "None"}:
        return True
    recommendation = str(record.get("import_recommendation", "")).lower()
    if "do not import" in recommendation or "不要导入" in recommendation:
        return True
    status = str(record.get("status", "")).lower()
    if "blocked" in status:
        return True
    page_role = str(record.get("page_role", "")).lower()
    if page_role.endswith("_attempt") or "attempt" in page_role:
        return True
    if status in {"blocked", "failed", "error"} and looks_like_block_page(_json_value_to_text(record)):
        return True
    return False


def _iter_json_records(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, list):
        if all(_looks_like_page_record(item) for item in value):
            return [(f"item-{idx}", item) for idx, item in enumerate(value)]
        return [("root", value)]
    if isinstance(value, dict):
        for key in ("items", "pages", "records", "results", "data"):
            child = value.get(key)
            if isinstance(child, list) and child and all(_looks_like_page_record(item) for item in child):
                return [(f"{key}-{idx}", item) for idx, item in enumerate(child)]
        return [("root", value)]
    return [("root", value)]


def _json_value_to_text(value: Any, parent_key: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if parent_key.lower() in HTML_KEYS:
            text, _ = html_to_text(value)
            return text
        return normalize_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_json_value_to_text(item, parent_key=parent_key) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, child in value.items():
            key_text = str(key)
            child_text = _json_value_to_text(child, parent_key=key_text)
            if not child_text:
                continue
            if key_text.lower() in TEXT_KEYS | HTML_KEYS:
                parts.append(child_text)
            elif isinstance(child, (dict, list)):
                parts.append(child_text)
            else:
                parts.append(f"{key_text}: {child_text}")
        return "\n".join(parts)
    return normalize_text(str(value))


def _document_from_json_record(
    path: Path,
    root: Path,
    record_key: str,
    record: Any,
) -> RawDocument | None:
    title = path.stem
    url: str | None = None
    metadata: dict[str, Any] = {"format": "json", "record": record_key}

    if isinstance(record, dict):
        title = _first_string(record, TITLE_KEYS) or title
        url = _first_string(record, URL_KEYS)
        for key in ("mod", "category", "source", "site", "id"):
            value = record.get(key)
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value

    text = normalize_text(_json_value_to_text(record))
    if not text:
        return None
    if _skip_json_record(record, text):
        return None

    relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
    source_ref = f"{relative}#{record_key}" if record_key != "root" else relative
    return RawDocument(
        source_ref=source_ref,
        source_path=path,
        title=title,
        text=text,
        url=url,
        metadata=metadata,
    )


def load_documents_from_path(path: Path, root: Path) -> list[RawDocument]:
    suffix = path.suffix.lower()
    raw = read_text_file(path)
    relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
    relative_lower = relative.lower()

    if "/reports/" in f"/{relative_lower}" or relative_lower.endswith("/manifest.json"):
        return []
    if path.name.lower() in {"manifest.json", "framework_failure_lesson.json"}:
        return []
    if "/raw_html/" in f"/{relative_lower}":
        return []
    if path.name.lower().startswith(DIAGNOSTIC_FILE_PREFIXES):
        return []

    if suffix in {".html", ".htm"}:
        text, title = html_to_text(raw)
        if looks_like_block_page(text):
            return []
        return [
            RawDocument(
                source_ref=relative,
                source_path=path,
                title=title or path.stem,
                text=text,
                metadata={"format": "html"},
            )
        ] if text else []

    if suffix in {".md", ".markdown"}:
        text = normalize_text(raw)
        if looks_like_block_page(text):
            return []
        return [
            RawDocument(
                source_ref=relative,
                source_path=path,
                title=markdown_title(text, path.stem),
                text=text,
                metadata={"format": "markdown"},
            )
        ] if text else []

    if suffix == ".txt":
        text = normalize_text(raw)
        if looks_like_block_page(text):
            return []
        return [
            RawDocument(
                source_ref=relative,
                source_path=path,
                title=markdown_title(text, path.stem),
                text=text,
                metadata={"format": "text"},
            )
        ] if text else []

    if suffix == ".jsonl":
        docs: list[RawDocument] = []
        for idx, line in enumerate(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            doc = _document_from_json_record(path, root, f"line-{idx}", record)
            if doc:
                docs.append(doc)
        return docs

    if suffix == ".json":
        parsed = json.loads(raw)
        docs = []
        for record_key, record in _iter_json_records(parsed):
            doc = _document_from_json_record(path, root, record_key, record)
            if doc:
                docs.append(doc)
        return docs

    return []
