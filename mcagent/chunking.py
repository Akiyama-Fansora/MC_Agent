from __future__ import annotations

import re

from .schema import RawDocument, TextChunk


SENTENCE_BOUNDARIES = ("\n", "\u3002", ".", "\uff1b", ";", "\uff01", "!", "\uff1f", "?")


def estimate_tokens(text: str) -> int:
    ascii_words = re.findall(r"[A-Za-z0-9_]+", text)
    non_ascii = sum(1 for char in text if ord(char) > 127)
    return max(1, len(ascii_words) + non_ascii // 2)


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    step = max(1, max_chars - overlap_chars)
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = max(text.rfind(marker, start, end) for marker in SENTENCE_BOUNDARIES)
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(0, end - overlap_chars)
        if start >= end:
            start = end
        if step <= 0:
            start = end
    return [chunk for chunk in chunks if chunk]


def chunk_document(document: RawDocument, max_chars: int, overlap_chars: int) -> list[TextChunk]:
    text = document.text.strip()
    if not text:
        return []

    max_chars = max(200, max_chars)
    overlap_chars = max(0, min(overlap_chars, max_chars // 2))
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    raw_chunks: list[tuple[str, int]] = []
    current: list[str] = []
    current_len = 0
    current_overlap = 0

    def flush() -> None:
        nonlocal current, current_len, current_overlap
        if current:
            raw_chunks.append(("\n\n".join(current).strip(), current_overlap))
            current = []
            current_len = 0
            current_overlap = 0

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            flush()
            split_chunks = _split_long_text(paragraph, max_chars, overlap_chars)
            raw_chunks.extend(
                (chunk, overlap_chars if index else 0)
                for index, chunk in enumerate(split_chunks)
            )
            continue
        extra = len(paragraph) + (2 if current else 0)
        if current and current_len + extra > max_chars:
            previous = "\n\n".join(current)
            flush()
            if overlap_chars:
                tail = previous[-overlap_chars:].strip()
                if tail:
                    current = [tail]
                    current_len = len(tail)
                    current_overlap = len(tail)
        current.append(paragraph)
        current_len += extra
    flush()

    chunks: list[TextChunk] = []
    cursor = 0
    for idx, (chunk_text, expected_overlap) in enumerate(raw_chunks):
        search_start = max(0, cursor - expected_overlap)
        start = text.find(chunk_text[:80], search_start)
        if start < 0:
            start = search_start
        end = min(len(text), start + len(chunk_text))
        cursor = end
        chunks.append(
            TextChunk(
                document_source_ref=document.source_ref,
                chunk_index=idx,
                text=chunk_text,
                start_char=start,
                end_char=end,
                token_estimate=estimate_tokens(chunk_text),
                metadata={},
            )
        )
    return chunks
