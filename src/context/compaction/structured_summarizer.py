"""Structured iterative history summarization."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.services.llm.intent import LLMCallIntent, Purpose


STRUCTURED_SUMMARY_TEMPLATE = """\
请基于以下对话历史生成/更新结构化摘要。

{previous_summary}

请按以下格式输出：

## 目标
当前任务或会话的核心目标。

## 已完成
- 已完成的步骤、发现和结论

## 关键决策
- 做出的决策及其理由

## 已修改对象
- 已修改的文件、资源或系统组件

## 影响面
- 这些修改可能影响的下游组件或功能

## 未决问题
- 需要用户确认或外部输入才能推进的问题

## 待办
- 接下来要做的具体步骤

## 关键上下文
- 后续对话中必须保留的关键事实和约束

---
对话历史：
{conversation}
"""


@dataclass(frozen=True)
class StructuredSummary:
    goal: str
    progress: tuple[str, ...]
    decisions: tuple[str, ...]
    pending: tuple[str, ...]
    key_context: tuple[str, ...]
    modified_objects: tuple[str, ...] = ()
    impact_scope: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    raw_text: str = ""


async def summarize_structured(
    messages: tuple[dict[str, Any], ...],
    previous_summary: StructuredSummary | None,
    llm_client: Any,
) -> StructuredSummary:
    """Generate or update a structured summary."""
    previous_text = (
        "\n前一轮摘要：\n" + previous_summary.raw_text
        if previous_summary is not None else ""
    )
    prompt = STRUCTURED_SUMMARY_TEMPLATE.format(
        previous_summary=previous_text,
        conversation=_format_messages(messages),
    )
    response = await llm_client.invoke(
        messages=[{"role": "user", "content": prompt}],
        intent=LLMCallIntent(
            purpose=Purpose.SUMMARIZE,
            quality_critical=True,
        ),
    )
    raw = getattr(response, "content", "") or ""
    if not raw:
        return StructuredSummary("", (), (), (), (), "")
    return _parse_structured_summary(raw)


def save_summary(worker_dir: Path, summary: StructuredSummary) -> None:
    """Persist the latest structured summary to disk."""
    summary_path = worker_dir / "memory" / "structured_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "goal": summary.goal,
                "progress": list(summary.progress),
                "decisions": list(summary.decisions),
                "modified_objects": list(summary.modified_objects),
                "impact_scope": list(summary.impact_scope),
                "open_questions": list(summary.open_questions),
                "pending": list(summary.pending),
                "key_context": list(summary.key_context),
                "raw_text": summary.raw_text,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_previous_summary(worker_dir: Path) -> StructuredSummary | None:
    """Load the previously persisted structured summary."""
    summary_path = worker_dir / "memory" / "structured_summary.json"
    if not summary_path.exists():
        return None
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    return StructuredSummary(
        goal=str(data.get("goal", "")),
        progress=tuple(data.get("progress", [])),
        decisions=tuple(data.get("decisions", [])),
        modified_objects=tuple(data.get("modified_objects", [])),
        impact_scope=tuple(data.get("impact_scope", [])),
        open_questions=tuple(data.get("open_questions", [])),
        pending=tuple(data.get("pending", [])),
        key_context=tuple(data.get("key_context", [])),
        raw_text=str(data.get("raw_text", "")),
    )


def summary_to_message(summary: StructuredSummary) -> dict[str, str]:
    """Convert a structured summary to a compact assistant message."""
    return {
        "role": "assistant",
        "content": f"[Structured summary]\n{summary.raw_text}",
    }


def _parse_structured_summary(raw_text: str) -> StructuredSummary:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    sections: dict[str, list[str]] = {
        "目标": [],
        "已完成": [],
        "进展": [],
        "关键决策": [],
        "决策": [],
        "已修改对象": [],
        "影响面": [],
        "未决问题": [],
        "待办": [],
        "关键上下文": [],
    }
    current_key: str | None = None
    for line in lines:
        heading = _match_heading(line)
        if heading is not None:
            current_key = heading
            remainder = line.split("：", 1)[1].strip() if "：" in line else ""
            if remainder:
                sections[current_key].append(remainder)
            continue
        if current_key is not None:
            sections[current_key].append(line.lstrip("- ").strip())

    return StructuredSummary(
        goal=" ".join(sections["目标"]).strip(),
        progress=tuple(sections["已完成"] or sections["进展"]),
        decisions=tuple(sections["关键决策"] or sections["决策"]),
        modified_objects=tuple(sections["已修改对象"]),
        impact_scope=tuple(sections["影响面"]),
        open_questions=tuple(sections["未决问题"]),
        pending=tuple(sections["待办"]),
        key_context=tuple(sections["关键上下文"]),
        raw_text=raw_text.strip(),
    )


def _match_heading(line: str) -> str | None:
    for heading in (
        "目标",
        "已完成",
        "进展",
        "关键决策",
        "决策",
        "已修改对象",
        "影响面",
        "未决问题",
        "待办",
        "关键上下文",
    ):
        if line.startswith(f"## {heading}") or (heading in line and "：" in line):
            return heading
    return None


def _format_messages(messages: tuple[dict[str, Any], ...]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        content = message.get("content", "")
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)
