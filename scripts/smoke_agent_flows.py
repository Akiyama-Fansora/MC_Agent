from __future__ import annotations

import json
import os
import sys
import time
import urllib.request


BASE = "http://127.0.0.1:8765"
TEST_MODEL = os.environ.get("MCAGENT_TEST_MODEL", "").strip()
FULL_SMOKE = os.environ.get("MCAGENT_SMOKE_FULL", "").strip() == "1"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def post_json(path: str, payload: dict, timeout: int = 90) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_stream(payload: dict, timeout: int = 90) -> tuple[list[dict], dict | None]:
    if TEST_MODEL and "model" not in payload:
        payload = {**payload, "model": TEST_MODEL}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    events: list[dict] = []
    final: dict | None = None
    event = "message"
    data_lines: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
                continue
            if line.strip():
                continue
            if not data_lines:
                event = "message"
                continue
            value = json.loads("\n".join(data_lines))
            events.append({"event": event, "data": value})
            if event == "response":
                final = value
                break
            event = "message"
            data_lines = []
    return events, final


def chat(session_id: str, agent: str, question: str, *, timeout: int = 180, **extra: object) -> tuple[list[dict], dict]:
    payload = {
        "session_id": session_id,
        "agent": agent,
        "question": question,
        "max_tokens": extra.pop("max_tokens", 700),
        **extra,
    }
    events, final = read_stream(payload, timeout=timeout)
    if final is None:
        raise AssertionError(f"no final response for {question}")
    return events, final


def stop_job(job_id: str) -> None:
    try:
        post_json("/api/jobs/stop", {"id": job_id}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN stop job {job_id}: {exc}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}", flush=True)
        return
    print(f"FAIL {name}: {detail}", flush=True)
    raise AssertionError(name)


def visible_answer(answer: str) -> str:
    markers = ["\n\n来源：", "\n来源：", "\n\n模型：", "\n模型：", "\n\n补库动作：", "\n补库动作："]
    cut = len(answer)
    for marker in markers:
        index = answer.find(marker)
        if index >= 0:
            cut = min(cut, index)
    return answer[:cut]


