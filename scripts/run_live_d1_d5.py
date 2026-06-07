from __future__ import annotations

import json
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


BASE = "http://127.0.0.1:8765"
MODEL = "profile:deepseek-template"
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
RUNTIME_DIR = Path("runtime")
OUT_JSON = RUNTIME_DIR / f"live_d1_d5_{STAMP}.json"
OUT_MD = RUNTIME_DIR / f"live_d1_d5_{STAMP}.md"
D5_DIR = RUNTIME_DIR / f"d5_packaging_export_{STAMP}"
D5_DIR.mkdir(parents=True, exist_ok=True)


def zh(text: str) -> str:
    return text.encode("utf-8").decode("unicode_escape")


UTOPIA = "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5"


def api(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 300) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def jobs() -> list[dict[str, Any]]:
    return list(api("GET", "/api/jobs", timeout=20).get("jobs") or [])


def job_ids() -> set[str]:
    return {str(item.get("id") or "") for item in jobs()}


def post_agent(
    from_agent: str,
    to_agent: str,
    content: str,
    *,
    session_id: str,
    intent: str = "question",
    agent: str | None = None,
    metadata: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    timeout: int = 240,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from_agent": from_agent,
        "to_agent": to_agent,
        "content": content,
        "intent": intent,
        "session_id": session_id,
        "model": MODEL,
        "chat_timeout": timeout,
    }
    if agent:
        payload["agent"] = agent
    if metadata:
        payload["metadata"] = metadata
    if extra:
        payload.update(extra)
    return api("POST", "/api/agent-message", payload, timeout=timeout + 30)


def wait_new_jobs(before: set[str], *, timeout: int = 8) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    latest: list[dict[str, Any]] = []
    while time.time() < deadline:
        latest = [item for item in jobs() if item.get("id") not in before]
        if latest:
            return latest
        time.sleep(1)
    return latest


def wait_job_done(job_id: str, *, timeout: int = 600) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    latest: dict[str, Any] | None = None
    while time.time() < deadline:
        for item in jobs():
            if item.get("id") == job_id:
                latest = item
                break
        if latest and latest.get("status") in {"done", "succeeded", "failed", "stopped"}:
            return latest
        time.sleep(4)
    return latest


