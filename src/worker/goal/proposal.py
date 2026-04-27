"""
Goal proposal - worker self-generated goal proposals awaiting approval.

Workers identify improvement opportunities and propose goals via LLM,
which are created with pending approval status.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.services.llm.intent import LLMCallIntent, Purpose

from .models import Goal, GoalProposal, Milestone


async def propose_goal(
    worker_id: str,
    title: str,
    justification: str,
    llm_client: object,
) -> GoalProposal:
    """
    Worker proposes a new goal: LLM generates goal structure with milestones.

    The proposal is created with approval_status="pending".
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    goal_id = f"goal-proposed-{worker_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    # Ask LLM to generate milestones
    milestones = await _generate_milestones(title, justification, llm_client)

    proposed_goal = Goal(
        goal_id=goal_id,
        title=title,
        status="pending_approval",
        priority="medium",
        created_by=f"worker:{worker_id}",
        milestones=milestones,
        preferred_skill_ids=(),
    )

    return GoalProposal(
        proposed_goal=proposed_goal,
        justification=justification,
        proposed_by=f"worker:{worker_id}",
        proposed_at=now_str,
        approval_status="pending",
    )


async def _generate_milestones(
    title: str,
    justification: str,
    llm_client: object,
) -> tuple[Milestone, ...]:
    """Use LLM to generate milestone suggestions for a proposed goal."""
    prompt = (
        f"Propose 2-3 milestones for the goal: '{title}'. "
        f"Context: {justification}. "
        f"Return milestone titles only, one per line."
    )

    try:
        response = await llm_client.invoke(
            messages=[
                {"role": "system", "content": "You generate concise milestone plans."},
                {"role": "user", "content": prompt},
            ],
            intent=LLMCallIntent(purpose=Purpose.GENERATE),
        )
        lines = [
            line.strip() for line in (response.content or "").splitlines()
            if line.strip()
        ]
    except Exception:
        lines = [f"Phase 1: Plan {title}", f"Phase 2: Execute {title}"]

    milestones: list[Milestone] = []
    for i, line in enumerate(lines):
        clean_title = line.lstrip("0123456789.-) ").strip()
        if not clean_title:
            clean_title = line.strip()
        milestones.append(Milestone(
            id=f"ms-{i + 1}",
            title=clean_title,
            status="pending",
        ))

    return tuple(milestones) if milestones else (
        Milestone(id="ms-1", title=f"Plan {title}", status="pending"),
        Milestone(id="ms-2", title=f"Execute {title}", status="pending"),
    )
