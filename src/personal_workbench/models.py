"""按供应商创建模型客户端，以及明确标注的离线流程演示。"""

import json
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from personal_workbench.settings import Settings


def _usage_int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def create_model(settings: Settings, tools):
    from personal_workbench.app_settings import configured
    from personal_workbench.runtime_limits import limit_model
    if not configured(settings):
        raise ValueError("请在设置中配置模型密钥，或选择本机模型。")
    if settings.provider == "anthropic":
        return limit_model(AnthropicModel(settings, tools), settings)
    if settings.provider != "deepseek":
        from langchain_openai import ChatOpenAI
        client = ChatOpenAI(model=settings.model, api_key=settings.api_key or "local-no-key",
                          base_url=settings.base_url, timeout=settings.timeout, max_retries=0,
                          max_tokens=settings.max_tokens)
        return limit_model(client.bind_tools(tools) if tools else client, settings)
    from langchain_deepseek import ChatDeepSeek

    client = ChatDeepSeek(
        model=settings.model,
        api_key=settings.api_key,
        api_base=settings.base_url,
        timeout=settings.timeout,
        max_retries=0,
        max_tokens=settings.max_tokens,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return limit_model(client.bind_tools(tools) if tools else client, settings)


class DemoModel:
    """固定脚本，只演示真实文件工具及检查点；不具备自然语言理解能力。"""

    def invoke(self, messages, *, max_tokens=None, **_kwargs):
        start = max(i for i, message in enumerate(messages) if isinstance(message, HumanMessage))
        results = [message for message in messages[start + 1:] if isinstance(message, ToolMessage)]
        step = len(results)
        if step == 0:
            name, args = "list_files", {}
        elif step == 1:
            name, args = "search_files", {"query": "LangGraph"}
        elif step == 2:
            inventory = json.loads(results[0].content)
            if not inventory.get("files"):
                return AIMessage(content="离线演示：资料目录中没有可读取的笔记。")
            name, args = "read_file", {"path": inventory["files"][0]}
        elif step == 3:
            record = json.loads(results[-1].content)
            if not record.get("sources"):
                return AIMessage(content="离线演示：未取得可引用的正文，未保存笔记。")
            source = record["sources"][0]
            citation = f"[{source['path']}:L{source['start']}-L{source['end']}]"
            name, args = "create_note", {
                "filename": "离线流程演示.md",
                "content": "# 离线流程演示\n\n这是固定脚本摘录，不是 AI 总结。\n\n"
                + "\n".join(record["lines"]) + f"\n\n来源：{citation}\n",
            }
        else:
            saved = json.loads(results[-1].content)
            return AIMessage(content="离线演示已完成。文件工具结果：" + json.dumps(saved, ensure_ascii=False))
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": uuid4().hex, "type": "tool_call"}])


class AnthropicModel:
    """Use the native Anthropic API through the same LiteLLM adapter as PageIndex."""
    def __init__(self, settings, tools):
        self.settings, self.tools = settings, tools

    def invoke(self, messages, *, max_tokens=None, **_kwargs):
        import os
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm
        from langchain_core.messages import convert_to_openai_messages
        from langchain_core.utils.function_calling import convert_to_openai_tool
        from personal_workbench.app_settings import sdk_model
        result = litellm.completion(model=sdk_model(self.settings), api_key=self.settings.api_key,
                                   api_base=self.settings.base_url, timeout=self.settings.timeout,
                                   max_tokens=max_tokens or self.settings.max_tokens, num_retries=0,
                                   messages=convert_to_openai_messages(messages),
                                   **({"tools": [convert_to_openai_tool(t) for t in self.tools]} if self.tools else {}))
        message = result.choices[0].message
        calls = [{"name": c.function.name, "args": json.loads(c.function.arguments), "id": c.id}
                 for c in (message.tool_calls or [])]
        usage = result.usage
        details = {}
        if usage:
            cached = (_usage_int(getattr(usage, "cache_read_input_tokens", 0))
                      or _usage_int(getattr(usage, "prompt_cache_hit_tokens", 0)))
            cache_write = _usage_int(getattr(usage, "cache_creation_input_tokens", 0))
            if cached:
                details["cache_read"] = cached
            if cache_write:
                details["cache_creation"] = cache_write
        return AIMessage(content=message.content or "", tool_calls=calls,
                         usage_metadata={"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
                                         "total_tokens": usage.total_tokens,
                                         **({"input_token_details": details} if details else {})} if usage else None)
