from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.chunking import SENTENCE_BOUNDARIES, chunk_document  # noqa: E402
from mcagent.schema import RawDocument  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_long_chinese_text_splits_on_real_sentence_boundaries() -> None:
    full_stop = "\u3002"
    semicolon = "\uff1b"
    text = ("\u7532" * 120) + full_stop + ("\u4e59" * 160) + semicolon + ("\u4e19" * 80)
    document = RawDocument(source_ref="scenario", source_path=Path("scenario.md"), title="Scenario", text=text)

    chunks = chunk_document(document, max_chars=200, overlap_chars=0)

    assert_equal("chunk_count", len(chunks), 3)
    assert_true("first_chunk_boundary", chunks[0].text.endswith(full_stop), chunks[0].text[-10:])
    assert_true("second_chunk_boundary", chunks[1].text.endswith(semicolon), chunks[1].text[-10:])
    assert_equal("chunk_indices", [chunk.chunk_index for chunk in chunks], [0, 1, 2])
    assert_equal("first_start", chunks[0].start_char, 0)
    assert_equal("last_end", chunks[-1].end_char, len(text))


def test_sentence_boundaries_do_not_contain_mojibake_markers() -> None:
    markers = {ord(char) for boundary in SENTENCE_BOUNDARIES for char in boundary}

    assert_true("has_chinese_full_stop", 0x3002 in markers)
    assert_true("has_chinese_semicolon", 0xFF1B in markers)
    assert_true("no_mojibake_full_stop_marker", 0x9286 not in markers)
    assert_true("no_mojibake_semicolon_marker", 0x951B not in markers)


def test_long_text_overlap_offsets_match_source_text() -> None:
    text = ("A" * 200) + ("B" * 150) + ("C" * 150)
    document = RawDocument(source_ref="overlap", source_path=Path("overlap.md"), title="Overlap", text=text)

    chunks = chunk_document(document, max_chars=200, overlap_chars=50)

    assert_equal("overlap_starts", [chunk.start_char for chunk in chunks], [0, 150, 300])
    assert_equal("overlap_ends", [chunk.end_char for chunk in chunks], [200, 350, 500])
    for index, chunk in enumerate(chunks):
        assert_equal(f"overlap_source_slice_{index}", text[chunk.start_char : chunk.end_char], chunk.text)


def test_paragraph_tail_overlap_offsets_match_source_text() -> None:
    text = ("A" * 180) + "\n\n" + ("B" * 100)
    document = RawDocument(source_ref="paragraphs", source_path=Path("paragraphs.md"), title="Paragraphs", text=text)

    chunks = chunk_document(document, max_chars=200, overlap_chars=50)

    assert_equal("paragraph_overlap_starts", [chunk.start_char for chunk in chunks], [0, 130])
    assert_equal("paragraph_overlap_ends", [chunk.end_char for chunk in chunks], [180, len(text)])
    for index, chunk in enumerate(chunks):
        assert_equal(f"paragraph_source_slice_{index}", text[chunk.start_char : chunk.end_char], chunk.text)


if __name__ == "__main__":
    test_long_chinese_text_splits_on_real_sentence_boundaries()
    test_sentence_boundaries_do_not_contain_mojibake_markers()
    test_long_text_overlap_offsets_match_source_text()
    test_paragraph_tail_overlap_offsets_match_source_text()
    print("chunking_scenarios: ok")
