"""Internal resource policy for local-first execution.

The product UI deals in tasks and queue states.  Worker counts stay an
implementation detail because a remote chat request and a local Ollama run do
not have comparable resource costs.
"""

import os
import threading
from urllib.parse import urlsplit


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def local_model(model) -> bool:
    """Return whether a frozen model config executes on this computer."""
    if not model:
        return False
    host = urlsplit(str(model.get("base_url") or "")).hostname
    return model.get("provider") == "ollama" or host in LOCAL_HOSTS


def physical_memory_bytes() -> int | None:
    """Best-effort memory detection without a platform-specific dependency."""
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def automatic_chat_capacity() -> int:
    """Choose a conservative desktop capacity; individual lanes limit further."""
    cores = os.cpu_count() or 4
    memory = physical_memory_bytes()
    if cores <= 4 or (memory is not None and memory <= 8 * 1024**3):
        return 2
    return 4


def execution_lane(snapshot: dict) -> str:
    return "local_model" if local_model(snapshot.get("model") or {}) else "remote_model"


def lane_limit(lane: str, total_capacity: int) -> int:
    # Local generation is deliberately serial. Ollama/Metal may otherwise
    # allocate competing model contexts and make every conversation slower.
    return 1 if lane == "local_model" else total_capacity


def queue_reason(lane: str, lane_busy: bool) -> str:
    if lane == "local_model" and lane_busy:
        return "正在等待本地模型空闲，其他页面和会话仍可使用。"
    return "任务已进入队列，系统会在资源可用时自动开始。"


class LimitedModel:
    """Limit actual model calls, including parallel stages inside one workflow."""

    def __init__(self, model, gate):
        self.model, self.gate = model, gate

    def invoke(self, messages, *args, **kwargs):
        with self.gate:
            return self.model.invoke(messages, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.model, name)


_MODEL_GATES = {
    "local_model": threading.BoundedSemaphore(1),
    "remote_model": threading.BoundedSemaphore(automatic_chat_capacity()),
}


def limit_model(model, settings):
    """Apply process-wide provider pressure control without exposing a UI knob."""
    lane = execution_lane({"model": {
        "provider": getattr(settings, "provider", ""),
        "base_url": getattr(settings, "base_url", ""),
    }})
    return LimitedModel(model, _MODEL_GATES[lane])
