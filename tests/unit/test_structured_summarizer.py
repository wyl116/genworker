# edition: baseline
from pathlib import Path

import pytest

from src.context.compaction.structured_summarizer import (
    StructuredSummary,
    load_previous_summary,
    save_summary,
    summarize_structured,
)


class MockLLM:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return type(
            "Resp",
            (),
            {
                "content": "\n".join((
                    "**目标**：完成代码修改",
                    "- **进展**：已补充测试",
                    "- **决策**：保持文件存储",
                    "- **待办**：运行回归",
                    "- **关键上下文**：需要热加载技能",
                )),
            },
        )()


@pytest.mark.asyncio
async def test_summarize_structured_parses_fields():
    summary = await summarize_structured(
        ({"role": "user", "content": "请完成任务"},),
        previous_summary=None,
        llm_client=MockLLM(),
    )
    assert "完成代码修改" in summary.goal
    assert "已补充测试" in summary.progress[0]


def test_save_and_load_summary(tmp_path: Path):
    summary = StructuredSummary(
        goal="g",
        progress=("p",),
        decisions=("d",),
        pending=("todo",),
        key_context=("ctx",),
        raw_text="raw",
    )
    save_summary(tmp_path, summary)
    loaded = load_previous_summary(tmp_path)
    assert loaded == summary
