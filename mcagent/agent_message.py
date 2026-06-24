from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any


AGENT_ALIASES = {
    "user": "User",
    "human": "User",
    "mcagent": "MCagent",
    "mcagent_rag": "MCagent",
    "mc agent": "MCagent",
    "mca": "MCagent",
    "crawler": "CrawlerAgent",
    "crawleragent": "CrawlerAgent",
    "crawler_agent": "CrawlerAgent",
    "crawler agent": "CrawlerAgent",
}

AGENT_IDS = {
    "User": "user",
    "MCagent": "mcagent_rag",
    "CrawlerAgent": "crawler_agent",
}

AGENT_ID_NAMES = {value: key for key, value in AGENT_IDS.items()}


def coerce_message_bool(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "default"}:
            return default
        if text in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "f", "no", "n", "off"}:
            return False
    return bool(value)


def normalize_agent_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "User"
    return AGENT_ALIASES.get(text.lower(), text)


def agent_id_for_name(value: str) -> str:
    return AGENT_IDS.get(normalize_agent_name(value), str(value or "").strip())


def display_name_for_agent_id(value: str) -> str:
    agent_id = str(value or "").strip()
    return AGENT_ID_NAMES.get(agent_id, normalize_agent_name(agent_id))


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """A single message passed between User, MCagent, and CrawlerAgent.

    The message bus is deliberately small: it records who said what to whom.
    Tool routing and next-step decisions still belong to the receiving Agent.
    """

    from_agent: str
    content: str
    to_agent: str
    intent: str = ""
    conversation_id: str = ""
    message_id: str = ""
    reply_to: str = ""
    requires_reply: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_agent", normalize_agent_name(self.from_agent))
        object.__setattr__(self, "to_agent", normalize_agent_name(self.to_agent))
        if not self.message_id:
            seed = f"{self.created_at}:{self.from_agent}:{self.to_agent}:{self.content[:80]}"
            import hashlib

            digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
            object.__setattr__(self, "message_id", f"msg_{digest}")

    @property
    def from_agent_id(self) -> str:
        return agent_id_for_name(self.from_agent)

    @property
    def to_agent_id(self) -> str:
        return agent_id_for_name(self.to_agent)

    def to_tuple(self) -> tuple[str, str, str]:
        return (self.from_agent, self.content, self.to_agent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "from_agent": self.from_agent,
            "from_agent_id": self.from_agent_id,
            "to_agent": self.to_agent,
            "to_agent_id": self.to_agent_id,
            "content": self.content,
            "intent": self.intent,
            "conversation_id": self.conversation_id,
            "reply_to": self.reply_to,
            "requires_reply": self.requires_reply,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "tuple": list(self.to_tuple()),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def make_agent_message(
    from_agent: str,
    content: str,
    to_agent: str,
    *,
    intent: str = "",
    conversation_id: str = "",
    reply_to: str = "",
    requires_reply: bool = True,
    metadata: dict[str, Any] | None = None,
) -> AgentMessage:
    return AgentMessage(
        from_agent=from_agent,
        content=str(content or ""),
        to_agent=to_agent,
        intent=intent,
        conversation_id=conversation_id,
        reply_to=reply_to,
        requires_reply=requires_reply,
        metadata=dict(metadata or {}),
    )


def message_from_payload(payload: dict[str, Any], *, default_to_agent: str, default_content: str) -> AgentMessage:
    raw = payload.get("agent_message")
    if isinstance(raw, AgentMessage):
        return raw
    if isinstance(raw, dict):
        return make_agent_message(
            str(raw.get("from_agent") or raw.get("from") or payload.get("message_from") or "User"),
            str(raw.get("content") or raw.get("message") or default_content),
            str(raw.get("to_agent") or raw.get("to") or default_to_agent),
            intent=str(raw.get("intent") or payload.get("intent") or ""),
            conversation_id=str(raw.get("conversation_id") or payload.get("session_id") or ""),
            reply_to=str(raw.get("reply_to") or ""),
            requires_reply=coerce_message_bool(raw.get("requires_reply"), default=True),
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        )
    return make_agent_message(
        str(payload.get("message_from") or "User"),
        default_content,
        default_to_agent,
        intent=str(payload.get("intent") or ""),
        conversation_id=str(payload.get("session_id") or ""),
        metadata={"source": "chat_payload"},
    )


def agent_reply_message_from_payload(payload: dict[str, Any], *, from_agent_id: str, content: str) -> AgentMessage:
    request = message_from_payload(
        payload,
        default_to_agent=display_name_for_agent_id(from_agent_id),
        default_content=str(payload.get("question") or payload.get("query") or ""),
    )
    return make_agent_message(
        display_name_for_agent_id(from_agent_id),
        content,
        request.from_agent or "User",
        intent="agent_reply",
        conversation_id=request.conversation_id or str(payload.get("session_id") or ""),
        reply_to=request.message_id,
        requires_reply=False,
        metadata={"request_message_id": request.message_id, "response_agent_id": from_agent_id},
    )


@dataclass(slots=True)
class CrawlerTask:
    task_id: str
    question: str
    missing_evidence: str
    topic: str
    preferred_sources: list[str]
    search_queries: list[str]
    output_dir: str
    intent: dict[str, Any] = field(default_factory=dict)
    entity: str = ""
    keywords: list[str] = field(default_factory=list)
    candidate_sources: list[dict[str, Any]] = field(default_factory=list)
    output_format: str = "markdown+manifest"
    success_criteria: list[str] = field(default_factory=list)
    max_urls: int = 40
    priority: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "missing_evidence": self.missing_evidence,
            "topic": self.topic,
            "preferred_sources": self.preferred_sources,
            "search_queries": self.search_queries,
            "output_dir": self.output_dir,
            "intent": self.intent,
            "entity": self.entity,
            "keywords": self.keywords,
            "candidate_sources": self.candidate_sources,
            "output_format": self.output_format,
            "success_criteria": self.success_criteria,
            "max_urls": self.max_urls,
            "priority": self.priority,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(slots=True)
class CrawlerResult:
    task_id: str
    status: str
    export_dir: str
    manifest_path: str
    records: int
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    confidence: float = 0.0
    coverage_note: str = ""
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "export_dir": self.export_dir,
            "manifest_path": self.manifest_path,
            "records": self.records,
            "skipped": self.skipped,
            "errors": self.errors,
            "confidence": self.confidence,
            "coverage_note": self.coverage_note,
            "files": self.files,
        }


@dataclass(slots=True)
class CollaborationState:
    session_id: str
    question: str
    phase: str = "initial_query"
    round_index: int = 0
    max_rounds: int = 3
    tasks_dispatched: list[CrawlerTask] = field(default_factory=list)
    results_received: list[CrawlerResult] = field(default_factory=list)
    evidence_chain: list[dict[str, Any]] = field(default_factory=list)

    def add_event(self, event_type: str, detail: str | dict[str, Any]) -> None:
        self.evidence_chain.append(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "phase": self.phase,
                "event": event_type,
                "detail": detail,
            }
        )

    def can_continue(self) -> bool:
        return self.round_index < self.max_rounds

    def trace_text(self) -> str:
        lines: list[str] = []
        for event in self.evidence_chain:
            detail = event["detail"]
            if isinstance(detail, dict):
                detail = json.dumps(detail, ensure_ascii=False)
            lines.append(f"[{event['phase']}] {event['event']}: {detail}")
        return "\n".join(lines)
