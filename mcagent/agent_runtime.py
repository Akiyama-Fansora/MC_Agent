from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Iterable


LLM_OWNERSHIP_PRINCIPLES = [
    "LLM owns interpretation, tool choice, reflection, and final answer wording.",
    "Tools provide objective observations only; tools must not invent final answers or subjective decisions.",
    "Agent behavior must be general. Do not encode one-off user test phrases as routing rules.",
    "Every non-trivial action should be observable as: observe -> deliberate -> choose_action -> preflight -> execute_tool -> observe_result -> reflect -> continue_or_finish.",
    "If a task is delegated between agents, pass caller, target, known context, acceptance criteria, delivery target, and failure-reporting expectations.",
]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    side_effects: str = "none"
    terminal: bool = False
    llm_final_answer_required: bool = True

    def to_prompt_line(self) -> str:
        effect = f" side_effects={self.side_effects}" if self.side_effects else ""
        final = " final_by_llm" if self.llm_final_answer_required else " objective_tool_result"
        return f"- {self.name}: {self.description}{effect};{final}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "result_schema": self.result_schema,
            "side_effects": self.side_effects,
            "terminal": self.terminal,
            "llm_final_answer_required": self.llm_final_answer_required,
        }


@dataclass(frozen=True, slots=True)
class AgentRole:
    agent_id: str
    display_name: str
    responsibility: str
    relationship: str

    def to_prompt_text(self) -> str:
        return (
            f"{self.display_name} ({self.agent_id}): {self.responsibility} "
            f"Relationship: {self.relationship}"
        )


@dataclass(slots=True)
class AgentAction:
    action: str
    goal: str
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    next_step_risk: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "tool_name": self.tool_name,
            "goal": self.goal,
            "arguments": self.arguments,
            "reason": self.reason,
            "next_step_risk": self.next_step_risk,
        }


@dataclass(frozen=True, slots=True)
class HandoffContract:
    requested_by: str
    from_agent: str
    to_agent: str
    user_request: str
    task_goal: str
    delivery_target: str
    known_context: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    failure_report: str = "If blocked, report exact failed sources/tools, reason, and next viable path."

    def to_prompt_text(self) -> str:
        criteria = "\n".join(f"- {item}" for item in self.acceptance_criteria if item)
        return (
            f"Requested by: {self.requested_by}\n"
            f"From: {self.from_agent}\n"
            f"To: {self.to_agent}\n"
            f"Original user request: {self.user_request}\n"
            f"Task goal: {self.task_goal}\n"
            f"Delivery target: {self.delivery_target}\n"
            f"Known context: {self.known_context or 'none'}\n"
            f"Acceptance criteria:\n{criteria or '- satisfy the task goal with citeable objective evidence'}\n"
            f"Failure reporting: {self.failure_report}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_by": self.requested_by,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "user_request": self.user_request,
            "task_goal": self.task_goal,
            "delivery_target": self.delivery_target,
            "known_context": self.known_context,
            "acceptance_criteria": self.acceptance_criteria,
            "failure_report": self.failure_report,
        }


AGENT_ROLES = {
    "mcagent_rag": AgentRole(
        agent_id="mcagent_rag",
        display_name="MCagent",
        responsibility=(
            "Talks with the user first-hand, understands conversation context, "
            "uses local/RAG/status/delegation tools when useful, and writes final answers."
        ),
        relationship="May ask CrawlerAgent for missing evidence; must not turn every message into retrieval.",
    ),
    "crawler_agent": AgentRole(
        agent_id="crawler_agent",
        display_name="CrawlerAgent",
        responsibility=(
            "Collects, verifies, saves, and prepares web/local data. It can serve the user directly "
            "or prepare data for MCagent/RAG depending on the delivery target."
        ),
        relationship="Receives direct human tasks or MCagent handoffs, then plans its own collection loop.",
    ),
    "retriever_only": AgentRole(
        agent_id="retriever_only",
        display_name="Retriever-only mode",
        responsibility="Returns objective local retrieval results without LLM final-answer ownership.",
        relationship="Mode, not a third conversational agent.",
    ),
}


