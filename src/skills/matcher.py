"""
SkillMatcher - intent matching via keyword scoring and LLM fallback.

Matching pipeline:
  1. Keyword weighted matching (score > 0 threshold)
  2. Description overlap matching (when skill has no keywords)
  3. LLM fallback classification (when no keyword/description match)
  4. Fallback: default_skill if configured, else SKILL_NOT_FOUND
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, Sequence

from src.common.logger import get_logger

from .models import Skill
from .registry import SkillRegistry

logger = get_logger()

_EN_TOKEN_PATTERN = re.compile(r"[a-z0-9_-]+")
_ZH_SPAN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_STOPWORDS = frozenset({
    "的", "了", "是", "在", "和", "与",
    "a", "an", "the", "is", "to", "for", "and",
})


class MatchStatus(Enum):
    """Status of a skill match attempt."""
    KEYWORD_MATCH = "keyword_match"
    LLM_MATCH = "llm_match"
    DEFAULT_FALLBACK = "default_fallback"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class MatchResult:
    """Result of skill matching."""
    skill: Optional[Skill]
    status: MatchStatus
    score: float = 0.0
    details: str = ""


class LLMClassifier(Protocol):
    """Protocol for LLM-based skill classification."""

    async def classify(
        self,
        task_description: str,
        skill_candidates: Sequence[Skill],
    ) -> Optional[str]:
        """
        Classify a task to a skill_id using LLM.

        Args:
            task_description: The user's task or query.
            skill_candidates: Available skills to choose from.

        Returns:
            The matched skill_id, or None if no match.
        """
        ...


class SkillMatcher:
    """
    Matches user intent to a registered skill.

    Pipeline:
      1. Keyword weighted matching
      2. LLM fallback (if classifier provided)
      3. default_skill fallback
      4. SKILL_NOT_FOUND
    """

    def __init__(
        self,
        registry: SkillRegistry,
        llm_classifier: Optional[LLMClassifier] = None,
        keyword_threshold: float = 0.0,
    ) -> None:
        self._registry = registry
        self._llm_classifier = llm_classifier
        self._keyword_threshold = keyword_threshold

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    def match_by_keyword(
        self,
        task_description: str,
        preferred_skill_ids: Sequence[str] = (),
    ) -> MatchResult:
        """
        Match using keyword weighted scoring with optional soft preferences.

        Preferred skills receive a small reranking bonus, but non-preferred
        skills can still win when their task match is materially stronger.
        For skills without keywords, a lightweight description overlap score
        is used instead.
        """
        if not task_description.strip():
            return _not_found("Empty task description")

        scored = _score_all_skills(
            task_description,
            self._registry.list_all(),
        )
        if preferred_skill_ids:
            scored = _score_all_skills_with_preferences(
                task_description,
                self._registry.list_all(),
                preferred_skill_ids=preferred_skill_ids,
            )

        matches = [
            (skill, score, raw_score) for skill, score, raw_score in scored
            if raw_score > self._keyword_threshold
        ]
        matches.sort(key=lambda pair: (pair[1], pair[0].priority), reverse=True)

        if matches:
            best_skill, best_score, best_raw_score = matches[0]
            logger.info(
                f"[SkillMatcher] Keyword match: '{best_skill.skill_id}' "
                f"(score={best_score:.2f}, raw={best_raw_score:.2f})"
            )
            return MatchResult(
                skill=best_skill,
                status=MatchStatus.KEYWORD_MATCH,
                score=best_score,
                details=f"Matched {len(matches)} skill(s) by keyword",
            )

        return _not_found("No keyword match")

    async def match(
        self,
        task_description: str,
        preferred_skill_ids: Sequence[str] = (),
    ) -> MatchResult:
        """
        Full matching pipeline: keyword -> LLM -> default -> not_found.

        Args:
            task_description: User's task or query text.

        Returns:
            MatchResult with the best-matching skill or not_found status.
        """
        # Step 1: Keyword matching
        keyword_result = self.match_by_keyword(
            task_description,
            preferred_skill_ids=preferred_skill_ids,
        )
        if keyword_result.status == MatchStatus.KEYWORD_MATCH:
            return keyword_result

        # Step 2: LLM fallback
        if self._llm_classifier is not None:
            llm_result = await self._try_llm_classification(
                task_description,
                preferred_skill_ids=preferred_skill_ids,
            )
            if llm_result is not None:
                return llm_result

        # Step 3: Preferred skill soft fallback
        preferred_skill = self._get_first_preferred_skill(preferred_skill_ids)
        if preferred_skill is not None:
            logger.info(
                f"[SkillMatcher] Falling back to preferred skill: "
                f"'{preferred_skill.skill_id}'"
            )
            return MatchResult(
                skill=preferred_skill,
                status=MatchStatus.DEFAULT_FALLBACK,
                score=0.0,
                details="No keyword or LLM match; using preferred skill",
            )

        # Step 4: Default skill fallback
        default_skill = self._registry.get_default_skill()
        if default_skill is not None:
            logger.info(
                f"[SkillMatcher] Falling back to default skill: "
                f"'{default_skill.skill_id}'"
            )
            return MatchResult(
                skill=default_skill,
                status=MatchStatus.DEFAULT_FALLBACK,
                score=0.0,
                details="No keyword or LLM match; using default skill",
            )

        # Step 5: Not found
        logger.warning("[SkillMatcher] No skill matched and no default configured")
        return _not_found("No match found and no default skill configured")

    async def _try_llm_classification(
        self,
        task_description: str,
        preferred_skill_ids: Sequence[str] = (),
    ) -> Optional[MatchResult]:
        """Attempt LLM-based classification."""
        try:
            candidates = _sort_candidates_by_preference(
                self._registry.list_all(),
                preferred_skill_ids,
            )
            skill_id = await self._llm_classifier.classify(
                task_description, candidates,
            )
            if skill_id:
                skill = self._registry.get(skill_id)
                if skill:
                    logger.info(
                        f"[SkillMatcher] LLM classified to: '{skill_id}'"
                    )
                    return MatchResult(
                        skill=skill,
                        status=MatchStatus.LLM_MATCH,
                        score=0.0,
                        details=f"LLM classified to '{skill_id}'",
                    )
        except Exception as exc:
            logger.error(
                f"[SkillMatcher] LLM classification failed: {exc}",
                exc_info=True,
            )
        return None

    def _get_first_preferred_skill(
        self,
        preferred_skill_ids: Sequence[str],
    ) -> Optional[Skill]:
        """Return the first existing preferred skill from the given sequence."""
        for skill_id in _normalize_preferred_skill_ids(preferred_skill_ids):
            skill = self._registry.get(skill_id)
            if skill is not None:
                return skill
        return None


def _score_all_skills(
    task_description: str,
    skills: Sequence[Skill],
) -> list[tuple[Skill, float, float]]:
    """Score all skills against a task description using keyword weights."""
    return _score_all_skills_with_preferences(
        task_description,
        skills,
        preferred_skill_ids=(),
    )


def _score_all_skills_with_preferences(
    task_description: str,
    skills: Sequence[Skill],
    preferred_skill_ids: Sequence[str] = (),
) -> list[tuple[Skill, float, float]]:
    """
    Score all skills against a task description using keyword weights.

    Returns tuples of: (skill, reranked_score, raw_keyword_score).
    """
    task_lower = task_description.lower()
    preferred = set(_normalize_preferred_skill_ids(preferred_skill_ids))
    results: list[tuple[Skill, float, float]] = []
    for skill in skills:
        raw_score = _compute_keyword_score(task_lower, skill)
        score = raw_score + (_preferred_bonus(raw_score) if skill.skill_id in preferred else 0.0)
        results.append((skill, score, raw_score))
    return results


def _compute_keyword_score(task_lower: str, skill: Skill) -> float:
    """Compute a match score for a skill against lowered task text."""
    if not skill.keywords:
        return _description_score(task_lower, skill.description)

    score = 0.0
    for kw in skill.keywords:
        if kw.keyword.lower() in task_lower:
            score += kw.weight
    return score


def _description_score(task: str, description: str) -> float:
    """Compute a lightweight overlap score between task text and description."""
    task_tokens = _tokenize_for_match(task)
    desc_tokens = _tokenize_for_match(description)
    if not task_tokens or not desc_tokens:
        return 0.0
    overlap = task_tokens & desc_tokens
    if not overlap:
        return 0.0
    return len(overlap) / max(len(task_tokens), len(desc_tokens))


def _tokenize_for_match(text: str) -> set[str]:
    """
    Tokenize mixed Chinese/English text for lightweight skill matching.

    - English: extract `[a-z0-9_-]+`, dropping one-character tokens
    - Chinese: keep 2-grams and full spans of length >= 3
    - Mixed text is handled by combining both token sets
    - High-frequency stopwords are removed
    """
    lowered = str(text or "").strip().lower()
    if not lowered:
        return set()

    tokens: set[str] = set()

    for token in _EN_TOKEN_PATTERN.findall(lowered):
        if len(token) < 2 or token in _STOPWORDS:
            continue
        tokens.add(token)

    for span in _ZH_SPAN_PATTERN.findall(lowered):
        if len(span) >= 3 and span not in _STOPWORDS:
            tokens.add(span)
        if len(span) < 2:
            continue
        for idx in range(len(span) - 1):
            gram = span[idx:idx + 2]
            if gram not in _STOPWORDS:
                tokens.add(gram)

    return tokens


def _normalize_preferred_skill_ids(
    preferred_skill_ids: Sequence[str],
) -> tuple[str, ...]:
    """Normalize preferred skill ids while preserving order and uniqueness."""
    normalized: list[str] = []
    seen: set[str] = set()
    for skill_id in preferred_skill_ids:
        skill_text = str(skill_id or "").strip()
        if not skill_text or skill_text in seen:
            continue
        normalized.append(skill_text)
        seen.add(skill_text)
    return tuple(normalized)


def _sort_candidates_by_preference(
    skills: Sequence[Skill],
    preferred_skill_ids: Sequence[str],
) -> tuple[Skill, ...]:
    """Move preferred skills to the front while preserving relative order."""
    preferred = set(_normalize_preferred_skill_ids(preferred_skill_ids))
    preferred_items = [skill for skill in skills if skill.skill_id in preferred]
    other_items = [skill for skill in skills if skill.skill_id not in preferred]
    return tuple(preferred_items + other_items)


def _preferred_bonus(raw_score: float) -> float:
    """
    Compute a soft preference bonus.

    Keep the bonus modest so a much better non-preferred keyword match can still win.
    """
    if raw_score <= 0:
        return 0.0
    return min(0.5, raw_score * 0.25)


def _not_found(details: str) -> MatchResult:
    """Create a SKILL_NOT_FOUND result."""
    return MatchResult(
        skill=None,
        status=MatchStatus.NOT_FOUND,
        score=0.0,
        details=details,
    )
