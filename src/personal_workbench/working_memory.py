"""Compatibility exports for the pre-C1 working-memory module.

New code should import :mod:`personal_workbench.context_engine`. Keeping these
names avoids breaking local extensions and saved workflows.
"""
from personal_workbench.context_budget import ContextLimit, count, dump, excerpt, size
from personal_workbench.context_engine import (
    ENGINE_ID,
    ContextEngine,
    ExclusionInput,
    RebuildInput,
    TaskEdit,
    TaskFields,
    WorkingMemory,
    stamp,
    usable_messages,
)
from personal_workbench.context_compiler import ContextCompiler, ContextInputs

__all__ = [
    "ENGINE_ID", "ContextEngine", "WorkingMemory", "TaskFields", "TaskEdit",
    "ExclusionInput", "RebuildInput", "usable_messages", "ContextLimit",
    "count", "dump", "excerpt", "size", "stamp", "ContextCompiler", "ContextInputs",
]
