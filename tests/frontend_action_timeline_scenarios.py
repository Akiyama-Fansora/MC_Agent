from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class FrontendHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/agents":
            return self._json(
                {
                    "agents": [
                        {"id": "mcagent_rag", "name": "MCagent", "description": "local Minecraft assistant"},
                        {"id": "crawler_agent", "name": "CrawlerAgent", "description": "crawler assistant"},
                    ]
                }
            )
        if self.path == "/api/llm-profiles":
            return self._json(
                {
                    "profiles": [{"id": "test-profile", "name": "Test", "model": "fake-model", "provider": "test"}],
                    "assignments": {"mcagent_rag": "test-profile", "crawler_agent": "test-profile"},
                }
            )
        if self.path == "/api/status":
            return self._json(
                {
                    "database": {"documents": 0, "chunks": 0},
                    "sources": {"files": 0, "source_dir": "", "manifests": 0, "reports": 0, "latest_files": []},
                    "ledger": {"by_status": {}},
                    "jobs": [],
                    "crawler_progress": {},
                    "agenttest_runs": [],
                    "memory": {"events": 0},
                }
            )
        if self.path == "/api/jobs":
            return self._json({"jobs": []})
        return super().do_GET()

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002, ANN002
        return


def assert_equal(name: str, actual, expected) -> None:  # noqa: ANN001
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_agent_action_timeline_survives_message_rerender() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FrontendHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector("#agentActionTimeline", timeout=10000)
            result = page.evaluate(
                """
                () => {
                  const sessionId = "frontend-action-timeline-test";
                  state.sessionsByAgent = {
                    mcagent_rag: [{
                      id: sessionId,
                      name: "Action Timeline Test",
                      agent: "mcagent_rag",
                      messages: [{
                        role: "assistant",
                        agent: "MCagent",
                        time: Date.now(),
                        text: "",
                        processLog: [
                          "我收到你的问题。",
                          "我开始检索本地资料库。",
                          "我筛好了证据，准备组织回答。"
                        ]
                      }]
                    }]
                  };
                  state.activeSessionByAgent = { mcagent_rag: sessionId };
                  state.activeAgent = "mcagent_rag";
                  state.actionTimeline = [];
                  state.actionTimelineSessionId = "";
                  renderMessages();
                  const firstCount = document.querySelectorAll("#agentActionTimeline .agent-action-row").length;
                  const processPanelsAfterFirstRender = document.querySelectorAll(".message .process-panel").length;
                  renderMessages();
                  const secondCount = document.querySelectorAll("#agentActionTimeline .agent-action-row").length;
                  const processPanelsAfterSecondRender = document.querySelectorAll(".message .process-panel").length;
                  const texts = [...document.querySelectorAll("#agentActionTimeline .agent-action-text")]
                    .map((item) => item.textContent.trim());
                  return { firstCount, secondCount, processPanelsAfterFirstRender, processPanelsAfterSecondRender, texts };
                }
                """
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    assert_equal("first_render_action_count", result["firstCount"], 3)
    assert_equal("second_render_action_count", result["secondCount"], 3)
    assert_equal("process_panel_hidden_after_first_render", result["processPanelsAfterFirstRender"], 0)
    assert_equal("process_panel_hidden_after_second_render", result["processPanelsAfterSecondRender"], 0)
    assert_equal(
        "action_texts",
        result["texts"],
        ["我收到你的问题。", "我开始检索本地资料库。", "我筛好了证据，准备组织回答。"],
    )


def test_agent_action_timeline_resets_for_each_turn() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FrontendHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector("#agentActionTimeline", timeout=10000)
            result = page.evaluate(
                """
                () => {
                  resetAgentActionsForTurn("session-a", "turn-1", true);
                  recordAgentAction({ actor: "MCagent", text: "first turn step", messageKey: "turn-1" });
                  const firstCount = document.querySelectorAll("#agentActionTimeline .agent-action-row").length;
                  resetAgentActionsForTurn("session-a", "turn-2", true);
                  recordAgentAction({ actor: "MCagent", text: "second turn step", messageKey: "turn-2" });
                  const rows = [...document.querySelectorAll("#agentActionTimeline .agent-action-text")]
                    .map((item) => item.textContent.trim());
                  return {
                    firstCount,
                    secondCount: rows.length,
                    rows,
                    sessionId: state.actionTimelineSessionId,
                    turnId: state.actionTimelineTurnId
                  };
                }
                """
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    assert_equal("first_turn_count", result["firstCount"], 1)
    assert_equal("second_turn_count", result["secondCount"], 1)
    assert_equal("second_turn_rows", result["rows"], ["second turn step"])
    assert_equal("timeline_session", result["sessionId"], "session-a")
    assert_equal("timeline_turn", result["turnId"], "turn-2")


def main() -> int:
    test_agent_action_timeline_survives_message_rerender()
    test_agent_action_timeline_resets_for_each_turn()
    print("FRONTEND ACTION TIMELINE SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
