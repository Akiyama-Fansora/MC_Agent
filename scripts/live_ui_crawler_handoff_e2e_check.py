from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = os.environ.get("MCAGENT_BASE", "http://127.0.0.1:8765").rstrip("/")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def require(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}", flush=True)
        return
    raise AssertionError(f"{name}: {detail}")


def api_json(base: str, path: str, *, timeout: int = 20) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body)


def check_backend(base: str) -> None:
    try:
        data = api_json(base, "/api/health", timeout=10)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"Backend is not reachable at {base}: {exc}") from exc
    require("backend_reachable", bool(data.get("ok")), str(data))


def newest_crawler_job(base: str, marker: str, previous_ids: set[str]) -> dict | None:
    jobs = api_json(base, "/api/jobs", timeout=20).get("jobs") or []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("kind") != "crawler" or str(job.get("id") or "") in previous_ids:
            continue
        text = json.dumps(job, ensure_ascii=False, default=str)
        if marker in text:
            return job
    return None


def wait_for_job(base: str, job_id: str, *, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    last: dict = {}
    while time.time() < deadline:
        jobs = api_json(base, "/api/jobs", timeout=20).get("jobs") or []
        for job in jobs:
            if isinstance(job, dict) and job.get("id") == job_id:
                last = job
                if job.get("status") not in {"queued", "running"}:
                    return job
        time.sleep(8)
    raise AssertionError(f"job_timeout: {job_id}: {json.dumps(last, ensure_ascii=False, default=str)[:2000]}")


def audit_from_job(job: dict) -> dict:
    readable = job.get("readable") if isinstance(job.get("readable"), dict) else {}
    audit = readable.get("self_audit") if isinstance(readable.get("self_audit"), dict) else {}
    if audit:
        return audit
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return result.get("self_audit") if isinstance(result.get("self_audit"), dict) else {}


def send_ui_question(page, question: str, *, answer_regex: str, timeout_seconds: int) -> str:  # noqa: ANN001
    before_articles = page.locator("article").count()
    before_assistant = page.locator('article.message.assistant[data-final-answer="true"]').count()
    page.locator("textarea").fill(question, timeout=10000)
    page.locator('button[type="submit"]').click(timeout=10000)
    page.wait_for_function(
        """([before, beforeAssistant, q, pattern]) => {
            const button = document.querySelector('button[type="submit"]');
            const articles = [...document.querySelectorAll('article')].map(a => a.innerText || '');
            if (articles.length < before + 2) return false;
            const finalAssistant = [...document.querySelectorAll('article.message.assistant[data-final-answer="true"]')];
            if (finalAssistant.length < beforeAssistant + 1) return false;
            const answer = finalAssistant[finalAssistant.length - 1]?.innerText || '';
            const hasQuestion = document.body.innerText.includes(q);
            const hasUseful = new RegExp(pattern, 'i').test(answer);
            const buttonReady = button && !button.disabled && button.innerText.length > 0;
            return hasQuestion && buttonReady && answer.length > 40 && hasUseful;
        }""",
        arg=[before_articles, before_assistant, question, answer_regex],
        timeout=timeout_seconds * 1000,
    )
    page.wait_for_timeout(1000)
    return str(
        page.evaluate(
            """() => {
                const articles = [...document.querySelectorAll('article.message.assistant[data-final-answer="true"]')].map(a => a.innerText || '');
                return articles[articles.length - 1] || '';
            }"""
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live UI E2E check for MCagent handoff to CrawlerAgent.")
    parser.add_argument("--base", default=DEFAULT_BASE, help="MCagent web base URL.")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds to wait for the crawler job.")
    parser.add_argument("--chat-timeout", type=int, default=360, help="Seconds to wait for each UI answer.")
    parser.add_argument("--expected-profile", default="deepseek-template", help="Expected selected model profile id.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime", help="Directory for screenshots and text artifacts.")
    parser.add_argument("--marker", default="Farmer's Delight UI handoff E2E", help="Unique marker used to find the created job.")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    check_backend(base)

    previous_ids = {
        str(job.get("id") or "")
        for job in api_json(base, "/api/jobs", timeout=20).get("jobs") or []
        if isinstance(job, dict)
    }
    run_id = time.strftime("%Y%m%d_%H%M%S")
    screenshot_path = output_dir / f"live_ui_crawler_handoff_{run_id}.png"
    transcript_path = output_dir / f"live_ui_crawler_handoff_{run_id}.txt"

    handoff_question = (
        f"请让 CrawlerAgent 获取农夫乐事 Farmer's Delight 的公开基础资料并交给 MCagent/RAG 使用。"
        f"测试标记：{args.marker}。"
        "采集重点是项目介绍、版本/下载页线索、玩法入门和可靠来源，自审时说明接受、拒绝、待复核来源。"
    )
    answer_question = "基于刚才 CrawlerAgent 补到/复用的资料，农夫乐事 Farmer's Delight 新手应该怎样开始？请给出来源线索。"
    lines: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1365, "height": 900})
        page.goto(base + f"/?live-ui-crawler-e2e={run_id}", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_function(
            """() => {
                const select = document.querySelector('#modelSelect');
                return select && select.value && select.options && select.options.length > 0;
            }""",
            timeout=15000,
        )
        selected_profile = page.locator("#modelSelect").input_value(timeout=10000)
        require("selected_profile", selected_profile == args.expected_profile, selected_profile)

        handoff_answer = send_ui_question(
            page,
            handoff_question,
            answer_regex=r"Crawler|采集|任务|job|转达|接单|MCagent/RAG",
            timeout_seconds=args.chat_timeout,
        )
        lines.append("=== HANDOFF ANSWER ===")
        lines.append(handoff_answer)

        job = None
        deadline = time.time() + 120
        while time.time() < deadline:
            job = newest_crawler_job(base, args.marker, previous_ids)
            if job:
                break
            time.sleep(4)
        require("crawler_job_created", bool(job), args.marker)
        job_id = str(job.get("id"))
        require("crawler_job_has_id", bool(job_id), str(job))

        final_job = wait_for_job(base, job_id, timeout_seconds=args.timeout)
        require("crawler_job_finished", final_job.get("status") in {"succeeded", "failed", "stopped"}, str(final_job.get("status")))
        require("crawler_job_succeeded", final_job.get("status") == "succeeded", json.dumps(final_job, ensure_ascii=False, default=str)[:3000])
        require("crawler_job_not_stopped", final_job.get("status") != "stopped", json.dumps(final_job, ensure_ascii=False, default=str)[:2000])

        audit = audit_from_job(final_job)
        counts = audit.get("counts") if isinstance(audit.get("counts"), dict) else {}
        accepted = int(counts.get("accepted") or 0)
        pending = int(counts.get("pending_review") or 0)
        rejected = int(counts.get("rejected") or 0)
        require("self_audit_present", bool(audit), json.dumps(final_job, ensure_ascii=False, default=str)[:2000])
        require("self_audit_accounting", accepted + pending + rejected > 0, str(counts))
        require("self_audit_has_review_notes", "review_summary" in audit or any("review_note" in item for item in (audit.get("accepted_sources") or [])), str(audit)[:1000])
        require("crawler_has_accepted_or_pending", accepted + pending > 0, str(counts))

        answer_text = send_ui_question(
            page,
            answer_question,
            answer_regex=r"农夫|Farmer|Delight|来源|资料|证据|RAG|本地",
            timeout_seconds=args.chat_timeout,
        )
        lines.append("=== MCAGENT ANSWER AFTER CRAWLER ===")
        lines.append(answer_text)
        require("mcagent_answer_mentions_topic", bool(re.search(r"农夫|Farmer|Delight", answer_text, re.I)), answer_text[:500])
        require("mcagent_answer_not_network_error", "network error" not in answer_text.lower(), answer_text[:500])
        require(
            "mcagent_answer_uses_collected_material",
            not bool(re.search(r"未找到|没有找到|无法找到|未收录|未补到|没有包含具体", answer_text)),
            answer_text[:800],
        )

        page.reload(wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)
        restored = page.evaluate(
            """([handoff, answer]) => {
                const text = document.body.innerText || '';
                const storage = localStorage.getItem('mcagent.sessionsByAgent') || '';
                return {
                    hasHandoff: text.includes(handoff),
                    hasAnswerQuestion: text.includes(answer),
                    hasStorageHandoff: storage.includes(handoff),
                    hasStorageAnswerQuestion: storage.includes(answer),
                    hasNetworkError: /network error/i.test(text),
                    textLength: text.length,
                    storageLength: storage.length,
                };
            }""",
            [handoff_question, answer_question],
        )
        page.screenshot(path=str(screenshot_path), full_page=False)
        browser.close()

    require("history_handoff_after_reload", bool(restored["hasHandoff"]), str(restored))
    require("history_answer_question_after_reload", bool(restored["hasAnswerQuestion"]), str(restored))
    require("history_storage_after_reload", bool(restored["hasStorageHandoff"] and restored["hasStorageAnswerQuestion"]), str(restored))
    require("history_no_network_error_after_reload", not bool(restored["hasNetworkError"]), str(restored))

    lines.append("=== CRAWLER JOB ===")
    lines.append(json.dumps({"id": job_id, "status": final_job.get("status"), "self_audit": audit}, ensure_ascii=False, indent=2, default=str))
    transcript_path.write_text("\n\n".join(lines), encoding="utf-8")
    print(f"selected_profile={selected_profile}", flush=True)
    print(f"job_id={job_id}", flush=True)
    print(f"transcript={transcript_path}", flush=True)
    print(f"screenshot={screenshot_path}", flush=True)
    print("LIVE UI CRAWLER HANDOFF E2E CHECK PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"LIVE UI CRAWLER HANDOFF E2E CHECK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
