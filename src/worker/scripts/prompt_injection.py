"""Prompt injection helpers for pre-script output."""
from __future__ import annotations


def inject_pre_script_output(task: str, script_output: str) -> str:
    """Prepend pre-script output to the task prompt in a stable format."""
    output = (script_output or "").strip()
    if not output:
        return task
    return (
        "## Pre-execution Script Output\n"
        "The following data was collected by the pre-script.\n"
        "Use it as context for your analysis.\n\n"
        f"```\n{output}\n```\n\n"
        f"{task}"
    )
