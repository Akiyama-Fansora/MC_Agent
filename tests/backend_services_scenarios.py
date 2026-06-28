from __future__ import annotations

from pathlib import Path
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.event_stream import ThreadedEventStream  # noqa: E402
from mcagent.session_state import InMemorySessionStore, merge_limited, payload_history  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def sse_events(text: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        events.append((event, payload))
    return events


def test_session_store_context_roundtrip() -> None:
    store = InMemorySessionStore()
    store.append_turn("ctx-test", {"question": "介绍乌托邦", "answer": "玩法包括探索", "sources": []})
    store.append_turn("ctx-test", {"question": "缺什么", "answer": "缺模组列表", "sources": []})
    store.update_summary("ctx-test", lambda _current: {"topics": ["乌托邦"], "gaps": ["模组列表"]})

    context = store.context("ctx-test", agent="mcagent_rag").to_dict()
    assert_equal("context_session", context["session_id"], "ctx-test")
    assert_equal("context_agent", context["agent"], "mcagent_rag")
    assert_equal("context_turns", context["turn_count"], 2)
    assert_equal("context_last_question", context["last_turn"]["question"], "缺什么")
    assert_equal("context_summary_topic", context["summary"]["topics"], ["乌托邦"])

    deleted = store.delete("ctx-test")
    assert_true("deleted", deleted["deleted"])
    assert_equal("history_deleted", store.history("ctx-test"), [])


def test_payload_history_and_merge_helpers() -> None:
    payload = {
        "history": [
            {"role": "user", "text": "问题一", "time": 1000},
            {"role": "assistant", "text": "回答一"},
            {"role": "user", "text": "问题二", "time": 2000},
            {"role": "assistant", "text": "处理中..."},
            {"role": "assistant", "text": "回答二"},
        ]
    }
    turns = payload_history(payload, limit=4)
    assert_equal("payload_turn_count", len(turns), 2)
    assert_equal("payload_question", turns[0]["question"], "问题一")
    assert_equal("payload_second_question", turns[1]["question"], "问题二")

    merged = merge_limited(["A", "B"], ["b", "C", "D"], limit=3)
    assert_equal("merge_dedupe", merged, ["A", "B", "C"])


def test_threaded_event_stream_sse_shape() -> None:
    def target(emit):
        emit("trace", {"stage": "observe"})
        emit("response", {"answer": "ok"})
        emit("done", {"ok": True})

    text = "".join(ThreadedEventStream(target).sse())
    assert_true("trace_event", "event: trace" in text)
    assert_true("response_event", "event: response" in text)
    assert_true("done_event", "event: done" in text)
    assert_true("json_payload", '"answer": "ok"' in text)


def test_threaded_event_stream_emits_runtime_response_before_error() -> None:
    def target(_emit):
        raise RuntimeError("stream failure")

    events = sse_events("".join(ThreadedEventStream(target).sse()))
    names = [name for name, _payload in events]
    assert_equal("event_order", names, ["response", "error"])
    response = events[0][1]
    assert_true("response_payload", isinstance(response, dict), str(response))
    assert_true("runtime_error", bool(response.get("runtime_error")), str(response))
    assert_true("answer_mentions_error", "RuntimeError: stream failure" in str(response.get("answer") or ""), str(response))


def main() -> int:
    test_session_store_context_roundtrip()
    test_payload_history_and_merge_helpers()
    test_threaded_event_stream_sse_shape()
    test_threaded_event_stream_emits_runtime_response_before_error()
    print("BACKEND SERVICES SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
