"""Install and update the first-party skills shipped with Auro."""

from __future__ import annotations

from pathlib import Path

from personal_workbench.external_skills import package_folder
from personal_workbench.file_tools import TOOL_INFO


BUNDLED_SKILLS = {
    "web-research": "联网研究",
    "installing-agent-skills": "技能安装助手",
    "study-notes": "学习笔记整理",
    "document-comparison": "资料对比",
}


def sync_bundled_skills(store, project_dir: Path) -> dict:
    """Ensure shipped skills exist without overwriting a same-named user package.

    Installed revisions remain immutable. A newer bundled revision is imported as
    a normal revision while preserving the user's enabled state, activation
    choices and granted tools.
    """

    root = project_dir / "bundled" / "skills"
    existing = {item["name"]: item for item in store.list()}
    installed, updated, unchanged, skipped, errors = [], [], [], [], []
    for name, display_name in BUNDLED_SKILLS.items():
        folder = root / name
        try:
            if not folder.is_dir() or folder.is_symlink():
                raise ValueError("内置技能目录不存在或无效。")
            preview = store.preview(
                f"{name}.zip",
                package_folder(folder),
                {
                    "kind": "example",
                    "label": "内置技能",
                    "identifier": name,
                    "trust_level": "builtin",
                },
            )
            current = existing.get(name)
            if current and current["revision"] == preview["revision"]:
                store.discard_preview(preview["preview_id"])
                unchanged.append(name)
                continue
            if current:
                source = current.get("source") or {}
                if source.get("kind") != "example" or source.get("identifier") != name:
                    store.discard_preview(preview["preview_id"])
                    skipped.append(name)
                    continue
                allowed_tools = [tool for tool in current["allowed_tools"] if tool in TOOL_INFO]
                activation = {
                    "auto": current["auto_trigger"],
                    "user_invocable": current["user_invocable"],
                    "internal_only": current["internal_only"],
                    "keywords": current.get("activation", {}).get("keywords", []),
                    "priority": current.get("activation", {}).get("priority", 50),
                }
            else:
                allowed_tools = [tool for tool in preview["suggested_tools"] if tool in TOOL_INFO]
                activation = preview.get("activation")
            result = store.commit(
                preview["preview_id"],
                allowed_tools,
                display_name,
                current["id"] if current else None,
                activation,
                False,
                preview["security"]["verdict"] == "caution",
            )
            existing[name] = result
            (updated if current else installed).append(name)
        except Exception as exc:
            errors.append({"name": name, "error": str(exc)})
    return {
        "installed": installed,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "errors": errors,
    }