MCAGENT_TOOLS = [
    ToolSpec(
        name="direct_answer",
        description="Answer directly with the LLM when no external tool is needed.",
        input_schema={"question": "original user message", "session_summary": "conversation memory"},
        result_schema={"answer": "LLM-written final answer"},
        terminal=True,
    ),
    ToolSpec(
        name="local_rag_search",
        description="Search local documents, chunks, raw HTML, and manifests for evidence.",
        input_schema={"question": "topic-focused search question"},
        result_schema={"sources": "ranked citeable evidence", "context": "formatted evidence"},
        side_effects="read_local_index",
        llm_final_answer_required=True,
    ),
    ToolSpec(
        name="evidence_select",
        description="Select and validate whether retrieved evidence is enough for the user's question.",
        input_schema={"question": "evidence question", "candidates": "retrieval results"},
        result_schema={"verdict": "ok|insufficient", "reasons": "evidence gaps"},
        side_effects="none",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="final_answer_llm",
        description="Write the user-facing answer from selected evidence and conversation context.",
        input_schema={"question": "user question", "selected_sources": "evidence"},
        result_schema={"answer": "LLM-written grounded response"},
        terminal=True,
    ),
    ToolSpec(
        name="status",
        description="Read crawler/import/background job status when the user asks about system progress.",
        input_schema={"scope": "optional status focus"},
        result_schema={"status": "human-readable current system state"},
        side_effects="read_runtime_state",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="delegate_crawler",
        description="Send a natural-language data collection task to CrawlerAgent with context and delivery target.",
        input_schema={"handoff_contract": "caller, goal, context, acceptance criteria"},
        result_schema={"job_id": "crawler job", "handoff": "agent-to-agent message"},
        side_effects="start_background_job",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="planned_workflow",
        description="Create a short tool plan for compound requests, then execute steps with LLM checkpoints.",
        input_schema={"goal": "compound user goal", "steps": "candidate tool steps"},
        result_schema={"plan": "observable action plan"},
        side_effects="depends_on_steps",
        llm_final_answer_required=True,
    ),
]


CRAWLER_ROUTE_TOOLS = [
    ToolSpec(
        name="direct_answer",
        description="Explain CrawlerAgent capabilities, limitations, or simple non-collection replies with the LLM.",
        input_schema={"question": "original user message", "session_summary": "conversation memory"},
        result_schema={"answer": "LLM-written response"},
        terminal=True,
    ),
    ToolSpec(
        name="status",
        description="Read current crawler/import/background job status.",
        input_schema={"scope": "optional status focus"},
        result_schema={"status": "current crawler state"},
        side_effects="read_runtime_state",
        terminal=True,
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="delegate_crawler",
        description="Accept a human or MCagent collection request and start CrawlerAgent's collection loop.",
        input_schema={"collection_target": "natural-language goal", "delivery_target": "human|MCagent/RAG|both"},
        result_schema={"job_id": "crawler job", "handoff": "collection contract"},
        side_effects="start_background_job",
        terminal=True,
        llm_final_answer_required=False,
    ),
]


