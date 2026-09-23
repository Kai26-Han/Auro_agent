"""Hermes-style local terminal tool with host-owned dangerous-command checks."""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field


MAX_COMMAND_CHARS = 12_000
MAX_OUTPUT_BYTES = 96 * 1024

SKILL_ID_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SKILLHUB_ACTIONS = {"install", "update", "upgrade", "uninstall", "remove"}
SKILLHUB_VALUE_OPTIONS = {
    "--dir", "--namespace", "--files-base-uri", "--download-url-template",
    "--primary-download-url-template", "--search-url", "--search-limit",
    "--search-timeout", "--secret", "--timeout", "--index",
}


class TerminalInput(BaseModel):
    command: str = Field(description="要在工作台项目目录执行的 Shell 命令。")
    timeout: int = Field(default=60, ge=1, le=300, description="超时秒数。")


RULES = (
    ("pipe_to_shell", "下载内容后直接交给 Shell 执行",
     re.compile(r"(?:curl|wget)[^\n|]{0,400}\|\s*(?:ba)?sh\b|\biex\s*\(\s*irm\b", re.I)),
    ("recursive_delete", "递归删除文件或目录",
     re.compile(r"(?:^|[;&|]\s*)rm\s+-[^\n;&|]*[rf][^\n;&|]*\s", re.I)),
    ("privilege_escalation", "使用管理员权限",
     re.compile(r"(?:^|[;&|]\s*)sudo\b|\bsu\s+-", re.I)),
    ("package_install", "安装或卸载本机软件",
     re.compile(r"(?:^|[;&|]\s*)(?:brew\s+(?:install|uninstall)|pipx?\s+(?:install|uninstall)|uv\s+tool\s+(?:install|uninstall)|npm\s+(?:install|uninstall)(?:\s+-g|\s+--global)|apt(?:-get)?\s+(?:install|remove)|dnf\s+(?:install|remove)|yum\s+(?:install|remove))\b", re.I)),
    ("skill_install", "安装、更新或卸载 Agent Skill",
     re.compile(r"(?:^|[;&|]\s*)skillhub\b[^\n;&|]{0,400}\b(?:install|update|upgrade|uninstall|remove)\b", re.I)),
    ("system_configuration", "修改系统配置或启动项",
     re.compile(r"(?:>|>>|tee)\s*/(?:etc|Library|System)/|\b(?:launchctl|systemctl|crontab)\b", re.I)),
    ("credential_write", "修改凭据、SSH 或 Agent 全局配置",
     re.compile(r"(?:>|>>|tee|cp|mv|install)\s+[^\n;&|]*(?:\.ssh|\.aws|\.gnupg|\.netrc|credentials|authorized_keys|\.codex|\.hermes)", re.I)),
    ("process_control", "结束进程或关闭服务",
     re.compile(r"(?:^|[;&|]\s*)(?:kill(?:all)?|pkill)\b|\b(?:shutdown|reboot|halt)\b", re.I)),
    ("disk_or_device", "写入磁盘或设备",
     re.compile(r"\bmkfs\b|\bdd\s+[^\n]*\bof=/dev/|\bdiskutil\s+(?:erase|partition)", re.I)),
    ("shell_persistence", "修改 Shell 启动配置",
     re.compile(r"(?:>|>>|tee)\s+[^\n;&|]*(?:\.zshrc|\.bashrc|\.bash_profile|\.profile)\b", re.I)),
)


def inspect_command(command: str) -> dict:
    """Return deterministic command risk metadata without executing it."""
    value = str(command or "").strip()
    if not value:
        raise ValueError("终端命令不能为空。")
    if len(value) > MAX_COMMAND_CHARS or "\x00" in value:
        raise ValueError("终端命令过长或包含无效字符。")
    matches = [{"key": key, "description": description}
               for key, description, pattern in RULES if pattern.search(value)]
    keys = sorted(item["key"] for item in matches)
    return {
        "command": value,
        "dangerous": bool(matches),
        "reasons": matches,
        "pattern_key": "+".join(keys) if keys else "safe",
    }


