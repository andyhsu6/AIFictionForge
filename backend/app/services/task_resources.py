"""后台任务类型与完成后需要刷新的前端资源映射。"""

TASK_TYPE_RESOURCES: dict[str, tuple[str, ...]] = {
    "outline_new": ("outlines", "chapters", "projects"),
    "outline_continue": ("outlines", "chapters", "projects"),
    "outline_expand": ("outlines", "chapters"),
    "outline_batch_expand": ("outlines", "chapters"),
    "chapter_generate": ("chapters", "projects", "characters", "analysis", "foreshadows"),
    "chapter_batch": ("chapters", "projects", "characters", "analysis", "foreshadows"),
    "chapter_regenerate": ("chapters", "projects"),
    "chapter_partial_regenerate": ("chapters", "projects"),
    "chapter_analysis": ("analysis", "characters", "foreshadows"),
    "character_generate": ("characters",),
    "organization_generate": ("characters", "organizations"),
    "career_generate": ("careers",),
    "wizard": ("projects", "outlines", "chapters", "characters"),
}

AGENT_TASK_ACTION_TYPES: dict[str, str] = {
    "generate_outlines": "outline_new",
    "expand_outline": "outline_expand",
    "batch_expand_outlines": "outline_batch_expand",
    "generate_chapter": "chapter_generate",
    "batch_generate_chapters": "chapter_batch",
    "analyze_chapter": "chapter_analysis",
    "regenerate_chapter": "chapter_regenerate",
    "partial_regenerate_chapter": "chapter_partial_regenerate",
    "generate_character": "character_generate",
    "generate_organization": "organization_generate",
    "generate_careers": "career_generate",
}


def affected_resources_for_task(task_type: str) -> list[str]:
    return list(TASK_TYPE_RESOURCES.get(task_type, ()))


def affected_resources_for_agent_action(action: str) -> list[str]:
    task_type = AGENT_TASK_ACTION_TYPES.get(action, action)
    return ["tasks", *affected_resources_for_task(task_type)]
