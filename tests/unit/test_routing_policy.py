# edition: baseline
from src.services.llm.intent import LLMCallIntent, Purpose
from src.services.llm.routing_policy import TableRoutingPolicy


def test_fast_with_tools_upgrades_to_standard():
    policy = TableRoutingPolicy()
    tier = policy.choose(
        LLMCallIntent(
            purpose=Purpose.EXTRACT,
            requires_tools=True,
        )
    )
    assert tier == "standard"


def test_standard_with_tools_stays_standard():
    policy = TableRoutingPolicy()
    tier = policy.choose(
        LLMCallIntent(
            purpose=Purpose.GENERATE,
            requires_tools=True,
        )
    )
    assert tier == "standard"


def test_strong_with_tools_stays_strong():
    policy = TableRoutingPolicy()
    tier = policy.choose(
        LLMCallIntent(
            purpose=Purpose.CHAT_TURN,
            requires_tools=True,
        )
    )
    assert tier == "strong"


def test_reasoning_with_tools_stays_reasoning():
    policy = TableRoutingPolicy()
    tier = policy.choose(
        LLMCallIntent(
            purpose=Purpose.PLAN,
            requires_reasoning=True,
            requires_tools=True,
        )
    )
    assert tier == "reasoning"


def test_reflect_reasoning_overrides_quality_flag():
    policy = TableRoutingPolicy()
    tier = policy.choose(
        LLMCallIntent(
            purpose=Purpose.REFLECT,
            requires_reasoning=True,
            quality_critical=True,
        )
    )
    assert tier == "reasoning"


def test_extract_quality_critical_upgrades_to_standard():
    policy = TableRoutingPolicy()
    tier = policy.choose(
        LLMCallIntent(
            purpose=Purpose.EXTRACT,
            quality_critical=True,
        )
    )
    assert tier == "standard"


def test_summarize_long_context_routes_to_strong():
    policy = TableRoutingPolicy()
    tier = policy.choose(
        LLMCallIntent(
            purpose=Purpose.SUMMARIZE,
            requires_long_context=True,
        )
    )
    assert tier == "strong"
