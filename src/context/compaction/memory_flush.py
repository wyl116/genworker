"""Memory flush before compaction."""
from __future__ import annotations

import json
from typing import Any

from src.common.content_scanner import scan
from src.services.llm.intent import LLMCallIntent, Purpose


MEMORY_FLUSH_PROMPT = """\
你即将失去以下对话历史的访问权限。在它被压缩前，
提取值得保留的隐式洞察。

提取规则：
1. 只提取真正有用的洞察（非显而易见的事实）
2. 聚焦行为模式、决策策略、新发现的规律
3. 每条洞察必须独立可理解

输出 JSON：
{{"episodes": [...], "rule_candidates": [...]}}
如无值得保留的内容：{{"episodes": [], "rule_candidates": []}}

待分析的对话：
{conversation}
"""


async def flush_memory_before_compaction(
    messages: tuple[dict[str, Any], ...],
    llm_client: Any,
    learning_callback: Any | None = None,
) -> dict[str, tuple[str, ...]]:
    """Extract learnable artifacts from soon-to-be-compressed history."""
    if llm_client is None:
        return {}

    prompt = MEMORY_FLUSH_PROMPT.format(conversation=_format_messages(messages))
    try:
        response = await llm_client.invoke(
            messages=[{"role": "user", "content": prompt}],
            intent=LLMCallIntent(purpose=Purpose.SUMMARIZE),
        )
        payload = json.loads((getattr(response, "content", "") or "").strip() or "{}")
    except Exception:
        return {}

    episodes = tuple(
        item for item in payload.get("episodes", [])
        if isinstance(item, str) and item.strip()
    )
    rule_candidates = tuple(
        item for item in payload.get("rule_candidates", [])
        if isinstance(item, str) and item.strip() and scan(item).is_safe
    )
    result = {"episodes": episodes, "rule_candidates": rule_candidates}
    if learning_callback is None:
        return result
    try:
        callback_result = learning_callback(result)
        if hasattr(callback_result, "__await__"):
            await callback_result
    except Exception:
        return result
    return result


def _format_messages(messages: tuple[dict[str, Any], ...]) -> str:
    return "\n".join(
        f"[{message.get('role', 'unknown')}] {message.get('content', '')}"
        for message in messages
    )