def main() -> int:
    session_id = f"smoke-{int(time.time())}"
    q_status = "\u72b6\u6001"
    q_progress = "\u8fdb\u5ea6\u600e\u4e48\u6837"
    q_beginner = "\u843d\u5e55\u66f2\u65b0\u624b\u8be5\u600e\u4e48\u73a9"
    q_boss_collect = "\u5e2eMCAgent\u83b7\u53d6\u843d\u5e55\u66f2\u6709\u54ea\u4e9bBOSS \u8fd9\u4e9bBOSS\u5982\u4f55\u6253\u54ea\u91cc\u6253\u600e\u6837\u6253"
    q_utopia_collect = "\u53ebCrawlerAgent\u6536\u96c6\u4e4c\u6258\u90a6\u5b8c\u6574\u8d44\u6599"
    q_closing_boss_collect = "\u8ba9Crawler\u83b7\u53d6\u843d\u5e55\u66f2Boss\u6e05\u5355"
    cases = [
        {
            "name": "status_routes_to_tool",
            "payload": {"session_id": session_id + "-status", "agent": "mcagent_rag", "question": q_status},
            "check": lambda events, final: (
                any(e["event"] == "trace" and e["data"].get("status") == "tool_selected" and e["data"].get("detail", {}).get("tool") == "status" for e in events),
                "expected tool_selected=status",
            ),
        },
        {
            "name": "progress_routes_to_tool",
            "payload": {"session_id": session_id + "-progress", "agent": "mcagent_rag", "question": q_progress},
            "check": lambda events, final: (
                any(e["event"] == "trace" and e["data"].get("status") == "tool_selected" and e["data"].get("detail", {}).get("tool") == "status" for e in events),
                "expected tool_selected=status for progress query",
            ),
        },
        {
            "name": "mcagent_delegates_utopia_collection",
            "payload": {"session_id": session_id + "-utopia-collect", "agent": "mcagent_rag", "question": q_utopia_collect},
            "check": lambda events, final: (
                bool(final and final.get("delegation", {}).get("requested_by") == "user_via_mcagent" and "\u4e4c\u6258\u90a6" in final.get("delegation", {}).get("task", "")),
                "expected user-via-MCagent delegation for Utopia collection",
            ),
            "stop_job": True,
        },
        {
            "name": "mcagent_delegates_closing_song_boss_collection",
            "payload": {"session_id": session_id + "-boss-collect", "agent": "mcagent_rag", "question": q_closing_boss_collect},
            "check": lambda events, final: (
                bool(final and final.get("delegation", {}).get("requested_by") == "user_via_mcagent" and "\u843d\u5e55\u66f2" in final.get("delegation", {}).get("task", "")),
                "expected user-via-MCagent delegation for Closing Song Boss collection",
            ),
            "stop_job": True,
        },
        {
            "name": "rag_beginner_guide_has_answer_trace",
            "payload": {"session_id": session_id + "-rag", "agent": "mcagent_rag", "question": q_beginner, "max_tokens": 900},
            "check": lambda events, final: (
                bool(final and final.get("answer") and any(e["event"] == "trace" and e["data"].get("stage") == "retrieve" for e in events) and any(e["event"] == "delta" for e in events)),
                "expected answer, retrieve trace, and token delta events",
            ),
        },
        {
            "name": "crawler_direct_user_delegation",
            "payload": {"session_id": session_id + "-crawler", "agent": "crawler_agent", "question": q_boss_collect},
            "check": lambda events, final: (
                bool(final and final.get("delegation", {}).get("requested_by") == "user" and final.get("delegation", {}).get("delivery_target") == "MCagent/RAG"),
                "expected requested_by=user and delivery_target=MCagent/RAG",
            ),
            "stop_job": True,
        },
    ]
    for case in cases:
        print(f"\nCASE {case['name']}", flush=True)
        events, final = read_stream(case["payload"], timeout=180)
        ok, detail = case["check"](events, final)
        if not ok and final:
            print("DEBUG final delegation:", final.get("delegation"))
            print("DEBUG final answer:", (final.get("answer") or "")[:800])
        assert_true(case["name"], ok, detail)
        if final:
            print((final.get("answer") or "")[:500].replace("\n", "\\n"), flush=True)
            job = final.get("job") if isinstance(final.get("job"), dict) else None
            if case.get("stop_job") and job and job.get("id"):
                stop_job(str(job["id"]))

    if not FULL_SMOKE:
        print("\nCASE rag_utopia_mod_list", flush=True)
        events, final = chat(
            session_id + "-rag-utopia",
            "mcagent_rag",
            "\u4e4c\u6258\u90a6\u6709\u54ea\u4e9b\u6a21\u7ec4",
            max_tokens=700,
            show_context=True,
        )
        answer = final.get("answer") or ""
        main_answer = visible_answer(answer)
        context = final.get("context") or ""
        assert_true(
            "rag_utopia_mod_list",
            bool(main_answer)
            and ("423" in main_answer or "\u4e2a\u6a21\u7ec4" in main_answer)
            and "Immersive Aircraft" in context
            and "根据本地资料，可以确定相关候选内容如下" not in main_answer,
            main_answer[:800],
        )
        if final.get("job", {}).get("id"):
            stop_job(str(final["job"]["id"]))
        print(main_answer[:500].replace("\n", "\\n"), flush=True)
        print("\nFAST SMOKE PASSED. Set MCAGENT_SMOKE_FULL=1 for the long context matrix.", flush=True)
        return 0

    print("\nCASE continuous_context_boss_followups", flush=True)
    context_session = session_id + "-context"
    _events, first = chat(context_session, "mcagent_rag", "\u843d\u5e55\u66f2\u65b0\u624b\u8be5\u600e\u4e48\u73a9", max_tokens=900)
    turns = [
        "\u6709\u54ea\u4e9bBOSS",
        "\u8fd9\u4e9bBOSS\u600e\u4e48\u6253",
        "\u6389\u843d\u4ec0\u4e48",
    ]
    bad_fragments = ["\u662f\u6700\u65b9\u4fbf", "\u8bb8\u591a\\n-", "\u8bcd\u6761\\n-", "\u5de6\u53f3\\n-", "\u6750\u6599\\n-", "\u5305\u4f5c"]
    for turn in turns:
        events, final = chat(context_session, "mcagent_rag", turn, max_tokens=900)
        answer = final.get("answer") or ""
        main_answer = visible_answer(answer)
        if turn == "\u6389\u843d\u4ec0\u4e48":
            expected = "\u7a33\u5b9a" in main_answer or "\u6389\u843d" in main_answer or "\u5956\u52b1" in main_answer
        else:
            expected = True
        assert_true(
            f"context_followup_{turn}",
            bool(main_answer)
            and expected
            and not any(fragment in main_answer for fragment in bad_fragments)
            and "\u5c06\u672b\u5f71\u9f99" not in main_answer
            and "\u4e4c\u6258\u90a6" not in main_answer,
            main_answer[:800],
        )
        if final.get("job", {}).get("id"):
            stop_job(str(final["job"]["id"]))
        print(main_answer[:500].replace("\n", "\\n"), flush=True)

    print("\nCASE rag_answer_matrix", flush=True)
    rag_queries = [
        "\u843d\u5e55\u66f2\u91cc\u7684\u5854\u7f57\u724c\u662f\u4ec0\u4e48",
        "\u62d4\u5200\u5251\u600e\u4e48\u73a9",
        "\u68a6\u60f3\u4e00\u5fc3\u600e\u4e48\u505a",
        "\u4e4c\u6258\u90a6\u6709\u54ea\u4e9b\u6a21\u7ec4",
    ]
    for query in rag_queries:
        events, final = chat(session_id + "-rag-matrix", "mcagent_rag", query, max_tokens=900, show_context=True)
        answer = final.get("answer") or ""
        main_answer = visible_answer(answer)
        if query == "\u4e4c\u6258\u90a6\u6709\u54ea\u4e9b\u6a21\u7ec4":
            context = final.get("context") or ""
            expected = ("423" in main_answer or "\u4e2a\u6a21\u7ec4" in main_answer) and "Immersive Aircraft" in context
        else:
            expected = True
        assert_true(
            f"rag_query_{query}",
            bool(main_answer) and expected and "根据本地资料，可以确定相关候选内容如下" not in main_answer,
            main_answer[:800],
        )
        if final.get("job", {}).get("id"):
            stop_job(str(final["job"]["id"]))
        print(main_answer[:500].replace("\n", "\\n"), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
