from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.agent_executor import AgentToolExecutor


class FakeRun:
    def __init__(self, *, streaming: bool = False) -> None:
        self.config = {"name": "test"}
        self.original_question = "你好"
        self.question = "你好"
        self.agent = "mcagent_rag"
        self.model = "fake-model"
        self.temperature = 0.2
        self.max_tokens = 256
        self.is_streaming = streaming
        self.trace: list[dict[str, Any]] = []
        self.deltas: list[str] = []

    def add_trace(self, stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        item = {"stage": stage, "status": status, "detail": detail}
        self.trace.append(item)
        return item

    def emit_delta(self, text: str) -> None:
        self.deltas.append(text)

    def response(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        payload["trace"] = list(self.trace)
        return payload


def make_executor(
    *,
    direct_answer: str = "你好！",
    grounded_answer: tuple[str, str] = ("资料回答", "context"),
    status_payload: dict[str, Any] | None = None,
    fail_direct: bool = False,
    fail_grounded: bool = False,
) -> AgentToolExecutor:
    def generate_direct_answer(config, original_question, question, session_summary, model, temperature, max_tokens, agent):
        assert original_question == "你好"
        assert question == "你好"
        assert model == "fake-model"
        assert agent == "mcagent_rag"
        if fail_direct:
            raise RuntimeError("broken model")
        return direct_answer

    def generate_direct_answer_stream(
        config,
        original_question,
        question,
        session_summary,
        model,
        temperature,
        max_tokens,
        emit_delta,
        *,
        emit_thinking=None,
        agent="mcagent_rag",
    ):
        if fail_direct:
            raise RuntimeError("broken stream")
        assert agent == "mcagent_rag"
        if emit_thinking:
            emit_thinking({"reasoning_events": 1})
        emit_delta("你")
        emit_delta("好")
        return "你好"

    def generate_grounded_answer(
        config,
        answer_question,
        selected,
        model,
        temperature,
        max_tokens,
        *,
        retrieval_note="",
        evidence_question="",
    ):
        assert answer_question == "资料问题"
        assert evidence_question == "证据问题"
        if fail_grounded:
            raise RuntimeError("grounded broken")
        return grounded_answer

    def generate_grounded_answer_stream(
        config,
        answer_question,
        selected,
        model,
        temperature,
        max_tokens,
        emit_delta,
        *,
        emit_thinking=None,
        retrieval_note="",
        evidence_question="",
    ):
        if fail_grounded:
            raise RuntimeError("grounded stream broken")
        if emit_thinking:
            emit_thinking({"reasoning_events": 2})
        emit_delta("资")
        emit_delta("料")
        return "资料", "stream-context"

    def repair_answer(repair_question, answer, selected):
        return answer + f" repaired:{repair_question}:{len(selected)}"

    def status_answer(config):
        return status_payload or {"answer": "采集监控摘要", "sources": [], "agent": "mcagent_rag"}

    return AgentToolExecutor(
        generate_direct_answer=generate_direct_answer,
        generate_direct_answer_stream=generate_direct_answer_stream,
        generate_grounded_answer=generate_grounded_answer,
        generate_grounded_answer_stream=generate_grounded_answer_stream,
        repair_answer=repair_answer,
        status_answer=status_answer,
    )


def test_router_error_does_not_execute_tools() -> None:
    run = FakeRun()
    response = make_executor().router_error(run, {"error": "planner down"})
    assert "工具选择模型调用失败" in response["answer"]
    assert "没有执行本地检索" in response["answer"]
    assert response["sources"] == []
    assert run.trace[-1]["stage"] == "done"
    assert run.trace[-1]["status"] == "router_error"


def test_direct_answer_non_streaming() -> None:
    run = FakeRun()
    response = make_executor(direct_answer="直接回答").direct_answer(run, session_summary={"turn_count": 1})
    assert response["answer"] == "直接回答"
    assert response["trace"][0]["stage"] == "answer"
    assert response["trace"][0]["detail"]["mode"] == "direct"


def test_direct_answer_streaming_emits_delta_and_thinking_trace() -> None:
    run = FakeRun(streaming=True)
    response = make_executor().direct_answer(run, session_summary={}, mode="direct_after_retrieval_cancelled")
    assert response["answer"] == "你好"
    assert run.deltas == ["你", "好"]
    assert any(item["status"] == "thinking" for item in response["trace"])
    assert response["trace"][0]["detail"]["mode"] == "direct_after_retrieval_cancelled"


def test_direct_answer_failure_is_visible_model_failure() -> None:
    run = FakeRun()
    response = make_executor(fail_direct=True).direct_answer(run, session_summary={})
    assert response["answer"].startswith("模型调用失败：")
    assert response["sources"] == []


def test_status_returns_monitor_payload() -> None:
    run = FakeRun()
    response = make_executor(status_payload={"answer": "状态正常", "sources": [], "agent": "mcagent_rag"}).status(run)
    assert response["answer"] == "状态正常"
    assert response["agent"] == "mcagent_rag"


def test_retriever_only_answer_is_not_model_answer() -> None:
    answer, context = make_executor().retriever_only_answer("ctx")
    assert answer == "本地检索结果如下，未调用模型：\n\nctx"
    assert context == "ctx"


def test_grounded_answer_non_streaming_repairs_answer() -> None:
    run = FakeRun()
    answer, context = make_executor(grounded_answer=("基于资料回答", "ctx")).grounded_answer(
        run,
        answer_question="资料问题",
        selected=["S1"],
        retrieval_note="note",
        evidence_question="证据问题",
        repair_question="原问题",
    )
    assert answer == "基于资料回答 repaired:原问题:1"
    assert context == "ctx"
    assert run.trace[0]["stage"] == "answer"
    assert run.trace[0]["status"] == "generating"


def test_grounded_answer_streaming_emits_delta_and_thinking_trace() -> None:
    run = FakeRun(streaming=True)
    answer, context = make_executor().grounded_answer(
        run,
        answer_question="资料问题",
        selected=["S1", "S2"],
        retrieval_note="note",
        evidence_question="证据问题",
        repair_question="原问题",
    )
    assert answer == "资料 repaired:原问题:2"
    assert context == "stream-context"
    assert run.deltas == ["资", "料"]
    assert any(item["status"] == "thinking" for item in run.trace)


def test_grounded_answer_failure_is_visible_model_failure() -> None:
    run = FakeRun()
    answer, context = make_executor(fail_grounded=True).grounded_answer(
        run,
        answer_question="资料问题",
        selected=[],
        retrieval_note="note",
        evidence_question="证据问题",
        repair_question="原问题",
    )
    assert answer == "模型调用失败：grounded broken"
    assert context == ""


def main() -> int:
    test_router_error_does_not_execute_tools()
    test_direct_answer_non_streaming()
    test_direct_answer_streaming_emits_delta_and_thinking_trace()
    test_direct_answer_failure_is_visible_model_failure()
    test_status_returns_monitor_payload()
    test_retriever_only_answer_is_not_model_answer()
    test_grounded_answer_non_streaming_repairs_answer()
    test_grounded_answer_streaming_emits_delta_and_thinking_trace()
    test_grounded_answer_failure_is_visible_model_failure()
    print("agent_executor_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
