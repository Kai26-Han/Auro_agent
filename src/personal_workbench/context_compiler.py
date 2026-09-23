"""Typed context layers and their budget manifest.

The compiler owns *where* every context source sits and how its cost is
reported.  It deliberately does not retrieve memory, knowledge or skills;
those subsystems provide bounded text or tool results and remain responsible
for their own data lifecycle.
"""
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from personal_workbench.context_budget import count, excerpt


KNOWLEDGE_TOOLS = {"list_files", "search_files", "read_file", "read_outline", "read_page"}
SKILL_TOOLS = {"skills_list", "skill_view", "read_skill_resource", "run_skill_script"}
WEB_TOOLS = {"web_search", "web_fetch", "paper_search"}
CONVERSATION_TOOLS = {"search_conversation", "read_conversation_message"}


@dataclass(frozen=True)
class ContextInputs:
    """Inputs supplied by the runtime, before conversation history is added."""

    fixed_instructions: str
    workspace_instructions: str = ""
    skill_instructions: str = ""
    long_term_memory: str = ""
    runtime_capabilities: str = ""


@dataclass
class ContextLayer:
    id: str
    label: str
    messages: list = field(default_factory=list)
    policy: str = "required"
    required: bool = True
    original_tokens: int = 0
    truncated: bool = False
    source_count: int = 0

    def __post_init__(self):
        if not self.original_tokens:
            self.original_tokens = count(self.messages)
        if not self.source_count:
            self.source_count = len(self.messages)

    @property
    def used_tokens(self):
        return count(self.messages)

    def public(self, budget_tokens=None, parent=None):
        row = {
            "id": self.id,
            "label": self.label,
            "policy": self.policy,
            "required": self.required,
            "budget_tokens": self.used_tokens if budget_tokens is None else max(0, budget_tokens),
            "used_tokens": self.used_tokens,
            "original_tokens": self.original_tokens,
            "source_count": self.source_count,
            "truncated": self.truncated,
            "status": "truncated" if self.truncated else "included" if self.messages else "empty",
        }
        if parent:
            row["parent"] = parent
        return row


