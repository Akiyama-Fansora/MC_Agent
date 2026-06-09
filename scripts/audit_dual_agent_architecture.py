from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def contains(path: Path, *needles: str) -> bool:
    text = read_text(path)
    return all(needle in text for needle in needles)


def audit(root: Path = ROOT) -> dict[str, Any]:
    graphs = root / "mcagent" / "graphs"
    files = {
        "conversation_graph": graphs / "runtime.py",
        "mcagent_graph": graphs / "mcagent.py",
        "crawler_graph": graphs / "crawler.py",
        "agent_message": root / "mcagent" / "agent_message.py",
        "agent_runtime": root / "mcagent" / "agent_runtime.py",
        "crawler_capabilities": root / "mcagent" / "crawler_capabilities.py",
        "web_server": root / "mcagent" / "web_server.py",
    }
    checks = [
        {
            "id": "agent_message_triplet",
            "status": "pass"
            if contains(files["agent_message"], "class AgentMessage", "from_agent", "content", "to_agent")
            else "fail",
            "evidence": str(files["agent_message"].relative_to(root)),
        },
        {
            "id": "conversation_routes_by_to_agent",
            "status": "pass"
            if contains(files["conversation_graph"], "message.to_agent_id", "run_mcagent_graph", "run_crawler_graph")
            else "fail",
            "evidence": str(files["conversation_graph"].relative_to(root)),
        },
        {
            "id": "mcagent_subgraph_exists",
            "status": "pass"
            if contains(files["mcagent_graph"], "build_mcagent_graph", 'payload["agent"] = "mcagent_rag"', "blocked_capability_groups")
            else "fail",
            "evidence": str(files["mcagent_graph"].relative_to(root)),
        },
        {
            "id": "crawler_subgraph_exists",
            "status": "pass"
            if contains(files["crawler_graph"], "build_crawler_graph", 'payload["agent"] = "crawler_agent"', "candidate_domain_toolsets")
            else "fail",
            "evidence": str(files["crawler_graph"].relative_to(root)),
        },
        {
            "id": "mcagent_blocks_public_web_tools",
            "status": "pass"
            if contains(files["mcagent_graph"], "web_search", "browser", "download", "public_web_ingest")
            else "fail",
            "evidence": str(files["mcagent_graph"].relative_to(root)),
        },
        {
            "id": "crawler_general_and_domain_tools_split",
            "status": "pass"
            if contains(files["agent_runtime"], "CRAWLER_GENERAL_COLLECTION_TOOL_NAMES", "CRAWLER_DOMAIN_COLLECTION_TOOL_NAMES")
            and contains(files["crawler_capabilities"], "GENERAL_TOOL_GROUPS", "DOMAIN_TOOL_GROUPS")
            else "fail",
            "evidence": f"{files['agent_runtime'].relative_to(root)}; {files['crawler_capabilities'].relative_to(root)}",
        },
        {
            "id": "legacy_runtime_coupling_visible",
            "status": "warn"
            if contains(files["mcagent_graph"], "legacy MCagent runtime")
            or contains(files["crawler_graph"], "legacy CrawlerAgent runtime")
            or contains(files["web_server"], "def _chat_impl")
            else "pass",
            "evidence": "Graph subnodes still delegate core execution to the legacy web_server runtime during migration.",
        },
    ]
    counts = {status: sum(1 for item in checks if item["status"] == status) for status in ("pass", "warn", "fail")}
    return {
        "subject": "dual_agent_architecture",
        "summary": {
            "is_two_agent_shaped": counts["fail"] == 0,
            "pass": counts["pass"],
            "warn": counts["warn"],
            "fail": counts["fail"],
        },
        "checks": checks,
        "notes": [
            "This audit reports objective structure only.",
            "It does not decide whether a live LLM turn should call a tool or whether evidence is sufficient.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit objective dual-agent architecture facts.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()
    report = audit(ROOT)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(
            f"Dual-agent shaped: {summary['is_two_agent_shaped']} "
            f"(pass={summary['pass']} warn={summary['warn']} fail={summary['fail']})"
        )
        for item in report["checks"]:
            print(f"[{item['status']}] {item['id']}: {item['evidence']}")
    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
