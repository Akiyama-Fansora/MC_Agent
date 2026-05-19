from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_task(target: str, export_dir: Path, page_limit: int) -> str:
    return f"""任务：为离线 MCagent 采集少量 MC百科公开资料。

目标站点：{target}
允许域名：{target} 和同根域下公开只读页面。
导出目录：{export_dir}

要求：
- 先检查 robots.txt 或站点公开爬取边界。
- 只做低频、只读、可复盘采集，不登录、不提交、不修改站点状态。
- 不要离开 MC百科/MCMod 相关域名；如果遇到 WAF、503、403、验证码或 robots 不可读，停止并报告阻塞。
- 不要做全站深爬，本次最多采集 {page_limit} 个公开页面样本。
- 自己观察页面结构并选择合适通用工具，输出可用于问答的 Markdown/JSON/HTML 证据。
- 如果导出失败，也要报告 run 目录、已保存证据路径、阻塞原因和下一步建议。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch AgentTest for a bounded MCMod seed crawl.")
    parser.add_argument("--agenttest-root", default=r"D:\magic\AgentTest")
    parser.add_argument("--target", default="https://www.mcmod.cn/")
    parser.add_argument("--export-dir", default=str(PROJECT_ROOT / "data" / "crawler_exports"))
    parser.add_argument("--page-limit", type=int, default=5)
    parser.add_argument("--max-tool-rounds", type=int, default=8)
    args = parser.parse_args()

    agenttest_root = Path(args.agenttest_root).resolve()
    if not agenttest_root.exists():
        print(f"AgentTest root not found: {agenttest_root}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(agenttest_root))

    from agent.core import read_text, run_agent  # type: ignore
    from agent.llm import create_client, load_llm_config  # type: ignore
    from agent.paths import CONFIG_DIR, PROMPTS_DIR, TOOLS_DIR  # type: ignore
    from agent.tool_loader import load_tools  # type: ignore

    os.environ.setdefault("LLM_MAX_TOOL_ROUNDS", str(args.max_tool_rounds))
    os.environ.setdefault("LLM_MAX_RUN_SECONDS", "300")
    os.environ.setdefault("LLM_BUDGET_AWARE_STOP", "1")

    export_dir = Path(args.export_dir).resolve()
    export_dir.mkdir(parents=True, exist_ok=True)
    task = build_task(args.target, export_dir, args.page_limit)

    message_path = agenttest_root / "messages" / "mcmod_seed_task_latest.txt"
    message_path.write_text(task, encoding="utf-8")

    config = load_llm_config(CONFIG_DIR / "llm.env")
    client = create_client(config)
    system_prompt = read_text(PROMPTS_DIR / "system_prompt.txt")
    tools = load_tools(TOOLS_DIR)

    print(f"AgentTest root: {agenttest_root}")
    print(f"Task file: {message_path}")
    print(f"Loaded tools: {len(tools)}")
    print("Starting AgentTest. This may stop early if MCMod blocks automated access.")
    answer = run_agent(
        client=client,
        config=config,
        system_prompt=system_prompt,
        user_message=task,
        tools=tools,
        trace=lambda text: print(f"[trace] {text}"),
    )
    print("\nAgentTest final answer:\n")
    print(answer)
    print("\nTip: run scripts\\export_agenttest_run.py on the generated run directory if AgentTest did not export files itself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
