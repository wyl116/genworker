"""Routing policy for intent-based tier selection."""

from __future__ import annotations

from typing import Protocol

from .intent import LLMCallIntent, Purpose
from .model_tiers import ModelTier


class RoutingPolicy(Protocol):
    """Select a tier alias key for a given LLM intent."""

    def choose(self, intent: LLMCallIntent) -> str:
        """Return a base tier alias key: ``fast`` / ``standard`` / ``strong`` / ``reasoning``."""
        ...


class TableRoutingPolicy:
    """Default static routing policy matching the design matrix."""

    def choose(self, intent: LLMCallIntent) -> str:
        base_tier = self._choose_base_tier(intent)
        if intent.requires_tools and base_tier is ModelTier.FAST:
            base_tier = ModelTier.STANDARD
        return base_tier.value

    def _choose_base_tier(self, intent: LLMCallIntent) -> ModelTier:
        if intent.requires_reasoning:
            return ModelTier.REASONING

        purpose = intent.purpose
        if purpose is Purpose.CHAT_TURN:
            tier = ModelTier.STRONG
            if intent.latency_sensitive:
                tier = ModelTier.STANDARD
            return tier

        if purpose is Purpose.PLAN:
            if intent.quality_critical and intent.requires_long_context:
                return ModelTier.STRONG
            if intent.quality_critical:
                return ModelTier.STRONG
            return ModelTier.STANDARD

        if purpose is Purpose.REFLECT:
            if intent.quality_critical:
                return ModelTier.STRONG
            return ModelTier.STANDARD

        if purpose is Purpose.STRATEGIZE:
            return ModelTier.STANDARD

        if purpose is Purpose.EXTRACT:
            if intent.quality_critical:
                return ModelTier.STANDARD
            return ModelTier.FAST

        if purpose is Purpose.SUMMARIZE:
            if intent.requires_long_context:
                return ModelTier.STRONG
            return ModelTier.STANDARD

        if purpose is Purpose.CLASSIFY:
            if intent.quality_critical:
                return ModelTier.STANDARD
            return ModelTier.FAST

        if purpose is Purpose.GENERATE:
            return ModelTier.STANDARD

        if purpose is Purpose.TOOL_CALL:
            if intent.latency_sensitive:
                return ModelTier.STANDARD
            return ModelTier.STRONG

        return ModelTier.STANDARD
