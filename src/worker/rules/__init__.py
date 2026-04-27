"""Self-evolving rules subsystem - CRUD, conflict detection, prompt injection."""
import importlib

_EXPORTS = {
    "Rule": ".models",
    "RuleCandidate": ".models",
    "RuleQuery": ".models",
    "RuleScope": ".models",
    "RuleSource": ".models",
    "CONFIDENCE_BOOST": ".rule_manager",
    "CONFIDENCE_PENALTY": ".rule_manager",
    "CONFIDENCE_DECAY_PER_30D": ".rule_manager",
    "MIN_CONFIDENCE_TO_ACTIVATE": ".rule_manager",
    "create_rule": ".rule_manager",
    "detect_conflict": ".rule_manager",
    "load_rules": ".rule_manager",
    "update_confidence": ".rule_manager",
    "select_rules": ".rule_injector",
    "format_for_prompt": ".rule_injector",
    "extract_rule_from_feedback": ".rule_generator",
    "extract_rule_from_reflection": ".rule_generator",
    "validate_and_create_rule": ".rule_generator",
    "CRYSTALLIZATION_CONFIDENCE": ".crystallizer",
    "CRYSTALLIZATION_APPLY_COUNT": ".crystallizer",
    "CrystallizationCandidate": ".crystallizer",
    "CrystallizationResult": ".crystallizer",
    "identify_crystallization_candidates": ".crystallizer",
    "run_crystallization_cycle": ".crystallizer",
    "SHARING_CONFIDENCE": ".shared_store",
    "SHARING_APPLY_COUNT": ".shared_store",
    "SharedRule": ".shared_store",
    "identify_sharable_rules": ".shared_store",
    "propose_to_shared_store": ".shared_store",
    "load_shared_rules": ".shared_store",
    "discover_adoptable_rules": ".shared_store",
    "adopt_shared_rule": ".shared_store",
    "run_sharing_cycle": ".shared_store",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "Rule",
    "RuleCandidate",
    "RuleQuery",
    "RuleScope",
    "RuleSource",
    "CONFIDENCE_BOOST",
    "CONFIDENCE_PENALTY",
    "CONFIDENCE_DECAY_PER_30D",
    "MIN_CONFIDENCE_TO_ACTIVATE",
    "create_rule",
    "detect_conflict",
    "load_rules",
    "update_confidence",
    "select_rules",
    "format_for_prompt",
    "extract_rule_from_feedback",
    "extract_rule_from_reflection",
    "validate_and_create_rule",
    "CRYSTALLIZATION_CONFIDENCE",
    "CRYSTALLIZATION_APPLY_COUNT",
    "CrystallizationCandidate",
    "CrystallizationResult",
    "identify_crystallization_candidates",
    "run_crystallization_cycle",
    "SHARING_CONFIDENCE",
    "SHARING_APPLY_COUNT",
    "SharedRule",
    "identify_sharable_rules",
    "propose_to_shared_store",
    "load_shared_rules",
    "discover_adoptable_rules",
    "adopt_shared_rule",
    "run_sharing_cycle",
]
