"""Host-owned runtime readiness for installed Agent Skills."""

from __future__ import annotations

import re


RUNTIME_REF = re.compile(
    r"(?<![\w./-])(?:\{skill_base\}/|\$SKILL_DIR/)?"
    r"((?:scripts|bin|tools)/[A-Za-z0-9_.\-/]+\.(?:py|js|cjs|mjs|ts|sh|bash|rb|lua|r))(?![\w/-])",
    re.I,
)


def skill_readiness(item: dict) -> dict:
    """Separate catalog installation from the ability to complete the skill.

    External packages may contain scripts that the immutable skill importer
    deliberately omits.  If the skill body names one of those scripts, the
    package is installed but its primary runtime is not ready.
    """
    enabled = bool(item.get("enabled") and not item.get("archived") and item.get("compatible", True))
    omitted = set(item.get("omitted_files") or [])
    referenced = set(RUNTIME_REF.findall(item.get("body") or ""))
    missing_runtime = sorted(omitted & referenced)
    declared_scripts = list(item.get("scripts") or [])
    scripts_disabled = bool(declared_scripts and not item.get("scripts_enabled"))
    limitations = list(item.get("limitations") or [])
    if missing_runtime:
        detail = "技能说明依赖随包脚本，但当前未导入执行环境：" + "、".join(missing_runtime)
        if detail not in limitations:
            limitations.append(detail)
    if scripts_disabled:
        detail = "技能包含声明式运行脚本，但尚未授权在独立沙箱中执行。"
        if detail not in limitations:
            limitations.append(detail)
    if not item.get("compatible", True):
        runtime_status = "incompatible"
    elif not enabled:
        runtime_status = "disabled"
    elif missing_runtime or scripts_disabled:
        runtime_status = "configuration_required"
    elif item.get("support_level") == "partial":
        runtime_status = "limited"
    else:
        runtime_status = "ready"
    runtime_extensions = {path.rsplit('.', 1)[-1].casefold() for path in (referenced | set(missing_runtime))}
    if declared_scripts or referenced:
        kind = "runtime_app"
    elif item.get("connector_dependencies"):
        kind = "remote_service"
    elif (item.get("required_tools") or item.get("requires_knowledge_base")
          or (item.get("source") is None and item.get("allowed_tools"))):
        kind = "native"
    else:
        kind = "playbook"
    declared_engines = {script.get('runtime','python') for script in declared_scripts}
    if 'python' in declared_engines or runtime_extensions & {'py'}:
        runtime_engine = "python"
    elif 'node' in declared_engines:
        runtime_engine = "node"
    elif runtime_extensions & {'js', 'cjs', 'mjs', 'ts'}:
        runtime_engine = "node"
    elif runtime_extensions & {'sh', 'bash'}:
        runtime_engine = "shell"
    else:
        runtime_engine = "remote" if kind == "remote_service" else "none"
    return {
        "installed": True,
        "runtime_ready": runtime_status in {"ready", "limited"},
        "runtime_status": runtime_status,
        "missing_runtime": missing_runtime,
        "limitations": limitations,
        "kind": kind,
        "runtime_engine": runtime_engine,
    }
