"""Tier definitions for intent-based LLM routing."""

from __future__ import annotations

from enum import Enum


class ModelTier(str, Enum):
    """Base model tiers used by routing policy and adapter fallback."""

    FAST = "fast"
    STANDARD = "standard"
    STRONG = "strong"
    REASONING = "reasoning"

    @classmethod
    def from_value(cls, value: str | "ModelTier" | None) -> "ModelTier":
        """Parse a tier value with a safe default."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for tier in cls:
                if tier.value == normalized:
                    return tier
        return DEFAULT_TIER

    @classmethod
    def is_valid(cls, value: str | "ModelTier" | None) -> bool:
        """Check whether a value maps to a known base tier."""
        if isinstance(value, cls):
            return True
        if not isinstance(value, str):
            return False
        normalized = value.strip().lower()
        return any(tier.value == normalized for tier in cls)


DEFAULT_TIER = ModelTier.STANDARD
