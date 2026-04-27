"""
Prompt templates for the three planning phases.

Each template is a plain string with named placeholders.
The caller fills them via str.format() or str.format_map().
"""

DECOMPOSITION_PROMPT = (
    "你是 {worker_name}，{worker_role}。\n"
    "当前任务：{task}\n"
    "请将这个任务分解为可执行的子目标：\n"
    "1. 每个子目标应当足够具体，可以直接用一个 Skill 完成\n"
    "2. 标注子目标之间的依赖关系\n"
    "3. 为每个子目标提供非强制的优先 Skill 候选；可输出 `preferred_skill_ids` 或 `skills`\n"
    "4. 如仍需兼容旧格式，也可输出 `skill_hint`\n"
    "你可用的 Skill：{available_skills}\n"
    "你的历史经验：{episodic_context}\n"
    "你的行为规则：{rules_context}\n"
    '输出 JSON：{{"sub_goals": [...], "reasoning": "..."}}'
)

STRATEGY_SELECTION_PROMPT = (
    "对于子目标：{sub_goal_description}\n"
    "以下 Skill 可能适用：{candidate_skills}\n"
    "子目标已有优先 Skill 候选：{preferred_skill_ids}\n"
    "请选择一个建议优先的 Skill 并说明理由；这是软偏好，不是强制绑定。无合适 Skill 则建议委派。\n"
    '输出 JSON：{{"selected_skill": "...", "reason": "...", "delegate_to": null}}'
)

REFLECTION_PROMPT = (
    "原始任务：{original_task}\n"
    "子目标及结果：{sub_goal_results}\n"
    "评估：1. 完成度（0-10分） 2. 遗漏方面 3. 需追加的子目标；追加子目标也可输出 `preferred_skill_ids` 或 `skills`\n"
    '输出 JSON：{{"completeness_score": N, "missing_aspects": [], '
    '"additional_sub_goals": []}}'
)
