"""Discover CLI-installed SKILL.md packages and import immutable snapshots."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from personal_workbench.file_tools import TOOL_INFO
from personal_workbench.skill_package import TEXT_LIMIT, TEXT_SUFFIXES
from personal_workbench.skill_readiness import skill_readiness


def _declared_script_paths(folder: Path) -> set[str]:
    """Read only the host extension manifest; never infer executable files.

    External packages remain playbooks unless they explicitly opt into the
    Workbench runtime contract.  The normal package parser performs the full
    schema, path, dependency and security validation after these bytes are
    staged into the immutable import archive.
    """
    manifest = folder / "workbench.json"
    if not manifest.is_file() or manifest.is_symlink():
        return set()
    try:
        value = json.loads(manifest.read_text("utf-8"))
    except (ValueError, UnicodeError, OSError):
        return set()
    scripts = value.get("scripts", []) if isinstance(value, dict) else []
    if not isinstance(scripts, list):
        return set()
    declared = {entry.get("path") for entry in scripts
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    if declared:
        declared.update(str(path.relative_to(folder)) for path in (folder / 'scripts').rglob('*')
                        if path.is_file() and path.suffix.lower() in {'.py','.js','.cjs','.mjs'})
    return declared


def package_folder(folder: Path, include_report: bool = False) -> bytes | tuple[bytes, list[str]]:
    buffer = io.BytesIO()
    total = 0
    omitted = []
    declared_scripts = _declared_script_paths(folder)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise ValueError("外部技能不能包含符号链接。")
            if not path.is_file() or any(part.startswith(".") for part in path.relative_to(folder).parts):
                continue
            size = path.stat().st_size
            total += size
            if total > 30 * 1024 * 1024:
                raise ValueError("外部技能超过 30 MiB 上限。")
            relative = str(path.relative_to(folder))
            parts = path.relative_to(folder).parts
            if path.name.endswith(('~', '.bak', '.orig', '.tmp')):
                continue
            if path.name != "SKILL.md" and path.suffix.lower() in TEXT_SUFFIXES and size > TEXT_LIMIT:
                omitted.append(relative)
                continue
            if ((parts[0] == "scripts" and relative not in declared_scripts) or
                    (relative not in declared_scripts and path.name != "SKILL.md"
                     and path.suffix.lower() not in TEXT_SUFFIXES)):
                omitted.append(relative)
                continue
            archive.write(path, relative)
    result = buffer.getvalue()
    return (result, omitted) if include_report else result


def sync_external_skills(store) -> dict:
    root = store.root.parent / "external-skills"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("外部技能目录不能是符号链接。")
    existing = {item["name"]: item for item in store.list()}
    imported, updated, unchanged, disabled, disabled_details, errors = [], [], [], [], [], []
    roots = sorted({path.parent for path in root.rglob("SKILL.md")
                    if len(path.relative_to(root).parts) <= 4})
    for folder in roots[:200]:
        label = str(folder.relative_to(root))
        try:
            package, omitted = package_folder(folder, include_report=True)
            preview = store.preview(label + ".zip", package, {
                "kind": "local", "label": "外部技能目录", "identifier": label,
                "trust_level": "local", "omitted_files": omitted,
                "omitted_count": len(omitted),
            })
            current = existing.get(preview["name"])
            if current:
                source = current.get("source") or {}
                if (source.get("label"), source.get("identifier")) != ("外部技能目录", label):
                    store.discard_preview(preview["preview_id"])
                    errors.append({"path": label, "error": "技能名已被其他来源使用，已拒绝覆盖。"})
                    continue
            if current and current["revision"] == preview["revision"]:
                store.discard_preview(preview["preview_id"]); unchanged.append(preview["name"]); continue
            previous_revision = current and next(
                (revision for revision in current.get("revisions", [])
                 if revision["revision"] == preview["revision"]), None)
            if current and previous_revision:
                enabled_state = store.revision_enabled_state(current["id"], preview["revision"])
                store.discard_preview(preview["preview_id"])
                result = store.restore_revision(current["id"], preview["revision"])
                if enabled_state is not None and result["compatible"] and result["enabled"] != enabled_state:
                    result = store.update(result["id"], {"enabled": enabled_state})
                existing[result["name"]] = result
                updated.append(result["name"])
                continue
            if preview["security"]["verdict"] == "dangerous":
                store.discard_preview(preview["preview_id"])
                errors.append({"path": label, "error": "安全扫描判定为高风险，已拒绝导入。"}); continue
            allowed = [name for name in preview.get("suggested_tools", []) if name in TOOL_INFO]
            result = store.commit(preview["preview_id"], allowed, preview.get("display_name") or preview["name"],
                                  current["id"] if current else None,
                                  preview.get("activation"), False,
                                  preview["security"]["verdict"] == "caution")
            existing[result["name"]] = result
            (updated if current else imported).append(result["name"])
            if preview["security"]["verdict"] == "caution":
                store.update(result["id"], {"enabled": False})
                disabled.append(result["name"])
                disabled_details.append({
                    "name": result["name"], "review_required": True,
                    "reason": "新修订的安全扫描结果为 caution，需要检查提示后重新启用。",
                    "findings": [finding["rule_id"] for finding in preview["security"].get("findings", [])],
                })
        except Exception as exc:
            errors.append({"path": label, "error": str(exc)})
    statuses = []
    for item in store.list():
        source = item.get("source") or {}
        if source.get("label") != "外部技能目录":
            continue
        readiness = skill_readiness(item)
        callable_now = bool(readiness["runtime_ready"])
        review_required = bool(not callable_now and item.get("compatible")
                               and item.get("security", {}).get("verdict") == "caution")
        statuses.append({
            "name": item["name"],
            "description": item.get("description", ""),
            "callable": callable_now,
            "status": ("available_with_limitations" if callable_now and readiness["runtime_status"] == "limited"
                       else "available" if callable_now
                       else "configuration_required" if readiness["runtime_status"] == "configuration_required"
                       else "limited" if readiness["runtime_status"] == "limited"
                       else "incompatible" if not item.get("compatible")
                       else "disabled"),
            "support_level": item.get("support_level", "full"),
            "limitations": readiness["limitations"],
            "runtime_ready": readiness["runtime_ready"],
            "runtime_status": readiness["runtime_status"],
            "missing_runtime": readiness["missing_runtime"],
            "version": item.get("version", ""),
            "source": item.get("source"),
            "review_required": review_required,
            "reason": ("安全扫描结果为 caution，需要检查提示后重新启用。"
                       if review_required else ""),
        })
    report = {"root": str(root), "imported": imported, "updated": updated,
              "unchanged": unchanged, "disabled": disabled,
              "disabled_details": disabled_details, "errors": errors,
              "skills": sorted(statuses, key=lambda item: item["name"])}
    (root / ".sync-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
