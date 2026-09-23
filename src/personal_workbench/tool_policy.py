"""Versioned tool-effect declarations and the single side-effect gate.

Tool descriptions tell the model *how* to call a tool.  This module tells the
host *whether* it may execute the call, whether approval is required, and how
the resulting mutation is deduplicated.  Host policy never relies on model
text or on an MCP server's read-only hint.
"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


POLICY_SCHEMA_VERSION = 1


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = POLICY_SCHEMA_VERSION
    effect: Literal["read", "external_read", "local_write", "external_write"]
    approval: Literal["never", "explicit_intent", "on_conflict", "always"]
    idempotency: Literal["none", "receipt", "provider_key"]
    retry: Literal["model", "never"]
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)

    @model_validator(mode="after")
    def coherent(self):
        if self.effect in {"read", "external_read"}:
            if self.approval != "never" or self.idempotency != "none":
                raise ValueError("只读工具不能要求写入审批或写入幂等凭据。")
        if self.effect == "external_write":
            if self.approval != "always" or self.idempotency not in {"receipt", "provider_key"}:
                raise ValueError("外部写入必须逐次审批并使用幂等凭据。")
        if self.effect == "local_write":
            if self.approval not in {"explicit_intent", "on_conflict", "always"} or self.idempotency != "receipt":
                raise ValueError("本地写入必须受用户意图或审批约束并使用 receipt。")
        if self.effect.endswith("write") and self.retry != "never":
            raise ValueError("写入结果不能由执行层自动重试。")
        return self


def _policy(effect, approval="never", idempotency="none", retry="model", timeout=None):
    return ToolPolicy(effect=effect, approval=approval, idempotency=idempotency,
                      retry=retry, timeout_seconds=timeout)


BUILTIN_POLICIES = {
    "list_files": _policy("read"),
    "search_files": _policy("read"),
    "read_file": _policy("read"),
    "read_outline": _policy("read"),
    "read_page": _policy("read"),
    "read_tool_result": _policy("read"),
    "search_conversation": _policy("read"),
    "read_conversation_message": _policy("read"),
    "read_skill_resource": _policy("read"),
    "skills_list": _policy("read"),
    "skill_view": _policy("read"),
    "run_skill_script": _policy("read", timeout=65),
    "search_memory": _policy("read"),
    "web_search": _policy("external_read", timeout=20),
    "web_fetch": _policy("external_read", timeout=20),
    "paper_search": _policy("external_read", timeout=20),
    "jev_decide": _policy("external_read", timeout=15),
    "create_note": _policy("local_write", "on_conflict", "receipt", "never"),
    "create_artifact": _policy("local_write", "on_conflict", "receipt", "never"),
    "manage_memory": _policy("local_write", "explicit_intent", "receipt", "never"),
    # 只写入待审批建议，不会安装、启用或修改正式技能。
    "propose_skill": _policy("local_write", "explicit_intent", "receipt", "never"),
    "terminal": _policy("local_write", "explicit_intent", "receipt", "never", 300),
}


def connector_policy(ref):
    if ref.get("policy") == "read":
        return _policy("external_read", timeout=ref.get("timeout_seconds"))
    if ref.get("policy") == "confirm":
        idempotency = "provider_key" if ref.get("idempotency_parameter") else "receipt"
        return _policy("external_write", "always", idempotency, "never", ref.get("timeout_seconds"))
    raise ValueError("此连接器工具未获执行权限。")


def builtin_policy(name, timeout_seconds=None):
    try:
        policy = BUILTIN_POLICIES[name]
    except KeyError:
        raise ValueError(f"工具 {name} 没有副作用策略，已拒绝挂载。") from None
    if timeout_seconds is not None:
        policy = policy.model_copy(update={"timeout_seconds": timeout_seconds})
    return policy


def bind_tool_policy(tool, policy=None):
    """Attach a validated declaration to a LangChain tool."""
    ref = (tool.metadata or {}).get("mcp_ref")
    policy = policy or (connector_policy(ref) if ref else builtin_policy(tool.name))
    metadata = dict(tool.metadata or {})
    metadata["workbench_policy"] = policy.model_dump()
    tool.metadata = metadata
    return tool


def bind_tool_policies(tools):
    return [bind_tool_policy(tool) for tool in tools]


def policy_for_tool(tool):
    raw = (tool.metadata or {}).get("workbench_policy")
    if raw is None:
        return bind_tool_policy(tool).metadata["workbench_policy"]
    return ToolPolicy.model_validate(raw).model_dump()


def catalog_policy(tool, connector=None):
    """Build the same public declaration without constructing a live tool."""
    if connector is None:
        policy = builtin_policy(tool["id"])
    else:
        if tool.get("policy") == "disabled":
            return None
        policy = connector_policy({**tool, "timeout_seconds": connector.get("timeout")})
    return policy.model_dump()


def stable_action_id(thread_id, turn_id, tool_name, args):
    payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{thread_id}:{turn_id}:{payload}".encode()).hexdigest()


class ToolExecutionPolicy:
    """Common gate for approval, provider keys and durable external commits."""

    def __init__(self, thread_id, connectors=None):
        self.thread_id, self.connectors = thread_id, connectors

    def prepare(self, selected, call, turn_id, session_allowlist=()):
        policy = ToolPolicy.model_validate(policy_for_tool(selected))
        if selected.name == "terminal":
            from personal_workbench.terminal_tools import inspect_command
            inspection = inspect_command(str(call.get("args", {}).get("command", "")))
            if inspection["dangerous"] and inspection["pattern_key"] not in set(session_allowlist):
                return {"kind": "terminal", "args": dict(call.get("args", {})),
                        "inspection": inspection, "tool_policy": policy.model_dump()}
            return None
        if policy.effect != "external_write":
            return None
        ref = (selected.metadata or {}).get("mcp_ref")
        if ref is None or self.connectors is None:
            raise ValueError("外部写入工具缺少连接器执行上下文。")
        args = dict(call.get("args", {}))
        parameter = ref.get("idempotency_parameter")
        if policy.idempotency == "provider_key":
            if not parameter:
                raise ValueError("工具策略要求服务端幂等键，但连接器未配置对应参数。")
            args.pop(parameter, None)
            args[parameter] = stable_action_id(self.thread_id, turn_id, call["name"], args)
        self.connectors.validate(ref, args)
        return {"kind": "mcp", "ref": ref, "args": args, "tool_policy": policy.model_dump()}

    def approval_request(self, proposal):
        if proposal.get("kind") == "terminal":
            inspection = proposal["inspection"]
            return {"kind": "terminal", "command": inspection["command"],
                    "pattern_key": inspection["pattern_key"], "reasons": inspection["reasons"],
                    "message": "此命令可能修改本机环境或执行外部内容，请核对后决定。"}
        policy = ToolPolicy.model_validate(proposal.get("tool_policy") or connector_policy(proposal["ref"]))
        if policy.approval != "always":
            raise ValueError("此工具策略不允许进入外部写入审批。")
        ref = proposal["ref"]
        return {"kind": "mcp", "connector": ref["connector_name"], "tool": ref["name"],
                "arguments": proposal["args"],
                "execution_policy": policy.model_dump(),
                "message": "此工具可能修改外部数据，请核对目标和参数。批准仅适用于这次调用。"}

    def commit(self, proposal, call, turn_id):
        policy = ToolPolicy.model_validate(proposal.get("tool_policy") or connector_policy(proposal["ref"]))
        if policy.effect != "external_write" or policy.idempotency not in {"receipt", "provider_key"}:
            raise ValueError("工具策略不允许提交此外部操作。")
        if self.connectors is None:
            raise ValueError("连接器运行时不可用。")
        action_id = stable_action_id(self.thread_id, turn_id, call["name"], proposal["args"])
        return self.connectors.write_once(proposal["ref"], proposal["args"], action_id)
