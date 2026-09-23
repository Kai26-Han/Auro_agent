"""能力目录和统一生命周期；平台不解释能力内部的 Agent 或工作流。"""

from dataclasses import dataclass
from typing import Any, Callable, Protocol

DEFAULT_CAPABILITY = "workbench-assistant"
DEFAULT_VERSION = "1.0.0"


class RunStopped(InterruptedError):
    """运行实现响应平台取消信号，保留自己的恢复状态。"""


@dataclass
class PreparedRun:
    request: dict
    snapshot: dict
    runtime: Any  # 仅驻留内存，含模型认证；禁止序列化。


class Capability(Protocol):
    id: str
    version: str

    def describe(self) -> dict: ...
    def prepare(self, request: dict, snapshot: dict | None) -> PreparedRun: ...
    def execute(self, prepared: PreparedRun, stop, emit: Callable) -> dict: ...
    def inspect(self, thread_id: str) -> dict: ...
    def abandon(self, thread_id: str) -> dict: ...
    def supersede(self, thread_id: str) -> dict: ...


class Registry:
    def __init__(self, capabilities=()):
        self._entries = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: Capability):
        key = (capability.id, capability.version)
        if key in self._entries:
            raise ValueError("能力版本已注册。")
        self._entries[key] = capability

    def resolve(self, capability_id=DEFAULT_CAPABILITY, version=DEFAULT_VERSION) -> Capability:
        try:
            return self._entries[(capability_id, version)]
        except KeyError:
            raise ValueError("此会话绑定的能力版本不可用，请恢复对应版本。") from None

    def catalog(self):
        return [entry.describe() for entry in self._entries.values()]
