from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BASE = os.environ.get("MCAGENT_BASE", "http://127.0.0.1:8765").rstrip("/")
TEST_MODEL = os.environ.get("MCAGENT_TEST_MODEL", "").strip()
ROOT = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def request_json(method: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body or "{}")


def post_json(path: str, payload: dict, timeout: int = 30) -> dict:
    return request_json("POST", path, payload, timeout=timeout)


def get_json(path: str, timeout: int = 30) -> dict:
    return request_json("GET", path, timeout=timeout)


def read_stream(payload: dict, timeout: int = 180) -> tuple[list[dict], dict | None]:
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
        "max_tokens": extra.pop("max_tokens", 800),
        **extra,
    }
    events, final = read_stream(payload, timeout=timeout)
    if final is None:
        raise AssertionError(f"no final response for {agent}: {question}")
    return events, final


def answer_text(final: dict) -> str:
    agent_message = final.get("agent_message")
    if isinstance(agent_message, dict) and agent_message.get("content"):
        return str(agent_message["content"])
    return str(final.get("answer") or "")


def trace_steps(events: list[dict]) -> list[dict]:
    return [event["data"] for event in events if event.get("event") == "trace" and isinstance(event.get("data"), dict)]


def has_trace(events: list[dict], stage: str | None = None, status: str | None = None, tool: str | None = None) -> bool:
    for step in trace_steps(events):
        if stage is not None and step.get("stage") != stage:
            continue
        if status is not None and step.get("status") != status:
            continue
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        if tool is not None and detail.get("tool") != tool:
            continue
        return True
    return False


def message_tuple(events: list[dict]) -> list[str] | None:
    for step in trace_steps(events):
        if step.get("stage") == "message" and step.get("status") == "received":
            detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
            value = detail.get("tuple")
            if isinstance(value, list) and len(value) == 3:
                return [str(item) for item in value]
    return None


