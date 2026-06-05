from __future__ import annotations

import argparse
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


def check_backend(base: str) -> None:
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"Backend is not reachable at {base}: {exc}") from exc
    require("backend_reachable", response.status == 200, body[:500])


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live browser UI E2E check against a running MCagent web server.")
    parser.add_argument("--base", default=DEFAULT_BASE, help="MCagent web base URL.")
    parser.add_argument("--timeout", type=int, default=240, help="Seconds to wait for the streamed answer.")
    parser.add_argument("--expected-profile", default="", help="Optional selected profile id, such as deepseek-template.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime", help="Directory for screenshots and text artifacts.")
    parser.add_argument(
        "--question",
        default="Create mod live UI E2E: explain rotational power, stress, transmission, and automation entry path from local data.",
        help="Question to send through the real UI.",
    )
    parser.add_argument(
        "--answer-pattern",
        default="",
        help="Optional regular expression that the streamed answer and restored history must match.",
    )
    args = parser.parse_args()

    base = args.base.rstrip("/")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    check_backend(base)

    run_id = time.strftime("%Y%m%d_%H%M%S")
    screenshot_path = output_dir / f"live_ui_e2e_{run_id}.png"
    answer_path = output_dir / f"live_ui_e2e_{run_id}.txt"
    question = args.question
    answer_regex = args.answer_pattern or r"source|来源|资料|证据|本地|local|RAG|索引"
    answer_pattern = re.compile(answer_regex, re.I)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1365, "height": 900})
        page.goto(base + f"/?live-ui-e2e={run_id}", wait_until="domcontentloaded", timeout=15000)

        page.wait_for_function(
            """() => {
                const select = document.querySelector('#modelSelect');
                return select && select.value && select.options && select.options.length > 0;
            }""",
            timeout=15000,
        )
        selected_profile = page.locator("#modelSelect").input_value(timeout=10000)
        if args.expected_profile:
            require("selected_profile", selected_profile == args.expected_profile, selected_profile)
        else:
            require("selected_profile_present", bool(selected_profile), "model select has no value")

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
                return hasQuestion && buttonReady && answer.length > 120 && hasUseful;
            }""",
            arg=[before_articles, before_assistant, question, answer_regex],
            timeout=args.timeout * 1000,
        )
        page.wait_for_timeout(1500)

        before_data = page.evaluate(
            """(q) => {
                const articles = [...document.querySelectorAll('article.message.assistant[data-final-answer="true"]')].map(a => a.innerText || '');
                const answer = articles[articles.length - 1] || '';
                const storage = localStorage.getItem('mcagent.sessionsByAgent') || '';
                return {
                    articleCount: articles.length,
                    lastAnswer: answer,
                    bodyHasQuestion: document.body.innerText.includes(q),
                    storageHasQuestion: storage.includes(q),
                    storageLength: storage.length,
                };
            }""",
            question,
        )

        answer_text = str(before_data["lastAnswer"])
        answer_path.write_text(answer_text, encoding="utf-8")
        require("question_rendered", bool(before_data["bodyHasQuestion"]), answer_text[:500])
        require("question_saved_to_storage", bool(before_data["storageHasQuestion"]), str(before_data["storageLength"]))
        require("answer_not_network_error", "network error" not in answer_text.lower(), answer_text[:500])
        require("answer_mentions_topic", bool(answer_pattern.search(answer_text)), answer_text[:500])

        page.reload(wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)
        after_data = page.evaluate(
            """([q, pattern]) => {
                const text = document.body.innerText || '';
                const storage = localStorage.getItem('mcagent.sessionsByAgent') || '';
                return {
                    hasQuestion: text.includes(q),
                    hasAnswer: new RegExp(pattern, 'i').test(text),
                    hasNetworkError: /network error/i.test(text),
                    hasQuestionInStorage: storage.includes(q),
                    storageLength: storage.length,
                };
            }""",
            [question, answer_regex],
        )
        page.screenshot(path=str(screenshot_path), full_page=False)
        browser.close()

    require("history_question_after_reload", bool(after_data["hasQuestion"]), str(after_data))
    require("history_answer_after_reload", bool(after_data["hasAnswer"]), str(after_data))
    require("history_no_network_error_after_reload", not bool(after_data["hasNetworkError"]), str(after_data))
    require("history_storage_after_reload", bool(after_data["hasQuestionInStorage"]), str(after_data["storageLength"]))

    print(f"selected_profile={selected_profile}", flush=True)
    print(f"answer={answer_path}", flush=True)
    print(f"screenshot={screenshot_path}", flush=True)
    print("LIVE UI E2E CHECK PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"LIVE UI E2E CHECK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
