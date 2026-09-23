"""SDK-free recent complete-turn window for Mem0 compatibility and memory-off."""
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from personal_workbench.context_budget import count, dump, size, excerpt, ContextLimit


class RecentContext:
    def __init__(self, settings):
        self.settings = settings
        self.calls = self.usage = 0

    def prepare(self, state, prefix, tools, **kwargs):
        messages = list(state.get("messages", []))
        schema = size(dump([convert_to_openai_tool(t) for t in tools]))
        reserve = self.settings.max_tokens + max(1024, self.settings.context_window // 10)
        available = self.settings.context_window - count(prefix) - schema - reserve
        removed = 0
        # Drop whole old turns only: never leave orphaned tool-call/result pairs.
        while count(messages) > available:
            next_turn = next((i for i, m in enumerate(messages[1:], 1) if m.type == "human"), None)
            if next_turn is None:
                break
            removed += next_turn
            messages = messages[next_turn:]
        trimmed = []
        for i in sorted(range(len(messages)), key=lambda i: size(messages[i].content), reverse=True):
            if count(messages) <= available:
                break
            m = messages[i]
            if isinstance(m, ToolMessage) and size(m.content) > 600:
                messages[i] = m.model_copy(update={"content": excerpt(m.content, max(600, size(m.content) - (count(messages) - available) - 100))})
                trimmed.append(m.id)
        if available <= 0 or count(messages) > available:
            raise ContextLimit("近期原文窗口已满，请缩短本轮问题或减少工具；原始对话已保留。")
        insertion = max((i for i, m in enumerate(messages) if m.type == "human"), default=0)
        prepared = ([prefix[0], *prefix[2:], *messages[:insertion], prefix[1], *messages[insertion:]] if len(prefix) > 1 else [*prefix, *messages])
        return {"prepared_messages": prepared, "running_summary": {}, "request_summary": {}, "task_state": {},
                "context_error": "", "context_budget": {"policy": "recent_turns", "omitted_messages": removed,
                    "trimmed_tool_ids": trimmed, "estimated_input": count(prefix) + schema + count(messages),
                    "window": self.settings.context_window, "output_reserve": self.settings.max_tokens}}

    def update_task(self, state):
        return {}