def compact_trace(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in trace:
        detail = item.get("detail")
        if isinstance(detail, str) and len(detail) > 1200:
            detail = detail[:1200] + "...[truncated]"
        output.append({"stage": item.get("stage"), "status": item.get("status"), "detail": detail})
    return output


def has_invalid_encoding(response: dict[str, Any]) -> bool:
    return any(item.get("status") == "invalid_encoding" for item in response.get("trace") or [])


def score(test_id: str, response: dict[str, Any], new_jobs: list[dict[str, Any]], final_jobs: list[dict[str, Any]]) -> tuple[bool, str]:
    if has_invalid_encoding(response):
        return False, "input was rejected as invalid encoding"
    answer = str(response.get("answer") or "")
    raw_response = json.dumps(response, ensure_ascii=False, default=str)
    if test_id == "D1":
        ok = bool(answer.strip()) and not new_jobs and ("Utopia" in answer or UTOPIA in answer) and ("Fabric" in answer or "1.20.1" in answer)
        return ok, "local answer returned and no Crawler job was created" if ok else "missing local answer evidence or unexpected Crawler job"
    if test_id == "D2":
        job = final_jobs[0] if final_jobs else (new_jobs[0] if new_jobs else None)
        raw_job = json.dumps(job or {}, ensure_ascii=False, default=str)
        ok = bool(job) and job.get("status") in {"done", "succeeded"} and ("agent_message" in raw_response or "From-Content-To" in raw_response or "agent_message" in raw_job)
        return ok, "MCagent delegated through the message bus and Crawler job completed" if ok else f"delegation missing or job not done: {job.get('status') if job else 'no job'}"
    if test_id == "D3":
        failure_markers = (
            "CrawlerAgent 临时读取网页失败",
            "temporary_url_failed",
            "temporary extraction failed",
            "No URL found for temporary extraction",
        )
        ok = (
            bool(answer.strip())
            and not new_jobs
            and not any(marker in raw_response for marker in failure_markers)
            and ("Trace Viewer" in raw_response or "show-trace" in raw_response)
            and ("temporary_extract" in raw_response or "no_save" in raw_response or "临时" in raw_response)
        )
        return ok, "Crawler temporary extraction answered without persistent job" if ok else "temporary extraction did not produce a grounded no-save answer"
    if test_id == "D4":
        job = final_jobs[0] if final_jobs else (new_jobs[0] if new_jobs else None)
        raw_job = json.dumps(job or {}, ensure_ascii=False, default=str)
        ok = bool(job) and job.get("status") in {"done", "succeeded"} and ("mcagent_context" in raw_job or "MCagent" in raw_job and "CrawlerAgent" in raw_job) and ("agent_message" in raw_job or "From-Content-To" in raw_job or "_send_agent_message" in raw_job)
        return ok, "Crawler asked MCagent for local context over the message bus and completed" if ok else f"missing Crawler->MCagent context exchange or job not done: {job.get('status') if job else 'no job'}"
    if test_id == "D5":
        job = final_jobs[0] if final_jobs else (new_jobs[0] if new_jobs else None)
        files = sorted(path.name for path in D5_DIR.glob("*"))
        raw_job = json.dumps(job or {}, ensure_ascii=False, default=str)
        ok = bool(job) and job.get("status") in {"done", "succeeded"} and "crawler_result.md" in files and "crawler_result.json" in files and ("Python Packaging" in raw_job or "packaging.python.org" in raw_job)
        return ok, "general-domain collection completed and exported user delivery files" if ok else f"job/export incomplete: status={job.get('status') if job else 'no job'}, files={files}"
    return False, "unknown test"


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    session_id = f"live-d1d5-{STAMP}-{case['id']}-{uuid.uuid4().hex[:6]}"
    before = job_ids()
    started = time.time()
    response: dict[str, Any] = {}
    new_jobs: list[dict[str, Any]] = []
    final_jobs: list[dict[str, Any]] = []
    error = ""
    try:
        response = case["call"](session_id)
        new_jobs = wait_new_jobs(before, timeout=10)
        if case.get("wait") and new_jobs:
            final = wait_job_done(str(new_jobs[0].get("id") or ""), timeout=int(case.get("wait_timeout") or 600))
            if final:
                final_jobs = [final]
        elif new_jobs:
            final_jobs = new_jobs
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.time() - started, 3)
    ok, reason = (False, error) if error else score(case["id"], response, new_jobs, final_jobs)
    return {
        "id": case["id"],
        "name": case["name"],
        "session_id": session_id,
        "question": case["question"],
        "verdict": "passed" if ok else "failed",
        "reason": reason,
        "elapsed": elapsed,
        "answer": response.get("answer", ""),
        "response": response,
        "new_jobs": new_jobs,
        "final_jobs": final_jobs,
        "trace": compact_trace(list(response.get("trace") or [])),
        "error": error,
    }


