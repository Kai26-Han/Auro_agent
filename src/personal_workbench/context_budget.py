"""Shared byte-budget primitives; no engine or model dependencies."""
import json

def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def size(text):
    return len(str(text).encode("utf-8"))


def count(messages):
    return sum(16 + size(m.content) + (size(dump(m.tool_calls)) if getattr(m, "tool_calls", None) else 0)
               + size(getattr(m, "tool_call_id", "")) + size(getattr(m, "name", "") or "") for m in messages)


def excerpt(text, limit):
    data = str(text).encode("utf-8")
    if len(data) <= limit:
        return str(text)
    marker = "\n[上下文节选：中间内容省略，完整结果保留在原始消息；需要细节请重新读取来源。]\n"
    keep = max(0, (limit - size(marker)) // 2)
    return data[:keep].decode("utf-8", "ignore") + marker + data[-keep:].decode("utf-8", "ignore") if keep else marker


class ContextLimit(ValueError):
    pass