def parse_skillhub_operation(command: str) -> dict | None:
    """Parse one targeted SkillHub operation without depending on option order."""
    try:
        tokens = shlex.split(command or "", posix=True)
    except ValueError:
        return None
    executable = next((index for index, token in enumerate(tokens)
                       if Path(token).name == "skillhub"), None)
    if executable is None:
        return None
    action_index = next((index for index in range(executable + 1, len(tokens))
                         if tokens[index].lower() in SKILLHUB_ACTIONS), None)
    if action_index is None:
        return None
    action = tokens[action_index].lower()
    namespace = target_dir = None
    force = False
    requested = None
    index = executable + 1
    while index < len(tokens):
        token = tokens[index]
        if token in {";", "&&", "||", "|"}:
            break
        if index == action_index:
            index += 1
            continue
        option, separator, inline = token.partition("=")
        if option in SKILLHUB_VALUE_OPTIONS:
            if separator:
                value = inline
            elif index + 1 < len(tokens):
                index += 1
                value = tokens[index]
            else:
                return None
            if option == "--namespace":
                namespace = value.strip("'\"").lstrip("@")
            elif option == "--dir":
                target_dir = value
            index += 1
            continue
        if token == "--force":
            force = True
        elif token.startswith("-"):
            pass
        elif index > action_index and requested is None:
            requested = token.strip("'\"")
        index += 1
    # `skillhub upgrade` may target every locked skill.  It has no single
    # package identity and therefore cannot produce a package receipt.
    if not requested:
        return None
    slug = requested
    if requested.startswith("@") and "/" in requested:
        namespace, slug = requested[1:].split("/", 1)
    if not SKILL_ID_PART.fullmatch(slug) or (namespace and not SKILL_ID_PART.fullmatch(namespace)):
        return None
    canonical = f"@{namespace}/{slug}" if namespace else slug
    return {"action": action, "requested": requested, "namespace": namespace,
            "slug": slug, "canonical": canonical, "target_dir": target_dir,
            "force": force}


def _sync_skills(settings) -> dict:
    from personal_workbench.external_skills import sync_external_skills
    from personal_workbench.skill_store import SkillStore
    return sync_external_skills(SkillStore(settings))


def _matching_skill(report: dict, operation: dict) -> dict | None:
    expected = f"@{operation['namespace']}/{operation['slug']}" if operation.get("namespace") else operation["slug"]
    return next((item for item in report.get("skills", [])
                 if (item.get("source") or {}).get("identifier") == expected), None)


def _matching_sync_errors(report: dict, operation: dict) -> list[str]:
    if report.get("error"):
        return [str(report["error"])]
    expected = operation["canonical"]
    return [str(item.get("error") or "").strip() for item in report.get("errors", [])
            if item.get("path") in {expected, operation["slug"]} and item.get("error")]


