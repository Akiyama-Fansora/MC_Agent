from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import urllib.request


BASE = os.environ.get("MCAGENT_BASE", "http://127.0.0.1:8765").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def request_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body or "{}")


def stream_chat(payload: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any], float]:
    started = time.time()
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    events: list[dict[str, Any]] = []
    current_event = "message"
    data_lines: list[str] = []
    final: dict[str, Any] | None = None
    with urllib.request.urlopen(req, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
                continue
            if line.strip() or not data_lines:
                continue
            value = json.loads("\n".join(data_lines))
            events.append({"event": current_event, "data": value})
            if current_event == "response":
                final = value
                break
            current_event = "message"
            data_lines = []
    if final is None:
        raise AssertionError("stream ended without response event")
    return events, final, time.time() - started


def answer_text(final: dict[str, Any]) -> str:
    message = final.get("agent_message")
    if isinstance(message, dict) and message.get("content"):
        return str(message.get("content") or "")
    return str(final.get("answer") or "")


def trace_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "trace" or not isinstance(event.get("data"), dict):
            continue
        data = event["data"]
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        compact: dict[str, Any] = {}
        for key in ("tool", "from_agent", "to_agent", "job_id", "status", "reason", "tuple", "collection_target", "delivery_target"):
            if key in detail:
                compact[key] = detail.get(key)
        decision = detail.get("decision") if isinstance(detail.get("decision"), dict) else {}
        if decision:
            compact["decision"] = {
                key: decision.get(key)
                for key in ("tool", "reason", "collection_target", "delivery_target", "action_plan")
                if key in decision
            }
        output.append({"stage": data.get("stage"), "status": data.get("status"), "detail": compact})
    return output[-16:]


def recent_job(job_id: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    jobs = request_json("GET", "/api/jobs", timeout=15).get("jobs") or []
    for job in jobs:
        if isinstance(job, dict) and str(job.get("id") or "") == job_id:
            return job
    return None


def wait_job(job_id: str, timeout: int) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = recent_job(job_id)
        if isinstance(last, dict) and str(last.get("status") or "") in {"succeeded", "failed", "stopped"}:
            return last
        time.sleep(3)
    return last


def job_id_from_response(final: dict[str, Any], events: list[dict[str, Any]]) -> str:
    job = final.get("job") if isinstance(final.get("job"), dict) else {}
    if job.get("id"):
        return str(job.get("id"))
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        if detail.get("job_id"):
            return str(detail.get("job_id"))
    return ""


def verdict(checks: dict[str, bool]) -> str:
    return "pass" if checks and all(checks.values()) else "fail"


def run_case(case: dict[str, Any], session_id: str) -> dict[str, Any]:
    payload = {
        "session_id": session_id,
        "agent": case["agent"],
        "question": case["question"],
        "max_tokens": case.get("max_tokens", 700),
    }
    events, final, elapsed = stream_chat(payload, timeout=case.get("timeout", 180))
    answer = answer_text(final)
    trace = trace_summary(events)
    job_id = job_id_from_response(final, events)
    job = wait_job(job_id, case.get("job_timeout", 0)) if job_id and case.get("job_timeout", 0) else recent_job(job_id)
    checks = case["checks"](answer, final, trace, job)
    return {
        "id": case["id"],
        "title": case["title"],
        "agent": case["agent"],
        "question": case["question"],
        "answer": answer,
        "elapsed_seconds": round(elapsed, 3),
        "trace": trace,
        "job_id": job_id,
        "job_status": job.get("status") if isinstance(job, dict) else "",
        "job_summary": job.get("summary") if isinstance(job, dict) else "",
        "semantic_checks": checks,
        "verdict": verdict(checks),
    }


def contains_any(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def main() -> int:
    health = request_json("GET", "/api/health", timeout=10)
    if not health.get("ok"):
        raise SystemExit(f"backend unhealthy: {health}")
    session = f"dual-agent-matrix-{int(time.time())}"
    cases = [
        {
            "id": "A1",
            "title": "MCagent local inventory answer",
            "agent": "mcagent_rag",
            "question": "本地现在有哪些整合包或模组资料？请只根据本地资料简单分组介绍。",
            "checks": lambda answer, _final, trace, _job: {
                "answers_with_local_scope": contains_any(answer, ["本地", "资料", "知识库", "整合包", "模组"]),
                "does_not_start_crawler_job": not any(step.get("detail", {}).get("job_id") for step in trace),
            },
        },
        {
            "id": "A2",
            "title": "MCagent delegates a missing-data task",
            "agent": "mcagent_rag",
            "question": "如果本地农夫乐事资料不足，请通过 From-Content-To 转达 CrawlerAgent 去补充公开资料，然后告诉我任务状态。",
            "job_timeout": 90,
            "checks": lambda answer, _final, trace, job: {
                "mentions_crawler_or_task": contains_any(answer, ["Crawler", "任务", "采集", "补充"]),
                "message_bus_trace": any(step.get("detail", {}).get("to_agent") in {"CrawlerAgent", "crawler_agent"} or "CrawlerAgent" in str(step.get("detail", {}).get("tuple") or "") for step in trace),
                "job_created_or_reported": isinstance(job, dict) and bool(job.get("id")),
            },
        },
        {
            "id": "A3",
            "title": "Crawler direct general web extraction",
            "agent": "crawler_agent",
            "question": "请临时获取 https://httpbin.org/json 的公开内容，总结 slideshow title 和 slide 数量，不要入库。",
            "timeout": 180,
            "checks": lambda answer, _final, trace, _job: {
                "answers_httpbin_content": contains_any(answer, ["slideshow", "Sample Slide Show", "slide"]),
                "uses_crawler_tooling": any(step.get("detail", {}).get("tool") for step in trace),
                "not_mc_specific": "整合包" not in answer[:300],
            },
        },
        {
            "id": "A4",
            "title": "Crawler asks MCagent for local gaps",
            "agent": "crawler_agent",
            "question": "请先用 From-Content-To 问 MCagent 本地关于农夫乐事资料缺什么，再按缺口补充公开资料；先返回你问到的缺口和任务状态。",
            "job_timeout": 120,
            "checks": lambda answer, _final, trace, job: {
                "mentions_gap_or_mcagent": contains_any(answer, ["MCagent", "缺", "本地", "资料"]),
                "shows_mcagent_gap_context": contains_any(answer, ["MCagent 返回", "本地上下文", "本地已有", "没有找到", "仍需", "缺口"]),
                "mcagent_context_step_completed": any(step.get("status") == "mcagent_context_completed" or step.get("detail", {}).get("tool") == "mcagent_context" for step in trace),
                "job_created": isinstance(job, dict) and bool(job.get("id")),
                "has_job_status": isinstance(job, dict) and str(job.get("status") or "") in {"queued", "running", "succeeded", "failed", "stopped"},
            },
        },
        {
            "id": "A5",
            "title": "Session context continuity",
            "agent": "mcagent_rag",
            "question": "延续刚才的话，上一轮我让谁去补农夫乐事资料？你只根据当前会话上下文回答。",
            "checks": lambda answer, _final, _trace, _job: {
                "remembers_crawler": contains_any(answer, ["Crawler", "CrawlerAgent", "采集", "From-Content-To", "补充"]),
                "mentions_farmer": contains_any(answer, ["农夫乐事", "Farmer"]),
            },
        },
    ]
    results = []
    for case in cases:
        print(f"RUN {case['id']} {case['title']}", flush=True)
        try:
            results.append(run_case(case, session))
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "id": case["id"],
                    "title": case["title"],
                    "agent": case["agent"],
                    "question": case["question"],
                    "answer": "",
                    "elapsed_seconds": 0,
                    "trace": [],
                    "semantic_checks": {"exception": False},
                    "verdict": "fail",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    report_dir = ROOT / "runtime" / "dual_agent_dialogue_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"dual_agent_dialogue_{time.strftime('%Y%m%d_%H%M%S')}.json"
    markdown_path = path.with_suffix(".md")
    ascii_path = path.with_name(path.stem + "_ascii.txt")
    payload = {"session_id": session, "results": results, "passed": all(item["verdict"] == "pass" for item in results)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [f"# Dual Agent Dialogue Matrix {time.strftime('%Y-%m-%d %H:%M:%S')}", "", f"Session: `{session}`", ""]
    for item in results:
        lines.extend(
            [
                f"## {item['id']} {item['title']} - {item['verdict']}",
                "",
                f"Agent: `{item['agent']}`",
                "",
                f"Q: {item['question']}",
                "",
                f"A: {item.get('answer') or item.get('error') or ''}",
                "",
                f"Checks: `{json.dumps(item.get('semantic_checks'), ensure_ascii=False)}`",
                "",
                f"Job: `{item.get('job_id') or ''}` `{item.get('job_status') or ''}` {item.get('job_summary') or ''}",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    ascii_lines = []
    for item in results:
        ascii_lines.extend(
            [
                f"{item['id']} {item['title']} {item['verdict']}",
                "Q: " + item["question"].encode("unicode_escape").decode("ascii"),
                "A: " + (item.get("answer") or item.get("error") or "").encode("unicode_escape").decode("ascii")[:4000],
                "checks: " + json.dumps(item.get("semantic_checks"), ensure_ascii=False).encode("unicode_escape").decode("ascii"),
                "",
            ]
        )
    ascii_path.write_text("\n".join(ascii_lines), encoding="ascii")
    print(json.dumps({"report": str(path), "markdown": str(markdown_path), "ascii": str(ascii_path), "passed": payload["passed"]}, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
