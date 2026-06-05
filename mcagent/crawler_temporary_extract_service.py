from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .cleaners import _HTMLTextExtractor, normalize_text
from .provider_registry import request_text


FetchTextFn = Callable[[str], tuple[str, str, int]]
SummarizeFn = Callable[[str, str, str], str]
ReviewSummarizeFn = Callable[[str, str, str, str, list[str], str], str]


URL_RE = re.compile(r"https?://[^\s<>'\"，。；、)）\]]+", flags=re.I)
ASCII_URI_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%")
DEFAULT_USER_AGENT = "MC_Agent/0.1 (temporary crawler extraction; no local persistence)"
TECHNICAL_TERM_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
REQUEST_TERM_STOPWORDS = {
    "about",
    "and",
    "anything",
    "background",
    "com",
    "crawler",
    "docs",
    "documentation",
    "extract",
    "from",
    "html",
    "http",
    "https",
    "library",
    "locally",
    "open",
    "org",
    "python",
    "read",
    "save",
    "start",
    "summarize",
    "summary",
    "task",
    "temporarily",
    "temporary",
    "the",
    "then",
    "topic",
    "url",
    "use",
    "www",
}


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

    def requested_terms(self, question: str) -> list[str]:
        terms: list[str] = []
        for match in TECHNICAL_TERM_RE.finditer(str(question or "")):
            term = match.group(0)
            lowered = term.lower()
            if lowered in REQUEST_TERM_STOPWORDS:
                continue
            looks_technical = "_" in term or any(char.isdigit() for char in term) or (any(char.islower() for char in term) and any(char.isupper() for char in term))
            if not looks_technical and len(term) < 10:
                continue
            if term not in terms:
                terms.append(term)
        return terms[:12]

    def missing_requested_terms(self, question: str, answer: str) -> list[str]:
        answer_lower = str(answer or "").lower()
        return [term for term in self.requested_terms(question) if term.lower() not in answer_lower]

    def answer_looks_incomplete(self, answer: str) -> bool:
        text = str(answer or "").strip()
        if len(text) < 80:
            return True
        if text.count("```") % 2 == 1 or text.count("`") % 2 == 1:
            return True
        return bool(re.search(r"([：:，,、（(【\[]|about\s+`?)\s*$", text, flags=re.I))

    def relevant_excerpt(self, text: str, terms: list[str], *, max_chars: int = 5000) -> str:
        normalized = normalize_text(text)
        if not terms:
            return normalized[:max_chars]
        chunks: list[str] = []
        lowered = normalized.lower()
        for term in terms:
            index = lowered.find(term.lower())
            if index < 0:
                continue
            start = max(0, index - 700)
            end = min(len(normalized), index + 1200)
            chunk = normalized[start:end].strip()
            if chunk and chunk not in chunks:
                chunks.append(chunk)
        excerpt = "\n\n---\n\n".join(chunks)
        return (excerpt or normalized)[:max_chars]

    def run(
        self,
        *,
        question: str,
        collection_target: str,
        summarize: SummarizeFn,
        review_summarize: ReviewSummarizeFn | None = None,
        fetch: FetchTextFn | None = None,
        max_chars: int = 12000,
    ) -> TemporaryExtractResult:
        url = self.extract_url(collection_target) or self.extract_url(question)
        if not url:
            raise ValueError("No URL found for temporary extraction.")
        title, text, content_type, status = self.fetch_text(url, fetch=fetch)
        clipped = text[:max_chars]
        answer = summarize(question, url, clipped)
        missing_terms = self.missing_requested_terms(question, answer)
        if review_summarize and (missing_terms or self.answer_looks_incomplete(answer)):
            excerpt = self.relevant_excerpt(text, missing_terms or self.requested_terms(question))
            answer = review_summarize(question, url, clipped, answer, missing_terms, excerpt)
        return TemporaryExtractResult(
            answer=answer,
            url=url,
            title=title,
            status_code=status,
            content_type=content_type,
            text_chars=len(text),
        )