def stop_job(job_id: str) -> None:
    try:
        post_json("/api/jobs/stop", {"id": job_id}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN stop job {job_id}: {exc}", flush=True)


def find_job(job_id: str) -> dict | None:
    jobs = get_json("/api/jobs", timeout=20).get("jobs") or []
    for job in jobs:
        if isinstance(job, dict) and str(job.get("id")) == str(job_id):
            return job
    return None


def wait_job(job_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        job = find_job(job_id)
        if not job:
            time.sleep(2)
            continue
        status = str(job.get("status") or "")
        readable = job.get("readable") if isinstance(job.get("readable"), dict) else {}
        current = readable.get("current") or job.get("title") or ""
        snapshot = f"{status}: {current}"
        if snapshot != last_status:
            print(f"  {label} job {job_id}: {snapshot}", flush=True)
            last_status = snapshot
        if status in {"completed", "succeeded", "failed", "stopped"}:
            return job
        time.sleep(5)
    job = find_job(job_id) or {}
    raise AssertionError(f"{label} job timeout after {timeout}s; last={json.dumps(job, ensure_ascii=False, default=str)[:1200]}")


def require(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}", flush=True)
        return
    raise AssertionError(f"{name}: {detail}")


def run_direction_1(session: str) -> None:
    print("\nD1 MCagent local RAG answer", flush=True)
    q = "\u6839\u636e\u672c\u5730\u8d44\u6599\uff0c\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c53.0 \u6709\u54ea\u4e9b\u73a9\u6cd5\u6807\u7b7e\u548c\u7279\u8272\uff1f"
    events, final = chat(session + "-d1", "mcagent_rag", q, timeout=300, show_context=True, max_tokens=1000)
    text = answer_text(final)
    require("d1_answer_present", bool(text.strip()), text[:300])
    require("d1_no_crawler_job", not final.get("job") and not final.get("delegation"), json.dumps(final.get("delegation"), ensure_ascii=False))
    require("d1_used_local_retrieval", has_trace(events, "retrieve") or bool(final.get("sources")), "no retrieve trace or sources")
    require("d1_topic_grounded", "\u4e4c\u6258\u90a6" in text or "Utopia" in text or "\u4ea4\u6613" in text or "\u63a2\u7d22" in text, text[:500])


def run_direction_2(session: str, stop_jobs: bool) -> None:
    print("\nD2 MCagent delegates Crawler for missing material", flush=True)
    q = "\u73b0\u5728\u4e4c\u6258\u90a6\u6574\u5408\u5305\u4f60\u672c\u5730\u8fd8\u7f3a\u54ea\u4e9b\u8d44\u6599\uff0c\u5217\u51fa\u6765\uff0c\u7136\u540e\u8ba9 Crawler \u53bb\u8865\u5145\u3002"
    _events, final = chat(session + "-d2", "mcagent_rag", q, timeout=240, max_tokens=900)
    delegation = final.get("delegation") if isinstance(final.get("delegation"), dict) else {}
    require("d2_delegation_created", bool(delegation), answer_text(final)[:500])
    require("d2_requested_by_mcagent", delegation.get("requested_by") == "user_via_mcagent", json.dumps(delegation, ensure_ascii=False))
    require("d2_delivery_to_rag", delegation.get("delivery_target") == "MCagent/RAG", json.dumps(delegation, ensure_ascii=False))
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    require("d2_job_started", bool(job.get("id")), json.dumps(final, ensure_ascii=False, default=str)[:800])
    if stop_jobs and job.get("id"):
        stop_job(str(job["id"]))


def run_direction_3(session: str) -> None:
    print("\nD3 direct Crawler URL extraction without saving", flush=True)
    q = "\u603b\u7ed3\u4e00\u4e0b https://example.com/ \u7684\u5185\u5bb9\u7ed9\u6211\uff0c\u4e0d\u7528\u4fdd\u5b58\u5230\u672c\u5730\u3002"
    events, final = chat(session + "-d3", "crawler_agent", q, timeout=180, max_tokens=700)
    text = answer_text(final)
    temp = final.get("temporary_extract") if isinstance(final.get("temporary_extract"), dict) else {}
    require("d3_message_goes_to_crawler", (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent", str(message_tuple(events)))
    require("d3_no_background_job", not final.get("job") and not final.get("delegation"), json.dumps(final.get("delegation"), ensure_ascii=False))
    require("d3_no_save_metadata", temp.get("saved_to_local") is False or "\u4e0d\u4fdd\u5b58" in text or "Example Domain" in text, json.dumps(temp, ensure_ascii=False))
    require("d3_answer_mentions_page", "Example Domain" in text or "example.com" in text, text[:500])


def run_direction_4(session: str, wait_seconds: int) -> None:
    print("\nD4 Crawler asks MCagent gaps, then fills MCagent/RAG", flush=True)
    q = "\u95ee\u4e0bMCAgent\u4e4c\u6258\u90a6\u6574\u5408\u5305\u8fd8\u7f3a\u54ea\u4e9b\u4e1c\u897f \u4f60\u53bb\u7f51\u4e0a\u627e\u8865\u7ed9\u4ed6"
    events, final = chat(session + "-d4", "crawler_agent", q, timeout=240, max_tokens=900, max_tasks=8)
    require("d4_message_goes_to_crawler", (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent", str(message_tuple(events)))
    require(
        "d4_uses_inter_agent_context",
        has_trace(events, "decide", "mcagent_context_selected") or has_trace(events, "decide", "mcagent_context_deferred_to_crawler_job"),
        json.dumps(trace_steps(events)[-8:], ensure_ascii=False, default=str),
    )
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    require("d4_job_started", bool(job.get("id")), json.dumps(final, ensure_ascii=False, default=str)[:1000])
    completed = wait_job(str(job["id"]), wait_seconds, "d4")
    require("d4_job_completed", completed.get("status") in {"completed", "succeeded"}, json.dumps(completed, ensure_ascii=False, default=str)[:1200])
    result = completed.get("result") if isinstance(completed.get("result"), dict) else {}
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    require(
        "d4_no_planner_fallback",
        not plan.get("planner_error") and "fallback_after_llm_planner_error" not in str(plan.get("strategy") or ""),
        json.dumps({"strategy": plan.get("strategy"), "planner_error": plan.get("planner_error")}, ensure_ascii=False, default=str),
    )
    task_results = result.get("tasks") if isinstance(result.get("tasks"), list) else []
    saw_context = any(isinstance(item, dict) and item.get("source") == "mcagent_context" for item in task_results)
    require("d4_job_used_mcagent_context", saw_context, json.dumps(task_results[-5:], ensure_ascii=False, default=str))
    context_steps = [item for item in task_results if isinstance(item, dict) and item.get("source") == "mcagent_context"]
    if context_steps:
        require(
            "d4_mcagent_context_fast_enough",
            float(context_steps[0].get("elapsed_seconds") or 9999) < 60,
            json.dumps(context_steps[0], ensure_ascii=False, default=str)[:1200],
        )
        require(
            "d4_agent_exchange_visible",
            bool(context_steps[0].get("agent_message_exchange")),
            json.dumps(context_steps[0], ensure_ascii=False, default=str)[:1200],
        )
    external_steps = [item for item in task_results if isinstance(item, dict) and item.get("source") != "mcagent_context"]
    require("d4_continued_after_mcagent_context", bool(external_steps), json.dumps(task_results, ensure_ascii=False, default=str)[:1200])
    require("d4_has_usable_material", int(completed.get("success_count") or result.get("success_count") or 0) > 0 or "usable" in json.dumps(result, ensure_ascii=False).lower(), json.dumps(result, ensure_ascii=False, default=str)[:1200])
    _events, final_check = chat(
        session + "-d4-check",
        "mcagent_rag",
        "\u4e4c\u6258\u90a6\u6574\u5408\u5305\u6a21\u7ec4\u6570\u91cf\u6709\u54ea\u4e9b\u8bf4\u6cd5\uff1f",
        timeout=180,
        show_context=True,
        max_tokens=800,
    )
    check_text = answer_text(final_check)
    require("d4_mcagent_can_use_collected_context", any(value in check_text + str(final_check.get("context") or "") for value in ["430", "423", "505", "Immersive Aircraft"]), check_text[:700])


def run_direction_5(session: str, wait_seconds: int, output_dir: Path) -> None:
    print("\nD5 Crawler saves structured public data locally", flush=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    q = (
        "\u7528 Crawler \u6253\u5f00 https://webscraper.io/test-sites/e-commerce/static/computers/laptops "
        "\u63d0\u53d6\u524d 5 \u4e2a\u5546\u54c1\u7684\u540d\u79f0\u3001\u4ef7\u683c\u3001\u94fe\u63a5\uff0c"
        f"\u4fdd\u5b58\u4e3a xlsx\u3001csv\u3001json \u5230 {output_dir}\u3002"
    )
    events, final = chat(session + "-d5", "crawler_agent", q, timeout=240, max_tokens=900)
    require("d5_message_goes_to_crawler", (message_tuple(events) or ["", "", ""])[2] == "CrawlerAgent", str(message_tuple(events)))
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    require("d5_job_started", bool(job.get("id")), json.dumps(final, ensure_ascii=False, default=str)[:1000])
    completed = wait_job(str(job["id"]), wait_seconds, "d5")
    require("d5_job_completed", completed.get("status") in {"completed", "succeeded"}, json.dumps(completed, ensure_ascii=False, default=str)[:1200])
    expected = [output_dir / "items.xlsx", output_dir / "items.csv", output_dir / "items.json", output_dir / "manifest.json"]
    require("d5_files_exist", all(path.exists() for path in expected), "\n".join(str(path) for path in expected if not path.exists()))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    require("d5_manifest_ok", manifest.get("status") == "ok" and int(manifest.get("record_count") or 0) >= 1, json.dumps(manifest, ensure_ascii=False)[:800])


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live five-direction MCagent/CrawlerAgent test matrix.")
    parser.add_argument("--skip-long", action="store_true", help="Run D1-D3 only; D4-D5 start and wait for background crawler jobs.")
    parser.add_argument("--job-timeout", type=int, default=480, help="Seconds to wait for each long background crawler job.")
    parser.add_argument("--keep-jobs", action="store_true", help="Do not stop short delegation jobs after D2.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "manual_tests" / "live_five_direction_products" / time.strftime("%Y%m%d_%H%M%S"),
        help="Directory used by D5 structured-data save test.",
    )
    args = parser.parse_args()
    try:
        health = get_json("/api/health", timeout=10)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"Backend is not reachable at {BASE}: {exc}") from exc
    require("backend_health", bool(health.get("ok")), json.dumps(health, ensure_ascii=False))

    session = f"live-five-{int(time.time())}"
    run_direction_1(session)
    run_direction_2(session, stop_jobs=not args.keep_jobs)
    run_direction_3(session)
    if not args.skip_long:
        run_direction_4(session, args.job_timeout)
        run_direction_5(session, args.job_timeout, args.output_dir)
    print("\nLIVE FIVE-DIRECTION CHECK PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"LIVE FIVE-DIRECTION CHECK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
