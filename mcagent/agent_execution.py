from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable

from .agent_message import agent_reply_message_from_payload
from .config import AppConfig
from .llm_profiles import profiles_payload, resolve_profile_from_model


EmitFn = Callable[[str, Any], None]
TokenResolver = Callable[[dict[str, Any], str], int | None]


@dataclass(slots=True)
class AgentTraceRecorder:
    emit: EmitFn | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        step = {
            "stage": stage,
            "status": status,
            "detail": detail if detail is not None else {},
            "time": time.time(),
        }
        self.steps.append(step)
        if self.emit is not None:
            self.emit("trace", step)
        return step

    def response(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["trace"] = self.steps
        return payload

    def delta(self, text: str) -> None:
        if self.emit is not None:
            self.emit("delta", {"text": text})


@dataclass(slots=True)
class AgentExecutionContext:
    config: AppConfig
    payload: dict[str, Any]
    original_question: str
    question: str
    agent: str
    model: str
    temperature: float
    max_tokens: int | None
    trace: AgentTraceRecorder

    @property
    def is_streaming(self) -> bool:
        return self.trace.emit is not None

    def add_trace(self, stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        return self.trace.add(stage, status, detail)

    def response(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "agent_message" not in payload and str(payload.get("answer") or "").strip():
            payload["agent_message"] = agent_reply_message_from_payload(
                self.payload,
                from_agent_id=str(payload.get("agent") or self.agent),
                content=str(payload.get("answer") or ""),
            ).to_dict()
        return self.trace.response(payload)

    def emit_delta(self, text: str) -> None:
        self.trace.delta(text)


def resolve_agent_model(config: AppConfig, payload: dict[str, Any], agent: str) -> str:
    profile_id = str(payload.get("model_profile_id") or "").strip()
    if profile_id:
        return f"profile:{profile_id}"
    raw_model = str(payload.get("model") or "").strip()
    if raw_model.lower() in {"auto", "default", "assigned", "agent-default"}:
        raw_model = ""
    if raw_model:
        profile = resolve_profile_from_model(config, raw_model, agent=agent)
        if profile:
            return f"profile:{profile['id']}"
        return raw_model
    assignment_key = "crawler_agent" if agent == "crawler_agent" else "mcagent_rag"
    assigned = profiles_payload(config).get("assignments", {}).get(assignment_key, "")
    return f"profile:{assigned}" if assigned else config.ollama.model


def build_agent_execution_context(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    token_resolver: TokenResolver,
    emit: EmitFn | None = None,
) -> AgentExecutionContext:
    original_question = str(payload.get("question") or payload.get("query") or "").strip()
    agent = str(payload.get("agent") or "mcagent_rag")
    model = resolve_agent_model(config, payload, agent)
    temperature = float(payload.get("temperature") if payload.get("temperature") is not None else config.ollama.temperature)
    max_tokens = token_resolver(payload, original_question)
    trace = AgentTraceRecorder(emit=emit)
    context = AgentExecutionContext(
        config=config,
        payload=payload,
        original_question=original_question,
        question=original_question,
        agent=agent,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        trace=trace,
    )
    context.add_trace("observe", "received", {"agent": agent, "question": original_question})
    return context
