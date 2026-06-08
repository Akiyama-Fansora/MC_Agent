from __future__ import annotations

import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcagent.agent_memory as agent_memory  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_memory_reader_hides_damaged_events_by_default() -> None:
    original_path = agent_memory.MEMORY_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "agent_memory.jsonl"
            agent_memory.MEMORY_PATH = memory_path
            agent_memory.append_memory_event("crawler_gap_delegated", {"question": "采集乌托邦资料"})
            agent_memory.append_memory_event("crawler_gap_delegated", {"question": "é®ä¸MCAgentä¹æé¦ç¼ºåªäº"})
            clean = agent_memory.read_memory_events(limit=10)
            raw = agent_memory.read_memory_events(limit=10, include_damaged=True)
            summary = agent_memory.memory_summary(limit=10)
    finally:
        agent_memory.MEMORY_PATH = original_path

    assert_equal("raw_count", len(raw), 2)
    assert_equal("clean_count", len(clean), 1)
    assert_equal("clean_question", clean[0].get("question"), "采集乌托邦资料")
    assert_equal("hidden_count", summary.get("encoding_damaged_events_hidden"), 1)
    assert_true("summary_keeps_clean_memory", summary.get("events") == 1, str(summary))


if __name__ == "__main__":
    test_memory_reader_hides_damaged_events_by_default()
    print("agent_memory_encoding_scenarios passed")
