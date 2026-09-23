"""Stable, user-facing projection of the internal context budget.

The context engine keeps a detailed manifest for diagnostics.  The web UI
only needs a small, versioned summary and must not depend on engine internals.
"""

from __future__ import annotations


SCHEMA_VERSION = "context-window-v1"

SEGMENTS = (
    ("system_workspace", "系统与工作区"),
    ("tools_capabilities", "工具与能力"),
    ("conversation_history", "对话历史"),
    ("current_request", "当前请求"),
    ("knowledge_results", "知识与检索结果"),
    ("skills_partners", "技能与伙伴"),
    ("memory", "长期记忆"),
    ("other", "其他上下文"),
)

LAYER_GROUPS = {
    "fixed_instructions": "system_workspace",
    "workspace_instructions": "system_workspace",
    "task_state": "system_workspace",
    "tool_definitions": "tools_capabilities",
    "runtime_capabilities": "tools_capabilities",
    "history_core_summary": "conversation_history",
    "history_segment_summaries": "conversation_history",
    "recent_messages": "conversation_history",
    "current_request": "current_request",
    "rag_results": "knowledge_results",
    "web_results": "knowledge_results",
    "conversation_results": "knowledge_results",
    "current_results": "knowledge_results",
    "skill_instructions": "skills_partners",
    "skill_results": "skills_partners",
    "long_term_memory": "memory",
}


def _integer(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def context_window_view(budget, *, model="", context_window=0,
                        model_context_window=0, max_tokens=0,
                        compaction_trigger=75, compaction_target=50):
    """Return the public context-window view for one completed model input.

    ``budget`` may be absent for a new or legacy session.  In that case the
    selected model's configured limits are still useful, while usage remains
    deliberately unknown rather than pretending to be zero.
    """

    budget = budget if isinstance(budget, dict) else {}
    window = _integer(
        budget.get("working_context_window", budget.get("context_window")),
        context_window,
    )
    model_window = _integer(
        budget.get("model_context_window"),
        model_context_window or window,
    )
    output = _integer(budget.get("output_reserve"), max_tokens)
    safety = _integer(
        budget.get("safety_margin"),
        max(1024, window // 10) if window else 0,
    )
    capacity = _integer(
        budget.get("input_capacity"),
        max(0, window - output - safety),
    )
    measured = "estimated_input" in budget and budget.get("estimated_input") is not None
    used = _integer(budget.get("estimated_input")) if measured else None
    remaining = max(0, capacity - used) if measured else capacity
    usage_percent = round(min(100, used * 100 / capacity), 1) if measured and capacity else None

    grouped = {key: 0 for key, _ in SEGMENTS}
    layers = budget.get("layers")
    if isinstance(layers, list):
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            group = LAYER_GROUPS.get(str(layer.get("id", "")), "other")
            grouped[group] += _integer(layer.get("used_tokens"))

    if measured:
        manifested = sum(grouped.values())
        # Older checkpoints may have aggregate usage but no layer manifest.
        # A positive delta also accounts for future layers unknown to this UI.
        if manifested < used:
            grouped["other"] += used - manifested

    labels = dict(SEGMENTS)
    segments = [
        {
            "key": key,
            "label": labels[key],
            "tokens": tokens,
            "percent": round(tokens * 100 / window, 1) if window else 0,
        }
        for key, tokens in grouped.items()
        if tokens
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "measured": measured,
        "estimated": measured,
        "model": str(budget.get("model") or model or ""),
        "method": str(budget.get("method") or "utf8_upper_estimate"),
        "updated": budget.get("updated"),
        "model_context_window": max(model_window, window),
        "working_context_window": window,
        "context_window": window,
        "input_capacity": capacity,
        "input_used": used,
        "input_remaining": remaining,
        "output_reserve": output,
        "safety_margin": safety,
        "compaction_trigger_percent": _integer(
            budget.get("compaction_trigger_percent"), compaction_trigger),
        "compaction_target_percent": _integer(
            budget.get("compaction_target_percent"), compaction_target),
        "usage_percent": usage_percent,
        "degraded": bool(budget.get("degraded")),
        "degraded_message": (
            "会话整理服务暂时不可用，已使用已有摘要和近期对话继续。"
            if budget.get("degraded") else ""
        ),
        "compressed_messages": _integer(
            (budget.get("compression") or {}).get("compressed_messages")
            if isinstance(budget.get("compression"), dict) else 0
        ),
        "segments": segments,
    }
