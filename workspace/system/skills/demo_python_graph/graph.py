from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, StateGraph

from src.engine.langgraph.context import NodeContext


@dataclass(frozen=True)
class DemoState:
    task: str = ""
    summary: str = ""


def build_graph(ctx: NodeContext):
    graph = StateGraph(DemoState)

    async def summarize(state: DemoState):
        response = await ctx.llm.invoke(
            messages=[
                {"role": "system", "content": ctx.instruction("summary")},
                {"role": "user", "content": state.task},
            ],
            intent=ctx.intent("summary"),
        )
        return {"summary": response.content}

    graph.add_node("summarize", summarize)
    graph.set_entry_point("summarize")
    graph.add_edge("summarize", END)
    return graph.compile(checkpointer=ctx.checkpointer)