class ContextCompiler:
    """Build a stable, inspectable context package for one model call."""

    ORDER = (
        "fixed_instructions", "workspace_instructions", "skill_instructions",
        "task_state", "long_term_memory", "runtime_capabilities",
    )

    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def normalize(value):
        if isinstance(value, ContextInputs):
            return value, False
        # C1 compatibility: local extensions may still pass a list of messages.
        messages = list(value or [])
        fixed = messages[0].text if messages else ""
        runtime = "\n\n".join(m.text for m in messages[1:])
        return ContextInputs(fixed_instructions=fixed, runtime_capabilities=runtime), True

    def static_layers(self, inputs, task_message=None):
        values = {
            "fixed_instructions": ("固定指令", inputs.fixed_instructions, "required", True),
            "workspace_instructions": ("工作区与知识规则", inputs.workspace_instructions, "required", True),
            "skill_instructions": ("技能与伙伴方法", inputs.skill_instructions, "required", True),
            "task_state": ("当前任务状态", task_message.text if task_message else "", "required", True),
            "long_term_memory": ("长期记忆", inputs.long_term_memory, "bounded", False),
            "runtime_capabilities": ("本轮工具与能力", inputs.runtime_capabilities, "required", True),
        }
        layers = []
        for layer_id in self.ORDER:
            label, content, policy, required = values[layer_id]
            layers.append(ContextLayer(layer_id, label,
                                       [SystemMessage(content=content)] if content else [],
                                       policy=policy, required=required))
        return layers

    @staticmethod
    def clip_layer(layer, budget_tokens):
        """Bound a flexible system layer while preserving its safety framing."""
        if layer.required or layer.used_tokens <= budget_tokens:
            return layer
        original = layer.original_tokens
        if budget_tokens <= 48:
            layer.messages = []
        else:
            content = layer.messages[0].text
            layer.messages = [SystemMessage(content=excerpt(content, budget_tokens - 16))]
            if layer.used_tokens > budget_tokens:
                layer.messages = []
        layer.original_tokens = original
        layer.truncated = layer.used_tokens < original
        return layer

    @staticmethod
    def messages(layers):
        return [message for layer in layers for message in layer.messages]

    @staticmethod
    def _result_layer(message):
        if isinstance(message, ToolMessage):
            name = message.name or ""
            if name in KNOWLEDGE_TOOLS:
                return "rag_results"
            if name in SKILL_TOOLS:
                return "skill_results"
            if name in WEB_TOOLS:
                return "web_results"
            if name in CONVERSATION_TOOLS:
                return "conversation_results"
            return "current_results"
        if isinstance(message, AIMessage) and message.tool_calls:
            names = {call.get("name", "") for call in message.tool_calls}
            if names and names <= KNOWLEDGE_TOOLS:
                return "rag_results"
            if names and names <= SKILL_TOOLS:
                return "skill_results"
            if names and names <= WEB_TOOLS:
                return "web_results"
            if names and names <= CONVERSATION_TOOLS:
                return "conversation_results"
        return "current_results"

    def history_layers(self, history, has_summary=False, summary_sources=0, original_costs=None):
        original_costs = original_costs or {}
        def original(messages):
            return sum(original_costs.get(getattr(message, "id", None), count([message])) for message in messages)
        def layer(layer_id, label, messages, policy, required=False, source_count=0):
            return ContextLayer(layer_id, label, messages, policy=policy, required=required,
                                original_tokens=original(messages),
                                source_count=source_count or len(messages),
                                truncated=count(messages) < original(messages))
        rows = []
        raw = list(history)
        core, segments = [], []
        while raw and isinstance(raw[0], SystemMessage):
            marker = raw[0].additional_kwargs.get("context_layer")
            if marker == "history_core_summary":
                core.append(raw.pop(0))
            elif marker == "history_segment_summaries":
                segments.append(raw.pop(0))
            elif has_summary and not core and not segments:
                # Compatibility for checkpoints produced before C4.
                core.append(raw.pop(0))
            else:
                break
        rows.append(layer("history_core_summary", "会话核心摘要", core,
                          "bounded", source_count=sum(
                              m.additional_kwargs.get("source_count", 0) for m in core)
                          or (summary_sources if core else 0)))
        rows.append(layer("history_segment_summaries", "会话阶段摘要", segments,
                          "bounded", source_count=sum(
                              m.additional_kwargs.get("source_count", 0) for m in segments)))
        latest = max((i for i, message in enumerate(raw) if message.type == "human"), default=-1)
        recent = raw[:latest] if latest >= 0 else raw
        current = raw[latest:latest + 1] if latest >= 0 else []
        results = raw[latest + 1:] if latest >= 0 else []
        rows.append(layer("recent_messages", "近期原文", recent, "residual"))
        rows.append(layer("current_request", "当前用户请求", current, "protected", True))
        grouped = {name: [] for name in (
            "rag_results", "skill_results", "web_results",
            "conversation_results", "current_results")}
        for message in results:
            grouped[self._result_layer(message)].append(message)
        labels = {
            "rag_results": "本轮知识库结果",
            "skill_results": "本轮技能结果",
            "web_results": "本轮网络结果",
            "conversation_results": "本轮会话历史结果",
            "current_results": "本轮其他结果",
        }
        for name in grouped:
            rows.append(layer(name, labels[name], grouped[name], "bounded"))
        return rows

    def manifest(self, static_layers, history, *, schema_tokens, history_budget,
                 has_summary=False, summary_sources=0, summary_limit=0,
                 original_costs=None):
        rows = [layer.public() for layer in static_layers]
        rows.append({
            "id": "tool_definitions", "label": "工具定义", "policy": "required",
            "required": True, "budget_tokens": schema_tokens, "used_tokens": schema_tokens,
            "original_tokens": schema_tokens, "source_count": 0,
            "truncated": False, "status": "included" if schema_tokens else "empty",
        })
        dynamic = self.history_layers(history, has_summary, summary_sources, original_costs)
        for layer in dynamic:
            limit = summary_limit if layer.id in {"history_core_summary", "history_segment_summaries"} else layer.used_tokens
            rows.append(layer.public(budget_tokens=limit))
        return rows
