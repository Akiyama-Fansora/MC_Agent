from __future__ import annotations

from typing import Any, Callable, Protocol


class ExecutableRun(Protocol):
    config: Any
    original_question: str
    question: str
    agent: str
    model: str
    temperature: float
    max_tokens: int | None
    is_streaming: bool

    def add_trace(self, stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        ...

    def emit_delta(self, text: str) -> None:
        ...

    def response(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


DirectAnswerFn = Callable[[Any, str, str, dict[str, Any], str, float, int | None, str], str]
DirectAnswerStreamFn = Callable[..., str]
GroundedAnswerFn = Callable[..., tuple[str, str]]
GroundedAnswerStreamFn = Callable[..., tuple[str, str]]
RepairAnswerFn = Callable[[str, str, list[Any]], str]
StatusAnswerFn = Callable[[Any], dict[str, Any]]


class AgentToolExecutor:
    """Run an already selected tool without deciding which tool should run."""

    def __init__(
        self,
        *,
        generate_direct_answer: DirectAnswerFn,
        generate_direct_answer_stream: DirectAnswerStreamFn,
        generate_grounded_answer: GroundedAnswerFn | None = None,
        generate_grounded_answer_stream: GroundedAnswerStreamFn | None = None,
        repair_answer: RepairAnswerFn | None = None,
        status_answer: StatusAnswerFn,
    ) -> None:
        self._generate_direct_answer = generate_direct_answer
        self._generate_direct_answer_stream = generate_direct_answer_stream
        self._generate_grounded_answer = generate_grounded_answer
        self._generate_grounded_answer_stream = generate_grounded_answer_stream
        self._repair_answer = repair_answer
        self._status_answer = status_answer

    def router_error(self, run: ExecutableRun, tool_decision: dict[str, Any]) -> dict[str, Any]:
        error_text = str(tool_decision.get("error") or tool_decision.get("reason") or "unknown error")
        run.add_trace("done", "router_error", {"error": error_text, "delegated": False})
        return run.response(
            {
                "answer": f"Agent 工具选择模型调用失败：{error_text}\n\n本次没有执行本地检索，也没有启动 Crawler。请检查当前模型配置或稍后重试。",
                "sources": [],
                "context": "",
                "agent": run.agent,
            }
        )

    def direct_answer(self, run: ExecutableRun, *, session_summary: dict[str, Any], mode: str = "direct") -> dict[str, Any]:
        run.add_trace("answer", "generating", {"model": run.model, "mode": mode})
        try:
            if run.is_streaming:
                answer = self._generate_direct_answer_stream(
                    run.config,
                    run.original_question,
                    run.question,
                    session_summary,
                    run.model,
                    run.temperature,
                    run.max_tokens,
                    run.emit_delta,
                    emit_thinking=lambda detail: run.add_trace("answer", "thinking", detail),
                    agent=run.agent,
                )
            else:
                answer = self._generate_direct_answer(
                    run.config,
                    run.original_question,
                    run.question,
                    session_summary,
                    run.model,
                    run.temperature,
                    run.max_tokens,
                    run.agent,
                )
        except Exception as exc:  # noqa: BLE001 - model failures must be visible as model failures.
            answer = f"模型调用失败：{exc}"
        return run.response({"answer": answer, "sources": [], "context": "", "agent": run.agent})

    def status(self, run: ExecutableRun) -> dict[str, Any]:
        return run.response(self._status_answer(run.config))

    def retriever_only_answer(self, context: str) -> tuple[str, str]:
        return "本地检索结果如下，未调用模型：\n\n" + context, context

    def grounded_answer(
        self,
        run: ExecutableRun,
        *,
        answer_question: str,
        selected: list[Any],
        retrieval_note: str,
        evidence_question: str,
        repair_question: str,
    ) -> tuple[str, str]:
        if self._generate_grounded_answer is None or self._generate_grounded_answer_stream is None:
            raise RuntimeError("grounded answer generator is not configured")

        run.add_trace("answer", "generating", {"model": run.model})
        failed = False
        try:
            if run.is_streaming:
                answer, context = self._generate_grounded_answer_stream(
                    run.config,
                    answer_question,
                    selected,
                    run.model,
                    run.temperature,
                    run.max_tokens,
                    run.emit_delta,
                    emit_thinking=lambda detail: run.add_trace("answer", "thinking", detail),
                    retrieval_note=retrieval_note,
                    evidence_question=evidence_question,
                )
            else:
                answer, context = self._generate_grounded_answer(
                    run.config,
                    answer_question,
                    selected,
                    run.model,
                    run.temperature,
                    run.max_tokens,
                    retrieval_note=retrieval_note,
                    evidence_question=evidence_question,
                )
        except Exception as exc:  # noqa: BLE001 - final answer model failures must stay visible.
            failed = True
            answer = f"模型调用失败：{exc}"
            context = ""

        if self._repair_answer is not None and not failed:
            answer = self._repair_answer(repair_question, answer, selected)
        return answer, context
