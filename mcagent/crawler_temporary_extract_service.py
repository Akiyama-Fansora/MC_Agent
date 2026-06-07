from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
import urllib.request
import xml.etree.ElementTree as ET

from .cleaners import _HTMLTextExtractor, normalize_text
from .provider_registry import request_text


FetchTextFn = Callable[[str], tuple[str, str, int]]
SummarizeFn = Callable[[str, str, str], str]
ReviewSummarizeFn = Callable[[str, str, str, str, list[str], str], str]
ChooseUrlFn = Callable[[str, str, list[dict[str, Any]]], str]


URL_RE = re.compile(r"https?://[^\s<>'\"，。；、)）\]]+", flags=re.I)
ASCII_URI_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%")
DEFAULT_USER_AGENT = "MC_Agent/0.1 (temporary crawler extraction; no local persistence)"
TECHNICAL_TERM_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_-]{2,}\b")
REQUEST_TERM_STOPWORDS = {
    "about",
    "and",
    "anything",
    "answer",
    "background",
    "com",
    "crawler",
    "collection",
    "docs",
    "documentation",
    "extract",
    "from",
    "html",
    "http",
    "https",
    "information",
    "library",
    "locally",
    "long-running",
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
    "viewer",
    "www",
}
SEARCH_SKIP_HOSTS = (
    "bing.com",
    "google.",
    "baidu.",
    "duckduckgo.",
    "youtube.",
    "youtu.be",
    "bilibili.com",
)


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

    def search_candidates(self, query: str, *, limit: int = 8, timeout: int = 12) -> list[dict[str, Any]]:
        search_url = "https://www.bing.com/search?format=rss&mkt=en-US&setlang=en-US&q=" + quote(str(query or ""), safe="")
        request = urllib.request.Request(
            search_url,
            headers={
                "Accept": "application/rss+xml,application/xml,text/xml,*/*",
                "User-Agent": DEFAULT_USER_AGENT,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            content = raw.decode(charset, errors="replace")
        root = ET.fromstring(content)
        results: list[dict[str, Any]] = []
        for rank, item in enumerate(root.findall("./channel/item"), start=1):
            title = normalize_text(item.findtext("title") or "")
            url = self.normalize_url(item.findtext("link") or "")
            snippet = normalize_text(item.findtext("description") or "")
            host = urlparse(url).netloc.lower()
            if not url or any(host == skip or host.endswith("." + skip) or skip in host for skip in SEARCH_SKIP_HOSTS):
                continue
            results.append({"rank": rank, "title": title, "url": url, "snippet": snippet})
            if len(results) >= limit:
                break
        return results

    def discovery_query(self, *, question: str, collection_target: str) -> str:
        text = normalize_text(collection_target or question)
        terms = self.requested_terms(text)
        domains: list[str] = []
        lowered = text.lower()
        if "playwright" in lowered:
            domains.extend(["Playwright", "Trace Viewer"])
        if "python packaging" in lowered:
            domains.extend(["Python Packaging User Guide"])
        technical = [term for term in terms if term not in domains]
        query = " ".join(dict.fromkeys([*domains, *technical]))
        return query or text

    def discover_url(
        self,
        *,
        question: str,
        collection_target: str,
        choose_url: ChooseUrlFn,
        fetch: FetchTextFn | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        query = self.discovery_query(question=question, collection_target=collection_target)
        candidates = self.search_candidates(query)
        candidates = self.expand_verified_official_doc_candidates(
            question=question,
            collection_target=collection_target,
            candidates=candidates,
            fetch=fetch,
        )
        if not candidates:
            raise ValueError("No URL found for temporary extraction and discovery returned no candidates.")
        selected = self.normalize_url(choose_url(question, collection_target, candidates))
        if not selected:
            raise ValueError("CrawlerAgent did not select a candidate URL for temporary extraction.")
        candidate_urls = {self.normalize_url(str(item.get("url") or "")) for item in candidates}
        if selected not in candidate_urls:
            raise ValueError("CrawlerAgent selected a URL that was not present in objective discovery candidates.")
        return selected, candidates

    def expand_verified_official_doc_candidates(
        self,
        *,
        question: str,
        collection_target: str,
        candidates: list[dict[str, Any]],
        fetch: FetchTextFn | None = None,
    ) -> list[dict[str, Any]]:
        """Add objectively reachable docs-page candidates derived from official hosts.

        The service only verifies that a candidate URL is reachable and readable.
        CrawlerAgent still chooses and judges the page relevance.
        """

        text = normalize_text(f"{question}\n{collection_target}")
        lowered = text.lower()
        if "official" not in lowered or not any(term in lowered for term in ("docs", "documentation", "guide")):
            return candidates
        slugs = self.official_doc_slugs(text)
        if not slugs:
            return candidates
        hosts = self.official_candidate_hosts(text, candidates)
        if not hosts:
            return candidates
        existing_urls = {self.normalize_url(str(item.get("url") or "")) for item in candidates}
        verified_existing: list[dict[str, Any]] = []
        for item in candidates:
            url = self.normalize_url(str(item.get("url") or ""))
            if not url:
                continue
            if not self.official_doc_url_matches_request(url, text):
                continue
            try:
                title, body, _content_type, status = self.fetch_text(url, fetch=fetch)
            except Exception:
                continue
            cloned = dict(item)
            cloned["title"] = str(cloned.get("title") or title or url)
            cloned["snippet"] = str(cloned.get("snippet") or body[:260])
            cloned["verified"] = True
            cloned["verified_status"] = status
            verified_existing.append(cloned)
        additions: list[dict[str, Any]] = []
        for host in hosts[:3]:
            for slug in slugs[:4]:
                for path_template in ("/docs/{slug}", "/python/docs/{slug}", "/en/latest/{slug}/"):
                    url = self.normalize_url(f"https://{host}{path_template.format(slug=slug)}")
                    if url in existing_urls:
                        continue
                    try:
                        title, body, _content_type, status = self.fetch_text(url, fetch=fetch)
                    except Exception:
                        continue
                    additions.append(
                        {
                            "rank": 0,
                            "title": title or f"Verified official docs candidate: {slug}",
                            "url": url,
                            "snippet": body[:260],
                            "verified": True,
                            "verified_status": status,
                        }
                    )
                    existing_urls.add(url)
                    break
        ordered: list[dict[str, Any]] = []
        seen_ordered: set[str] = set()
        for item in verified_existing + additions + candidates:
            url = self.normalize_url(str(item.get("url") or ""))
            if not url or url in seen_ordered:
                continue
            ordered.append(item)
            seen_ordered.add(url)
        if not ordered:
            return candidates
        for index, item in enumerate(ordered, start=1):
            item["rank"] = index
        return ordered

    def official_doc_url_matches_request(self, url: str, text: str) -> bool:
        parsed = urlparse(str(url or ""))
        host = parsed.netloc.lower().lstrip("www.")
        path = parsed.path.lower()
        lowered = str(text or "").lower()
        if not parsed.scheme.startswith("http") or not host:
            return False
        if "playwright" in lowered and host == "playwright.dev" and "/docs/" in path:
            return True
        if ("python packaging" in lowered or "pypa" in lowered) and host == "packaging.python.org":
            return True
        return any(token in host for token in ("docs.", "documentation."))

    def official_candidate_hosts(self, text: str, candidates: list[dict[str, Any]]) -> list[str]:
        lowered = str(text or "").lower()
        hosts: list[str] = []
        if "playwright" in lowered:
            hosts.append("playwright.dev")
        if "python packaging" in lowered or "pypa" in lowered:
            hosts.append("packaging.python.org")
        for item in candidates:
            host = urlparse(str(item.get("url") or "")).netloc.lower().lstrip("www.")
            if not host:
                continue
            if host in hosts:
                continue
            if any(token in host for token in ("docs.", "documentation.", "playwright.dev", "packaging.python.org")):
                hosts.append(host)
        return hosts[:5]

    def official_doc_slugs(self, text: str) -> list[str]:
        slugs: list[str] = []
        lowered = str(text or "").lower()
        phrase_patterns = [
            r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,4})\s+(?:docs|documentation|guide)\b",
            r"\b(?:docs|documentation|guide)\s+(?:about|for|on)?\s*([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,4})\b",
        ]
        for pattern in phrase_patterns:
            for match in re.finditer(pattern, str(text or "")):
                phrase = normalize_text(match.group(1))
                slug = self.slugify_doc_phrase(phrase)
                if slug and slug not in slugs:
                    slugs.append(slug)
        for term in self.requested_terms(text):
            slug = self.slugify_doc_phrase(term)
            if slug and slug not in slugs:
                slugs.append(slug)
        if "trace viewer" in lowered and "trace-viewer" not in slugs:
            slugs.insert(0, "trace-viewer")
        return slugs[:8]

    def slugify_doc_phrase(self, value: str) -> str:
        words = re.findall(r"[A-Za-z0-9]+", str(value or ""))
        words = [word.lower() for word in words if word.lower() not in REQUEST_TERM_STOPWORDS]
        if not words:
            return ""
        return "-".join(words[:5])

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
        cleaned_question = re.sub(r"https?://\S+", " ", str(question or ""))
        for match in TECHNICAL_TERM_RE.finditer(cleaned_question):
            term = match.group(0)
            lowered = term.lower()
            if lowered in REQUEST_TERM_STOPWORDS:
                continue
            looks_technical = (
                "_" in term
                or "-" in term
                or any(char.isdigit() for char in term)
                or (any(char.islower() for char in term) and any(char.isupper() for char in term))
            )
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
        choose_url: ChooseUrlFn | None = None,
        fetch: FetchTextFn | None = None,
        max_chars: int = 12000,
    ) -> TemporaryExtractResult:
        url = self.extract_url(collection_target) or self.extract_url(question)
        if not url:
            if choose_url is None:
                raise ValueError("No URL found for temporary extraction.")
            url, _candidates = self.discover_url(question=question, collection_target=collection_target, choose_url=choose_url, fetch=fetch)
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