CRAWLER_COLLECTION_TOOLS = [
    ToolSpec(
        name="plan_collection",
        description="Understand the collection goal, delivery target, coverage goals, and next tool actions.",
        input_schema={"handoff_or_user_goal": "collection goal with context"},
        result_schema={"tasks": "ordered executable crawler tasks"},
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="mcmod",
        description="Search and scrape MC百科 pages, preserving markdown, manifest, and raw HTML when available.",
        input_schema={"query": "short source-specific query"},
        result_schema={"records": "saved pages", "manifest": "metadata"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modrinth",
        description="Search Modrinth projects and project contents.",
        input_schema={"query": "short project query"},
        result_schema={"records": "project/content markdown"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="browser_collect",
        description="Use a browser for general structured collection, saving CSV/JSON/report/raw HTML/screenshot.",
        input_schema={"query_or_url": "target", "fields": "requested fields", "output_dir": "save path"},
        result_schema={"files": "saved structured outputs", "failure_reason": "if blocked"},
        side_effects="browser_network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="playwright",
        description="Use a local browser to render/search pages, preserve text and raw HTML, and diagnose JS-heavy pages.",
        input_schema={"query": "short search or URL task"},
        result_schema={"records": "rendered page evidence"},
        side_effects="browser_network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modpack_download",
        description="Discover and download public .mrpack/.zip modpack archives when available.",
        input_schema={"query": "project/download query"},
        result_schema={"downloads": "archive files or failure reason"},
        side_effects="network_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="modpack_internal",
        description="Parse a real local modpack archive for manifest, modlist, quests, KubeJS, recipes, configs, and text.",
        input_schema={"archive_or_query": "downloaded/provided archive"},
        result_schema={"records": "internal files converted for RAG"},
        side_effects="filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="web_discovery",
        description="Discover public web pages and candidate URLs from broad search.",
        input_schema={"query": "short discovery query"},
        result_schema={"candidates": "URLs and snippets"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="tavily",
        description="Use Tavily search/extract when configured and quota allows.",
        input_schema={"query": "short web query"},
        result_schema={"records": "search/extract markdown"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="firecrawl",
        description="Use Firecrawl search/scrape when configured and quota allows.",
        input_schema={"query_or_url": "search query or URL"},
        result_schema={"records": "clean markdown/scrape results"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="jina",
        description="Use Jina Reader/Search as a no-key fallback for page text extraction/search.",
        input_schema={"query_or_url": "search query or URL"},
        result_schema={"records": "reader/search markdown"},
        side_effects="network_and_filesystem",
        llm_final_answer_required=False,
    ),
    ToolSpec(
        name="finish",
        description="Stop the collection loop when enough useful data or a clear blocker report exists.",
        result_schema={"done_summary": "coverage and gaps"},
        terminal=True,
        llm_final_answer_required=True,
    ),
]


def tools_for_agent(agent_id: str) -> list[ToolSpec]:
    if agent_id == "crawler_agent":
        return list(CRAWLER_ROUTE_TOOLS)
    if agent_id == "retriever_only":
        return [tool for tool in MCAGENT_TOOLS if tool.name == "local_rag_search"]
    return list(MCAGENT_TOOLS)


def collection_tools_for_crawler() -> list[ToolSpec]:
    return list(CRAWLER_COLLECTION_TOOLS)


def tool_names_for_agent(agent_id: str) -> list[str]:
    return [tool.name for tool in tools_for_agent(agent_id)]


def tool_catalog_prompt(agent_id: str, *, include_principles: bool = True) -> str:
    role = AGENT_ROLES.get(agent_id, AGENT_ROLES["mcagent_rag"])
    lines: list[str] = [role.to_prompt_text(), "Available tools:"]
    lines.extend(tool.to_prompt_line() for tool in tools_for_agent(agent_id))
    if include_principles:
        lines.append("Runtime principles:")
        lines.extend(f"- {item}" for item in LLM_OWNERSHIP_PRINCIPLES)
    return "\n".join(lines)


def tool_catalog_json(agent_id: str) -> str:
    return json.dumps([tool.to_dict() for tool in tools_for_agent(agent_id)], ensure_ascii=False)


def crawler_collection_catalog_prompt(*, include_principles: bool = True) -> str:
    role = AGENT_ROLES["crawler_agent"]
    lines: list[str] = [role.to_prompt_text(), "Collection tools:"]
    lines.extend(tool.to_prompt_line() for tool in collection_tools_for_crawler())
    if include_principles:
        lines.append("Runtime principles:")
        lines.extend(f"- {item}" for item in LLM_OWNERSHIP_PRINCIPLES)
    return "\n".join(lines)


def validate_tool_name(agent_id: str, name: str, *, fallback: str = "direct_answer") -> str:
    names = set(tool_names_for_agent(agent_id))
    if name in names:
        return name
    if fallback in names:
        return fallback
    return next(iter(names), fallback)


def build_handoff_contract(
    *,
    requested_by: str,
    from_agent: str,
    to_agent: str,
    user_request: str,
    task_goal: str,
    delivery_target: str,
    known_context: str = "",
    acceptance_criteria: Iterable[str] = (),
) -> HandoffContract:
    return HandoffContract(
        requested_by=requested_by,
        from_agent=from_agent,
        to_agent=to_agent,
        user_request=user_request,
        task_goal=task_goal,
        delivery_target=delivery_target,
        known_context=known_context,
        acceptance_criteria=[str(item).strip() for item in acceptance_criteria if str(item).strip()],
    )
