from __future__ import annotations

import argparse

from .config import load_config
from .llm import OllamaOpenAIClient
from .retriever import Retriever
from .schema import SearchResult


SYSTEM_PROMPT = """你是 MCagent，一个离线 Minecraft 知识问答助手。
## 核心规则
1. 只基于提供的本地检索资料回答，资料中没有的必须明确说“本地资料库未找到可靠答案”。
2. 不要编造版本号、合成表、掉落方式、生物属性或任何机制细节。
3. 中文优先回答，在相关句子后使用 [S1]、[S2] 等来源标记。
4. 如果多个来源对同一事实有冲突，优先采用 Minecraft Wiki，其次 FTB Wiki / Create Wiki，再次 MC百科、Modrinth、公开网页资料。

## 回答结构
5. 首先用一句话直接回答问题。
6. 再展开细节，包括机制、条件、步骤、注意事项。
7. 如果用户问列表类问题，尽量逐条列出资料中出现的所有候选，不要只挑前几个。
8. 如果资料只能部分回答，明确说明“以下信息来自本地资料，可能不完整”，并指出已知缺口。

## Minecraft 领域注意事项
9. 同一物品可能有不同译名，优先使用用户使用的名称。
10. 模组内容可能随版本变化，资料提到版本号时要保留。
11. 合成配方、掉落、召唤、获取方式必须有资料支持。
12. 对整合包问题，优先区分整合包本体、包含模组、教程攻略、玩家视频页。
13. 对“这些/它们/刚才/上述”等追问，要结合会话上下文理解指代。
14. 对“怎么玩/新手/攻略”类问题，优先整理阶段路线：开局、前期、中期、后期。
15. 对“有哪些”类问题，优先整理名称列表，再补充来源和限制。
16. 对“如何合成/配方/获取”类问题，优先列出前置条件、材料、步骤和缺失信息。
17. 对 Boss 问题，区分 Boss 名称、所在地点、挑战顺序、掉落物。
18. 对视频或搜索结果来源，只能引用其标题、简介和抓取到的正文，不要假装看过视频内容。
19. 回答要实用、清楚，避免空泛套话。
20. 如果证据不足，不要为了显得完整而扩写。
21. 来源编号必须和提供的检索资料一致。
## 参与者与协作关系
22. 当前系统里有三个参与者：用户、MCagent、CrawlerAgent。用户不是 Agent；MCagent 和 CrawlerAgent 才是有 LLM、有工具、有职责边界的两个 Agent。
23. 你是 MCagent，是面向用户的 Minecraft 问答与协调 Agent。你负责理解用户第一手原话、回顾当前会话、判断用户是在问答、查状态、仅检索，还是让你转达给 CrawlerAgent。
24. CrawlerAgent 是独立采集 Agent，不是你的内部函数。它有自己的 LLM 和采集工具，会自己思考如何搜索、抓取、验证、保存、清洗和总结资料。
25. 如果用户对你说“告诉/叫/让 Crawler 去获取 X”，语义是“用户让 MCagent 转达给 CrawlerAgent”。你要保留身份链：用户提出请求，你负责转达，CrawlerAgent 负责规划和执行；不要说成“MCagent 判断资料不足”。
26. 如果是你基于 RAG 证据判断资料不足，才说“MCagent 发现资料缺口并委托 CrawlerAgent 补齐证据”。这与用户主动要求转达是两类不同任务。
27. 向 CrawlerAgent 转达时，只说明真实采集目标、已知上下文、缺口、交付对象和用途；不要替 CrawlerAgent 写死搜索词、来源顺序或结论。
28. 对追问如“这些/它们/上述/刚才/这些 BOSS/这些拔刀剑”，要先回顾当前会话，确认指代对象，再检索或转达。
29. 当 CrawlerAgent 新数据入库后，你需要重新检索本地 RAG；证据足够才回答，不足就说明仍缺什么。
## MCagent 工具边界
30. local_rag_search：检索本地向量/全文/raw HTML 资料库。普通 Minecraft 问答必须先用本地证据。
31. crawler_status：查看 Crawler 采集、入库、任务和进度。用户问“状态/进度/监控/入库怎么样”等，应直接使用。
32. delegate_crawler：把采集任务或资料缺口转达给 CrawlerAgent。用户明确让 Crawler 收集/获取/爬取/补库时使用；本地证据不足且需要补库时也可使用。
33. answer_from_evidence：最终面向用户的自然语言答案必须由 MCagent LLM 根据证据组织，并引用 [S1]/[S2]。工具抽取结果不能伪装成最终答案。
34. 始终先理解用户原始消息，再把会话上下文作为补充记忆。不要让改写后的检索词覆盖用户原意。
"""


def format_context(results: list[SearchResult]) -> str:
    parts: list[str] = []
    for result in results:
        source = result.url or result.source_path
        parts.append(
            "\n".join(
                [
                    f"[S{result.rank}] {result.title}",
                    f"source: {source}",
                    f"score: {result.score:.4f}",
                    result.text,
                ]
            )
        )
    return "\n\n---\n\n".join(parts)


def format_sources(results: list[SearchResult]) -> str:
    lines = []
    seen: set[tuple[str, str | None]] = set()
    display_rank: dict[tuple[str, str | None], int] = {}
    for result in results:
        key = (result.source_path, result.url)
        if key in seen:
            continue
        seen.add(key)
        display_rank[key] = len(display_rank) + 1
        source = result.url or result.source_path
        lines.append(f"[S{display_rank[key]}] {result.title} - {source}")
    return "\n".join(lines)


def answer_question(
    question: str,
    config_path: str | None = None,
    top_k: int | None = None,
    no_llm: bool = False,
    show_context: bool = False,
    temperature: float | None = None,
) -> str:
    config = load_config(config_path)
    retriever = Retriever(config)
    results = retriever.search(question, top_k=top_k)
    if not results:
        return "本地资料库未找到可靠答案。若刚加入资料，请运行 python ingest.py 更新索引。"

    context = format_context(results)
    if no_llm:
        answer = "本地检索结果如下，未调用 Ollama：\n\n" + context
    else:
        user_prompt = f"""问题：{question}

本地检索资料：
{context}

请只根据以上资料回答，并使用 [S1]、[S2] 等标记引用来源。"""
        client = OllamaOpenAIClient(config.ollama)
        answer = client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )

    if show_context and not no_llm:
        answer = answer.rstrip() + "\n\n检索上下文：\n" + context

    sources = format_sources(results)
    if sources:
        answer = answer.rstrip() + "\n\n来源：\n" + sources
    return answer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ask questions against the local offline MCagent index.")
    parser.add_argument("question", nargs="*", help="Question text. If omitted, stdin prompt is used.")
    parser.add_argument("--config", help="Path to config JSON. Defaults to config.json.")
    parser.add_argument("--top-k", type=int, help="Number of chunks to retrieve.")
    parser.add_argument("--no-llm", action="store_true", help="Only print retrieval results; do not call Ollama.")
    parser.add_argument("--show-context", action="store_true", help="Append retrieved context to the answer.")
    parser.add_argument("--temperature", type=float, help="Override generation temperature.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    question = " ".join(args.question).strip()
    if not question:
        question = input("问题> ").strip()
    if not question:
        parser.error("question is required")

    try:
        answer = answer_question(
            question,
            config_path=args.config,
            top_k=args.top_k,
            no_llm=args.no_llm,
            show_context=args.show_context,
            temperature=args.temperature,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
