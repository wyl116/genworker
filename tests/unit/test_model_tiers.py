# edition: baseline
from src.services.llm.model_tiers import DEFAULT_TIER, ModelTier


def test_model_tier_from_value_parses_known_values():
    assert ModelTier.from_value("fast") is ModelTier.FAST
    assert ModelTier.from_value("REASONING") is ModelTier.REASONING


def test_model_tier_from_value_falls_back_to_default():
    assert ModelTier.from_value("unknown") is DEFAULT_TIER
    assert ModelTier.from_value(None) is DEFAULT_TIER
