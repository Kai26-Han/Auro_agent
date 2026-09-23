"""技能包静态安全扫描；只读文件内容，不执行任何技能代码。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


SCANNER_VERSION = "workbench-skill-guard-v1"


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    category: str
    description: str
    pattern: re.Pattern[str]


def _rule(rule_id: str, severity: str, category: str, description: str, pattern: str) -> Rule:
    return Rule(rule_id, severity, category, description, re.compile(pattern, re.I))


RULES = (
    _rule("prompt_override", "critical", "prompt_injection", "尝试覆盖或忽略上级指令",
          r"(?:ignore|disregard)\s+(?:all\s+|the\s+)?(?:previous|prior|system)\s+(?:instructions?|rules?)"),
    _rule("secret_exfiltration", "critical", "data_exfiltration", "可能把密钥或令牌发送到外部地址",
          r"(?:curl|wget|fetch\s*\(|requests\.(?:get|post)|httpx\.(?:get|post))[^\n]{0,180}(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"),
    _rule("credential_store", "high", "sensitive_access", "读取用户凭据目录或密钥文件",
          r"(?:\$HOME|~)/(?:\.ssh|\.aws|\.gnupg|\.kube|\.docker)|(?:cat|open\s*\()[^\n]{0,80}(?:\.env|credentials|\.netrc|\.npmrc)"),
    _rule("destructive_command", "critical", "destructive", "包含可能破坏系统或用户数据的命令",
          r"rm\s+-[a-z]*r[a-z]*f\s+(?:/(?!tmp(?:/|\b)|var/tmp(?:/|\b)|dev/shm(?:/|\b)|run(?:/|\b))\S*|\$HOME(?:/\S*)?|~(?:/\S*)?)|\bmkfs\b|\bdd\s+[^\n]*of=/dev/|>\s*/etc/"),
    _rule("reverse_shell", "critical", "network", "包含反向连接或外部隧道行为",
          r"/dev/tcp/|\bnc(?:at)?\s+-[a-z]*[lp]|\b(?:ngrok|localtunnel|serveo|cloudflared)\b"),
    _rule("dynamic_execution", "high", "execution", "通过动态解释或系统命令执行内容",
          r"\b(?:os\.system|os\.popen|subprocess\.(?:run|call|Popen)|child_process\.(?:exec|spawn)|eval|exec)\s*\("),
    _rule("download_execute", "critical", "execution", "下载内容后直接交给解释器执行",
          r"(?:curl|wget)[^\n|]{0,180}\|\s*(?:bash|sh|zsh|python|node|perl|ruby)\b"),
    _rule("persistence", "high", "persistence", "可能修改系统启动项或长期驻留配置",
          r"\bcrontab\b|launchctl\s+load|systemctl\s+enable|authorized_keys|/etc/sudoers"),
    _rule("agent_config_write", "high", "configuration", "要求修改 Agent 或工作台的全局指令配置",
          r"(?:write|edit|modify|update|append|overwrite)[^\n]{0,100}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.hermes/config\.yaml|\.codex/config)"),
    _rule("network_command", "medium", "network", "包含主动网络请求命令，需要确认用途",
          r"\b(?:curl|wget)\s+https?://|requests\.(?:get|post)\s*\(|httpx\.(?:get|post)\s*\("),
    _rule("secret_environment", "medium", "sensitive_access", "读取密钥类环境变量，需要确认使用范围",
          r"(?:os\.getenv|os\.environ\.get|process\.env)[^\n]{0,100}(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"),
)


TEXT_SCAN_LIMIT = 1024 * 1024
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".sh", ".bash", ".rb", ".html", ".css"}


def _excerpt(line: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 40)
    end = min(len(line), match.end() + 60)
    value = line[start:end].strip()
    return value[:220]


def scan_skill_files(files: dict[str, bytes], *, skill_name: str, source: dict) -> dict:
    findings: list[dict] = []
    scanned_files = 0
    for path, data in sorted(files.items()):
        if PurePosixPath(path).suffix.lower() not in TEXT_SUFFIXES or len(data) > TEXT_SCAN_LIMIT:
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            continue
        scanned_files += 1
        for line_number, line in enumerate(text.splitlines(), 1):
            if len(line) > 4000:
                line = line[:4000]
            for rule in RULES:
                match = rule.pattern.search(line)
                if match:
                    findings.append({
                        "rule_id": rule.id,
                        "severity": rule.severity,
                        "category": rule.category,
                        "description": rule.description,
                        "file": path,
                        "line": line_number,
                        "excerpt": _excerpt(line, match),
                    })
    severities = {item["severity"] for item in findings}
    verdict = "dangerous" if "critical" in severities else "caution" if severities else "safe"
    return {
        "scanner_version": SCANNER_VERSION,
        "skill_name": skill_name,
        "verdict": verdict,
        "scanned_files": scanned_files,
        "finding_count": len(findings),
        "findings": findings,
        "trust_level": source.get("trust_level", "community"),
    }


def normalize_source(source: dict | None, filename: str) -> dict:
    raw = source or {}
    kind = raw.get("kind") if raw.get("kind") in {"local", "example", "github", "url", "agent"} else "local"
    default_trust = "trusted" if kind == "example" else "agent-created" if kind == "agent" else "local" if kind == "local" else "community"
    identifier = str(raw.get("identifier") or filename)[:500]
    url = str(raw.get("url") or "")[:2000]
    normalized = {
        "kind": kind,
        "label": str(raw.get("label") or {"local": "本地文件", "example": "内置示例", "github": "GitHub", "url": "直接链接", "agent":"Agent 建议"}[kind])[:80],
        "identifier": identifier,
        "url": url,
        "trust_level": raw.get("trust_level") if raw.get("trust_level") in {"builtin", "trusted", "local", "community", "agent-created"} else default_trust,
    }
    if isinstance(raw.get("revision"), str): normalized["revision"] = raw["revision"][:200]
    github = raw.get("github")
    if isinstance(github, dict):
        normalized["github"] = {key:str(github.get(key) or "")[:500] for key in ("owner","repo","ref","path")}
    return normalized