def _brief_skill_function(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    sentence = re.split(r"(?<=[。！？])\s*", text, maxsplit=1)[0]
    return sentence if len(sentence) <= 240 else sentence[:237].rstrip() + "..."


def _skill_receipt(operation: dict, report: dict, exit_code: int, *, no_op: bool = False) -> dict:
    skill = _matching_skill(report, operation)
    sync_errors = _matching_sync_errors(report, operation)
    catalog_name = (skill or {}).get("name", operation["slug"])
    changed = "updated" if catalog_name in report.get("updated", []) else (
        "installed" if catalog_name in report.get("imported", []) else "already_installed"
    )
    if operation["action"] in {"uninstall", "remove"}:
        package_status = "removed" if exit_code == 0 and (skill is None or skill.get("archived")) else "failed"
    elif no_op and skill:
        package_status = "already_installed"
    elif exit_code == 0 and skill and not sync_errors:
        package_status = "already_installed" if no_op else changed
    else:
        package_status = "failed"
    runtime_status = (skill or {}).get("runtime_status", "unavailable")
    runtime_ready = bool((skill or {}).get("runtime_ready"))
    limitations = list((skill or {}).get("limitations") or [])
    if package_status == "failed":
        connectivity_status = "not_run"
        connectivity_detail = "安装未完成，未执行功能连通测试。"
    elif runtime_status in {"configuration_required", "disabled", "incompatible", "unavailable"}:
        connectivity_status = "blocked"
        connectivity_detail = "运行前提尚未满足，无法执行功能连通测试。"
    else:
        # Installation verifies package discovery and catalog registration.  A
        # real functional probe is skill-specific and must never be invented.
        connectivity_status = "not_run"
        connectivity_detail = "已通过工作台登记检查；本次未执行代表性功能任务。"
    error = "；".join(sync_errors)
    if package_status == "failed":
        message = f"技能 {operation['canonical']} 安装失败。"
        if not error and exit_code == 0 and not skill:
            error = "安装命令已完成，但工作台同步后未找到目标技能。"
    elif package_status == "already_installed":
        message = f"技能 {operation['canonical']} 已安装。"
    elif package_status == "updated":
        message = f"技能 {operation['canonical']} 已更新。"
    elif package_status == "removed":
        message = f"技能 {operation['canonical']} 已移除。"
    else:
        message = f"技能 {operation['canonical']} 已安装。"
    if package_status != "failed" and not runtime_ready:
        if runtime_status == "configuration_required":
            message += " 还需要配置运行环境。"
        elif runtime_status == "limited":
            message += " 可使用，但部分能力受限。"
        else:
            message += " 当前不可完整使用。"
    receipt = {
        "schema_version": 1,
        "kind": "skill_install",
        "package": operation["canonical"],
        "requested_action": operation["action"],
        "package_status": package_status,
        "runtime_status": runtime_status,
        "runtime_ready": runtime_ready,
        "success": package_status != "failed",
        "function": _brief_skill_function((skill or {}).get("description", "")),
        "version": (skill or {}).get("version", ""),
        "connectivity_test_status": connectivity_status,
        "connectivity_test_detail": connectivity_detail,
        "limitations": limitations,
        "missing_runtime": list((skill or {}).get("missing_runtime") or []),
        "message": message,
    }
    if error:
        receipt["error"] = error
    return receipt


def _uses_external_skill_dir(operation: dict, external: Path) -> bool:
    value = operation.get("target_dir")
    if value in {"$WORKBENCH_EXTERNAL_SKILLS_DIR", "${WORKBENCH_EXTERNAL_SKILLS_DIR}"}:
        return True
    if not value:
        return False
    try:
        return Path(os.path.expandvars(os.path.expanduser(value))).resolve() == external
    except (OSError, RuntimeError):
        return False


def _redact(value: str) -> str:
    patterns = (
        (r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)\b((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s'\"]+", r"\1[REDACTED]"),
        (r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def _terminal_environment(external: Path) -> dict[str, str]:
    """Build a subprocess environment that works with macOS CLI tooling.

    The desktop host currently exports C.UTF-8. macOS does not provide that
    locale, and the python.org runtime may also have no default CA bundle.
    Both problems surface in third-party installers as misleading warnings or
    generic network failures.
    """
    env = dict(os.environ)
    env["WORKBENCH_EXTERNAL_SKILLS_DIR"] = str(external)
    if sys.platform == "darwin":
        if env.get("LANG", "") in {"", "C.UTF-8"}:
            env["LANG"] = "en_US.UTF-8"
        if env.get("LC_ALL", "") in {"", "C.UTF-8"}:
            env["LC_ALL"] = "en_US.UTF-8"
        system_ca = Path("/etc/ssl/cert.pem")
        if not env.get("SSL_CERT_FILE") and system_ca.is_file():
            env["SSL_CERT_FILE"] = str(system_ca)
    return env


def build_terminal_tool(settings, stop_event=None):
    project = settings.project_dir.resolve()
    external = settings.data_dir.resolve() / "external-skills"
    external.mkdir(parents=True, exist_ok=True, mode=0o700)

    @tool(args_schema=TerminalInput)
    def terminal(command: str, timeout: int = 60) -> dict:
        """Run a shell command on the user's computer from the workbench project directory. Use it for explicit computer, development, CLI, or skill-install tasks. Dangerous commands pause for user approval before this function runs. Skills installed by a CLI must use $WORKBENCH_EXTERNAL_SKILLS_DIR as their --dir target. Skill installs return a host-generated operation_receipt: package_status reports installation and runtime_status reports actual readiness. Treat an existing matching package as a successful no-op; never describe limited/configuration_required as fully usable."""
        inspection = inspect_command(command)
        skill_operation = parse_skillhub_operation(inspection["command"])
        # 外部技能包是待验证输入，不能借通用终端直接执行或读取其中的
        # scripts/bin/tools。安装和更新只能通过可识别的 SkillHub 操作，
        # 随后由宿主重新导入并生成 readiness receipt。
        external_markers = (
            "$WORKBENCH_EXTERNAL_SKILLS_DIR", "${WORKBENCH_EXTERNAL_SKILLS_DIR}",
            str(external), "external-skills/", "external-skills\\",
        )
        if not skill_operation and any(marker in inspection["command"] for marker in external_markers):
            return {
                "exit_code": 2, "stdout": "",
                "stderr": "不能通过通用终端直接访问或执行外部技能目录；请使用技能广场或 SkillHub 安装流程。",
                "truncated": False, "duration_ms": 0, "cwd": str(project),
                "policy_blocked": True,
            }
        before = None
        if skill_operation and skill_operation["action"] in {"install", "update", "upgrade"}:
            before = _sync_skills(settings)
        if skill_operation and skill_operation["action"] == "install":
            # `skillhub install` returns an error when the target already
            # exists. Treat the same installed package as an idempotent no-op
            # instead of running a command that is guaranteed to fail.
            target = external / (("@" + skill_operation["namespace"]) if skill_operation["namespace"] else "") / skill_operation["slug"]
            if target.is_dir() and not target.is_symlink() and not skill_operation.get("force"):
                if _matching_skill(before, skill_operation):
                    receipt = _skill_receipt(skill_operation, before, 0, no_op=True)
                    return {
                        "exit_code": 0, "stdout": receipt["message"], "stderr": "",
                        "truncated": False, "duration_ms": 0, "cwd": str(project),
                        "no_op": True, "skill_sync": before, "operation_receipt": receipt,
                    }
                receipt = _skill_receipt(skill_operation, before, 1)
                receipt["error"] = receipt.get("error") or (
                    "目标目录已存在，但工作台未能导入该技能。"
                    "请先根据 skill_sync 中的错误修复技能包，不要重复安装。"
                )
                return {
                    "exit_code": 1, "stdout": "", "stderr": receipt["error"],
                    "truncated": False, "duration_ms": 0, "cwd": str(project),
                    "no_op": True, "skill_sync": before, "operation_receipt": receipt,
                }
        if (skill_operation and skill_operation["action"] in {"install", "update", "upgrade"}
                and not _uses_external_skill_dir(skill_operation, external)):
            receipt = _skill_receipt(skill_operation, before or {}, 2)
            receipt["error"] = (
                "SkillHub 安装或升级必须指定 --dir \"$WORKBENCH_EXTERNAL_SKILLS_DIR\"，"
                "否则工作台无法发现和验证该技能。"
            )
            return {
                "exit_code": 2, "stdout": "", "stderr": receipt["error"],
                "truncated": False, "duration_ms": 0, "cwd": str(project),
                "skill_sync": before or {}, "operation_receipt": receipt,
            }
        timeout = max(1, min(int(timeout), 300))
        env = _terminal_environment(external)
        started = time.monotonic()
        process = subprocess.Popen(
            ["/bin/zsh", "-lc", inspection["command"]], cwd=project,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        def terminate():
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: return
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
        try:
            if stop_event is not None and stop_event.is_set():
                terminate()
                return {"exit_code": -15, "stdout": "", "stderr": "任务已停止。", "stopped": True}
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate()
            out, err = process.communicate()
            return {"exit_code": -15, "stdout": _redact(out[-MAX_OUTPUT_BYTES:].decode("utf-8", "replace")),
                    "stderr": _redact((err[-MAX_OUTPUT_BYTES:] + b"\nCommand timed out.").decode("utf-8", "replace")),
                    "timed_out": True}
        finally:
            if process.poll() is None: terminate()
        result = {
            "exit_code": int(process.returncode or 0),
            "stdout": _redact(out[-MAX_OUTPUT_BYTES:].decode("utf-8", "replace")),
            "stderr": _redact(err[-MAX_OUTPUT_BYTES:].decode("utf-8", "replace")),
            "truncated": len(out) > MAX_OUTPUT_BYTES or len(err) > MAX_OUTPUT_BYTES,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "cwd": str(project),
        }
        try:
            result["skill_sync"] = _sync_skills(settings)
        except Exception as exc:
            result["skill_sync"] = {"error": str(exc)}
        if skill_operation:
            result["operation_receipt"] = _skill_receipt(
                skill_operation, result["skill_sync"], result["exit_code"]
            )
            if not result["operation_receipt"]["success"]:
                cli_error = result["stderr"][-1000:].strip()
                if cli_error:
                    existing = result["operation_receipt"].get("error", "")
                    result["operation_receipt"]["error"] = (
                        existing + ("\n" if existing else "") + cli_error
                    )
        return result

    return terminal


def tool_catalog():
    return {"id": "terminal", "name": "本地终端", "description": "执行本机 Shell 命令；危险命令必须确认。",
            "applicability": "明确的电脑操作、开发与技能安装任务。", "source": "builtin",
            "availability": "contextual", "schema": TerminalInput.model_json_schema()}
