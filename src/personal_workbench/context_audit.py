"""Read-only health checks for the unified conversation context.

The audit deliberately returns metadata only.  It validates persisted source
links and tool protocol pairs without copying prompts, messages or tool bodies
into a second observability store.
"""

from __future__ import annotations

from personal_workbench.context_engine import COMPRESSION_SCHEMA, ContextEngine, usable_messages
from personal_workbench.tool_results import ToolResultStore


CHECK_LABELS = {
    "budget": "上下文预算",
    "required_layers": "必需上下文层",
    "compression": "压缩来源完整性",
    "tool_protocol": "工具调用协议",
    "tool_results": "大型结果引用",
    "exclusions": "排除来源",
}


def _check(check_id: str, status: str, message: str) -> dict:
    return {"id": check_id, "label": CHECK_LABELS[check_id], "status": status, "message": message}


class ContextAudit:
    """Build a stable, content-free diagnostic view of one checkpoint."""

    schema_version = "c6-v1"

    def __init__(self, settings, db):
        self.engine = ContextEngine(settings)
        self.results = ToolResultStore(db)

    @staticmethod
    def _tool_protocol(messages, pending_ids: set[str]):
        calls: dict[str, str] = {}
        results: dict[str, int] = {}
        for message in messages:
            for call in getattr(message, "tool_calls", []) or []:
                call_id = str(call.get("id", ""))
                if call_id:
                    calls[call_id] = str(call.get("name", "tool"))
            if message.type == "tool":
                call_id = str(getattr(message, "tool_call_id", ""))
                if call_id:
                    results[call_id] = results.get(call_id, 0) + 1
        missing = sorted(set(calls) - set(results) - pending_ids)
        duplicate = sorted(key for key, count in results.items() if count != 1)
        orphan = sorted(set(results) - set(calls))
        return calls, results, missing, duplicate, orphan

    def build(self, thread_id: str, state: dict) -> dict:
        messages = list(state.get("messages", []))
        usable = usable_messages(state)
        budget = state.get("context_budget", {}) or {}
        layers = list(budget.get("layers", []) or [])
        compression = state.get("context_compression", {}) or {}
        summary = state.get("running_summary", {}) or {}
        refs = list(state.get("tool_result_refs", []) or [])
        checks = []

        capacity = int(budget.get("input_capacity", 0) or 0)
        estimated = int(budget.get("estimated_input", 0) or 0)
        if not budget:
            checks.append(_check("budget", "not_applicable", "尚无回答请求的预算记录。"))
        elif "input_capacity" not in budget:
            checks.append(_check("budget", "not_applicable", "旧会话没有完整容量字段，将在下一轮重新计算。"))
        elif estimated <= capacity:
            checks.append(_check("budget", "pass", "最近一次输入未超过可用容量。"))
        else:
            checks.append(_check("budget", "fail", "最近一次输入超过可用容量。"))

        required_truncated = [row.get("label") or row.get("id") for row in layers
                              if row.get("required") and (row.get("truncated") or row.get("status") == "omitted")]
        if required_truncated:
            checks.append(_check("required_layers", "fail", "必需层被截断或遗漏，请重新整理上下文。"))
        elif layers:
            checks.append(_check("required_layers", "pass", "必需层均已纳入最近一次请求。"))
        else:
            checks.append(_check("required_layers", "not_applicable", "尚无分层清单。"))

        expected = len(summary.get("source_ids", []) or [])
        if not compression and not expected:
            checks.append(_check("compression", "not_applicable", "当前会话尚未触发历史压缩。"))
            valid_boundary = 0
        else:
            normalized, valid_boundary = self.engine.normalize_compression(usable, state)
            valid_schema = normalized.get("schema_version") == COMPRESSION_SCHEMA
            if valid_schema and valid_boundary == expected and valid_boundary > 0:
                checks.append(_check("compression", "pass", "摘要连续覆盖有效原文前缀，来源哈希一致。"))
            else:
                checks.append(_check("compression", "fail", "压缩来源已失效，后续请求应重新生成摘要。"))

        pending_ids = {str(call.get("id", "")) for call in state.get("queue", []) or [] if call.get("id")}
        # Excluded turns remain in the raw transcript and may represent a
        # deliberately abandoned tool call.  Only the usable conversation is
        # required to satisfy the live model protocol.
        calls, results, missing, duplicate, orphan = self._tool_protocol(usable, pending_ids)
        if missing or duplicate or orphan:
            checks.append(_check("tool_protocol", "fail", "工具调用与结果消息不是一一对应。"))
        elif pending_ids:
            checks.append(_check("tool_protocol", "warning", "有工具调用仍在等待执行或确认。"))
        elif calls:
            checks.append(_check("tool_protocol", "pass", "所有已完成工具调用都有且仅有一条结果。"))
        else:
            checks.append(_check("tool_protocol", "not_applicable", "当前会话尚未调用工具。"))

        stored = {row["id"]: row for row in self.results.list(thread_id)}
        missing_refs = []
        mismatched_refs = []
        for ref in refs:
            result_id = ref.get("result_id")
            row = stored.get(result_id)
            if row is None:
                missing_refs.append(result_id)
            elif any(ref.get(key) not in (None, row.get(key)) for key in ("sha256", "bytes", "characters")):
                mismatched_refs.append(result_id)
        if missing_refs or mismatched_refs:
            checks.append(_check("tool_results", "fail", "大型结果引用缺失或元数据不一致。"))
        elif refs:
            checks.append(_check("tool_results", "pass", "大型结果均可在当前会话的本地仓库中定位。"))
        else:
            checks.append(_check("tool_results", "not_applicable", "当前会话没有外置的大型工具结果。"))

        human_ids = {str(message.id) for message in messages if message.type == "human"}
        excluded = [str(value) for value in state.get("working_excluded_turns", []) or []]
        invalid_exclusions = [value for value in excluded if value not in human_ids]
        if invalid_exclusions:
            checks.append(_check("exclusions", "fail", "存在无法对应到用户消息的排除来源。"))
        elif excluded:
            checks.append(_check("exclusions", "pass", "已排除轮次仍保留原文，并已从有效上下文移除。"))
        else:
            checks.append(_check("exclusions", "not_applicable", "当前会话没有排除来源。"))

        applicable = [row for row in checks if row["status"] != "not_applicable"]
        score = 100 if not applicable else round(100 * sum(
            1 if row["status"] == "pass" else .5 if row["status"] == "warning" else 0
            for row in applicable
        ) / len(applicable))
        status = ("error" if any(row["status"] == "fail" for row in checks)
                  else "warning" if any(row["status"] == "warning" for row in checks)
                  else "empty" if not budget and not messages else "healthy")
        search_names = {"search_conversation", "read_conversation_message"}
        history_searches = sum(name == "search_conversation" for name in calls.values())
        history_reads = sum(name == "read_conversation_message" for name in calls.values())
        metrics = {
            "context_window": int(budget.get("context_window", 0) or 0),
            "capacity_known": "input_capacity" in budget,
            "input_capacity": capacity,
            "estimated_input": estimated,
            "usage_percent": round(100 * estimated / capacity, 1) if capacity else 0,
            "unallocated_tokens": int(budget.get("unallocated_tokens", 0) or 0),
            "raw_messages": len(messages),
            "usable_messages": len(usable),
            "compressed_messages": expected,
            "level1_segments": len(compression.get("segments", []) or []),
            "level2_core": bool(compression.get("core")),
            "externalized_results": len(refs),
            "history_searches": history_searches,
            "history_reads": history_reads,
            "history_tool_calls": sum(name in search_names for name in calls.values()),
            "excluded_turns": len(excluded),
            "model_calls": int(state.get("model_calls", 0) or 0),
            "tool_calls": int(state.get("tool_calls", 0) or 0),
            "usage_tokens": int(state.get("usage_tokens", 0) or 0),
            "usage_unknown": bool(state.get("usage_unknown", False)),
        }
        return {
            "schema_version": self.schema_version,
            "status": status,
            "score": score,
            "checks": checks,
            "metrics": metrics,
            "layers": layers,
            "compression": budget.get("compression", {}) or {
                "schema_version": compression.get("schema_version"),
                "compressed_messages": valid_boundary,
                "level1_segments": len(compression.get("segments", []) or []),
                "level2_core": bool(compression.get("core")),
            },
            "tool_results": refs,
        }