def main() -> int:
    d1 = (
        f"According to local stored sources, briefly introduce Utopia Journey / {UTOPIA} modpack: "
        "core gameplay, version/loader info, and what is still uncertain. Do not start Crawler."
    )
    d2 = (
        f"First answer from local sources: does Utopia Journey / {UTOPIA} have a complete Boss name, summon method, "
        "and drop list? If local evidence is insufficient, use only From-Content-To to ask CrawlerAgent to collect "
        "the missing evidence and save it for MCagent/RAG."
    )
    d3 = (
        "Temporarily search the public web for the official Playwright Trace Viewer docs about trace.zip, "
        "show-trace, and viewing network/console information. Answer only this question; do not save to RAG "
        "and do not create a long-running collection job. If no URL is supplied, discover candidate pages and choose one yourself."
    )
    d4 = (
        f"Before collecting, use From-Content-To to ask MCagent what local sources for Utopia Journey / {UTOPIA} "
        "are still missing. Then decide whether to collect public evidence for MCagent/RAG. Focus on version, "
        "download/archive clues, full mod list, and gameplay progression."
    )
    d5 = (
        "Collect public official sources for two Python Packaging User Guide topics: installing packages with pip and "
        "dependency specifiers. Use sources such as packaging.python.org or official PyPA docs. Save the final user delivery "
        f"to this directory: {D5_DIR.resolve()}. You decide whether each source is useful; then provide inspectable results."
    )
    cases = [
        {
            "id": "D1",
            "name": "MCagent local RAG answer, no Crawler",
            "question": d1,
            "call": lambda session_id: post_agent("User", "MCagent", d1, session_id=session_id, agent="mcagent_rag", timeout=180),
        },
        {
            "id": "D2",
            "name": "MCagent delegates Utopia gaps to Crawler through From-Content-To",
            "question": d2,
            "call": lambda session_id: post_agent("User", "MCagent", d2, session_id=session_id, agent="mcagent_rag", timeout=220),
            "wait": True,
            "wait_timeout": 600,
        },
        {
            "id": "D3",
            "name": "Direct Crawler temporary technical browser extraction, no persistence",
            "question": d3,
            "call": lambda session_id: post_agent(
                "User",
                "CrawlerAgent",
                d3,
                session_id=session_id,
                agent="crawler_agent",
                intent="question",
                extra={"delivery_target": "human"},
                timeout=220,
            ),
        },
        {
            "id": "D4",
            "name": "Crawler asks MCagent for local context before Utopia collection",
            "question": d4,
            "call": lambda session_id: post_agent(
                "User",
                "CrawlerAgent",
                d4,
                session_id=session_id,
                agent="crawler_agent",
                intent="collection_request",
                metadata={"tool": "delegate_crawler"},
                extra={"delivery_target": "MCagent/RAG", "max_tasks": 4},
                timeout=220,
            ),
            "wait": True,
            "wait_timeout": 600,
        },
        {
            "id": "D5",
            "name": "General-domain Crawler collection with user output dir",
            "question": d5,
            "call": lambda session_id: post_agent(
                "User",
                "CrawlerAgent",
                d5,
                session_id=session_id,
                agent="crawler_agent",
                intent="collection_request",
                metadata={"tool": "delegate_crawler"},
                extra={"delivery_target": "human", "max_tasks": 6, "output_dir": str(D5_DIR.resolve())},
                timeout=220,
            ),
            "wait": True,
            "wait_timeout": 700,
        },
    ]
    results = []
    for case in cases:
        result = run_case(case)
        results.append(result)
        print(f"{result['id']} {result['verdict'].upper()} {result['reason']} elapsed={result['elapsed']}s", flush=True)
    report = {"base": BASE, "timestamp": STAMP, "model": MODEL, "d5_export_dir": str(D5_DIR.resolve()), "results": results}
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        f"# Live D1-D5 Test {STAMP}",
        "",
        f"Base: `{BASE}`",
        f"Model: `{MODEL}`",
        f"D5 export dir: `{D5_DIR.resolve()}`",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"## {result['id']} - {result['name']}",
                "",
                f"Verdict: **{result['verdict']}**",
                f"Reason: {result['reason']}",
                f"Elapsed: `{result['elapsed']}s`",
                "",
                "### Q",
                str(result["question"]),
                "",
                "### A",
                str(result.get("answer") or "").strip() or "(empty)",
                "",
                "### New Jobs",
                "```json",
                json.dumps(result.get("new_jobs") or [], ensure_ascii=False, indent=2, default=str)[:14000],
                "```",
                "",
                "### Final Jobs",
                "```json",
                json.dumps(result.get("final_jobs") or [], ensure_ascii=False, indent=2, default=str)[:18000],
                "```",
                "",
                "### Trace",
                "```json",
                json.dumps(result.get("trace") or [], ensure_ascii=False, indent=2, default=str)[:18000],
                "```",
                "",
            ]
        )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"JSON {OUT_JSON.resolve()}", flush=True)
    print(f"MD {OUT_MD.resolve()}", flush=True)
    return 0 if all(item["verdict"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
