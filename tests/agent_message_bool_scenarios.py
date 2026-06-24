from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.agent_message import message_from_payload  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def _message_with_requires_reply(value: object) -> dict[str, object]:
    return {
        "agent_message": {
            "from_agent": "User",
            "to_agent": "CrawlerAgent",
            "content": "one-way notice",
            "requires_reply": value,
        }
    }


def test_requires_reply_payload_bool_normalization() -> None:
    false_cases = ["false", "False", "0", "no", "off", 0, False]
    for index, raw_value in enumerate(false_cases):
        parsed = message_from_payload(
            _message_with_requires_reply(raw_value),
            default_to_agent="CrawlerAgent",
            default_content="fallback",
        )
        assert_equal(f"requires_reply_false_{index}", parsed.requires_reply, False)

    true_cases = ["true", "True", "1", "yes", "on", 1, True]
    for index, raw_value in enumerate(true_cases):
        parsed = message_from_payload(
            _message_with_requires_reply(raw_value),
            default_to_agent="CrawlerAgent",
            default_content="fallback",
        )
        assert_equal(f"requires_reply_true_{index}", parsed.requires_reply, True)

    parsed_default = message_from_payload(
        {"agent_message": {"from_agent": "User", "to_agent": "CrawlerAgent", "content": "default reply"}},
        default_to_agent="CrawlerAgent",
        default_content="fallback",
    )
    assert_equal("requires_reply_default", parsed_default.requires_reply, True)


def main() -> int:
    test_requires_reply_payload_bool_normalization()
    print("agent_message_bool_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
