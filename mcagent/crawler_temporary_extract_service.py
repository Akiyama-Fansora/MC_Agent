from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .cleaners import _HTMLTextExtractor, normalize_text
from .provider_registry import request_text


FetchTextFn = Callable[[str], tuple[str, str, int]]
SummarizeFn = Callable[[str, str, str], str]


URL_RE = re.compile(r"https?://[^\s<>'\"，。；、)）\]]+", flags=re.I)
ASCII_URI_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%")
DEFAULT_USER_AGENT = "MC_Agent/0.1 (temporary crawler extraction; no local persistence)"


@dataclass(frozen=True, slots=True)
class TemporaryExtractResult:
    answer: str
    url: str
    title: str
    status_code: int
    content_type: str
    text_chars: int

    def to_response(self, *, agent: str) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "sources": [
                {
                    "rank": 1,
                    "score": 1.0,
                    "title": self.title or self.url,
                    "url": self.url,
                    "text": "",
                    "metadata": {
                        "temporary_extract": True,
                        "status_code": self.status_code,
                        "content_type": self.content_type,
                        "text_chars": self.text_chars,
                        "saved_to_local": False,
                    },
                }
            ],
            "context": "",
            "agent": agent,
            "temporary_extract": {
                "url": self.url,
                "title": self.title,
                "status_code": self.status_code,
                "content_type": self.content_type,
                "text_chars": self.text_chars,
                "saved_to_local": False,
            },
        }


class CrawlerTemporaryExtractService:
    """Fetch public URL text for CrawlerAgent one-shot answers without saving files."""

    def extract_url(self, text: str) -> str:
        match = URL_RE.search(str(text or ""))
        if not match:
            return ""
        candidate = match.group(0).rstrip(".,;:")
        end = 0
        for index, char in enumerate(candidate):
            if char not in ASCII_URI_CHARS:
                break
            end = index + 1
        return self.normalize_url(candidate[:end])

    def normalize_url(self, url: str) -> str:
        value = str(url or "").strip().rstrip(".,;:")
        if not value:
            return ""
        try:
            parts = urlsplit(value)
        except ValueError:
            return value.rstrip("?")
        if not parts.scheme or not parts.netloc:
            return value.rstrip("?")
        path = parts.path.rstrip("?")
        query = parts.query
        fragment = parts.fragment
        if query and set(query) == {"?"}:
            query = ""
        return urlunsplit((parts.scheme, parts.netloc, path, query, fragment)).rstrip(".,;:")

    def html_to_text(self, raw: str, content_type: str) -> tuple[str, str]:
        if "html" not in str(content_type or "").lower() and "<html" not in raw[:1000].lower():
            text = normalize_text(raw)
            return "", text
        parser = _HTMLTextExtractor()
        parser.feed(raw)
        title = normalize_text(parser.title)
        text = normalize_text(parser.text)
        return title, text

    def default_fetch(self, url: str) -> tuple[str, str, int]:
        return request_text(url, DEFAULT_USER_AGENT, timeout=30, retries=1)

    def fetch_text(self, url: str, *, fetch: FetchTextFn | None = None) -> tuple[str, str, str, int]:
        fetcher = fetch or self.default_fetch
        url = self.normalize_url(url)
        errors: list[str] = []
        try:
            raw, content_type, status = fetcher(url)
            title, text = self.html_to_text(raw, content_type)
            if len(text) >= 120:
                return title, text, content_type, status
            errors.append(f"{url}: extracted text too short")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
        raise RuntimeError("; ".join(errors) or "temporary extraction failed")

    def run(
        self,
        *,
        question: str,
        collection_target: str,
        summarize: SummarizeFn,
        fetch: FetchTextFn | None = None,
        max_chars: int = 12000,
    ) -> TemporaryExtractResult:
        url = self.extract_url(collection_target) or self.extract_url(question)
        if not url:
            raise ValueError("No URL found for temporary extraction.")
        title, text, content_type, status = self.fetch_text(url, fetch=fetch)
        clipped = text[:max_chars]
        answer = summarize(question, url, clipped)
        return TemporaryExtractResult(
            answer=answer,
            url=url,
            title=title,
            status_code=status,
            content_type=content_type,
            text_chars=len(text),
        )
