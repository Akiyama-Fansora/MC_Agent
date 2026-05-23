from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_matrix_document_covers_five_directions_with_examples() -> None:
    doc = (ROOT / "docs" / "agent_test_matrix.md").read_text(encoding="utf-8")
    headings = [
        "方向一：MCagent 回答本地已有资料",
        "方向二：MCagent 发现本地资料不足并委托 Crawler",
        "方向三：用户直连 Crawler 获取指定网页数据但不保存",
        "方向四：Crawler 为 MCagent/RAG 找资料并保存入库",
        "方向五：Crawler 获取网页/数据并保存到用户指定本地位置",
    ]
    for heading in headings:
        assert_true(f"heading_{heading}", heading in doc)
    assert_true("matrix_has_many_examples", doc.count("- “") >= 20)
    assert_true("matrix_rejects_hardcoded_rules", "测试例子不是硬编码规则" in doc)


def test_frontend_does_not_show_fixed_three_way_prompt() -> None:
    app = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    forbidden = [
        "判断这是问答、状态，还是要交给 Crawler 采集",
        "判断这份数据是给你看，还是要清洗成 MCagent/RAG 能用的格式",
    ]
    for phrase in forbidden:
        assert_true(f"forbidden_frontend_phrase_{phrase}", phrase not in app)
    assert_true("neutral_mcagent_status", "MCagent 正在读取你的问题。" in app)
    assert_true("neutral_crawler_status", "CrawlerAgent 正在读取你的任务。" in app)
    assert_true("neutral_trace_status", "正在读取你的目标和当前上下文" in app)


def test_crawler_tool_catalog_exposes_temporary_and_persistent_paths() -> None:
    runtime = (ROOT / "mcagent" / "agent_runtime.py").read_text(encoding="utf-8")
    assert_true("temporary_extract_tool", 'name="temporary_extract"' in runtime)
    assert_true("persistent_delegate_tool", 'name="delegate_crawler"' in runtime)
    assert_true("browser_collect_tool", 'name="browser_collect"' in runtime)
    assert_true("save_artifact_tool", 'name="save_artifact"' in runtime)
    assert_true("no_persistence_side_effect", "network_only_no_filesystem_persistence" in runtime)
    assert_true("persistent_side_effect", "start_background_job" in runtime)


def test_router_prompt_does_not_hardcode_url_no_save_rule() -> None:
    router = (ROOT / "mcagent" / "agent_router.py").read_text(encoding="utf-8")
    forbidden = [
        "给出一个或多个具体公开 URL",
        "明确不保存、不入库、不交给 MCagent/RAG，则选择 temporary_extract",
    ]
    for phrase in forbidden:
        assert_true(f"no_hardcoded_router_rule_{phrase}", phrase not in router)
    assert_true("tool_catalog_still_used", "tool_catalog_prompt(agent)" in router)


def main() -> int:
    test_matrix_document_covers_five_directions_with_examples()
    test_frontend_does_not_show_fixed_three_way_prompt()
    test_crawler_tool_catalog_exposes_temporary_and_persistent_paths()
    test_router_prompt_does_not_hardcode_url_no_save_rule()
    print("agent_five_direction_matrix_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
