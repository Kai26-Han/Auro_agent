"""加载本地配置；密钥只在模型客户端内使用，不进入图状态或日志。"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    notes_dir: Path
    api_key: str = field(default="", repr=False)
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    provider: str = "deepseek"
    # ``model_context_window`` is the provider's hard request limit.  The
    # smaller ``context_window`` is the workbench's active working set, so a
    # long-running agent does not have to fill the model limit before it
    # starts compacting stale history.
    model_context_window: int = 1_000_000
    context_window: int = 256_000
    max_tokens: int = 8192
    context_compaction_trigger: int = 75
    context_compaction_target: int = 50
    # These are emergency circuit breakers, not normal task budgets.  Skill
    # tasks often need several progressive resource reads plus verification.
    max_model_calls: int = 16
    max_tool_calls: int = 30
    timeout: int = 60

    @property
    def data_dir(self) -> Path:
        return self.project_dir / ".workbench"

    @property
    def outputs_dir(self) -> Path:
        return self.project_dir / "outputs"

    @classmethod
    def load(cls, project_dir: Path = PROJECT_DIR) -> "Settings":
        # 环境变量优先于 .env；不把文件内容注入整个进程。
        values = {**dotenv_values(project_dir / ".env"), **os.environ}

        def number(name: str, default: int, maximum: int) -> int:
            try:
                value = int(values.get(name) or default)
            except ValueError:
                raise ValueError(f"{name} 必须是整数。") from None
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} 必须在 1 到 {maximum} 之间。")
            return value

        notes = Path(values.get("WORKBENCH_NOTES_DIR") or "notes").expanduser()
        notes = (project_dir / notes).resolve()
        if not notes.is_dir():
            raise ValueError("资料目录不存在，请检查 WORKBENCH_NOTES_DIR。")
        private = [project_dir / ".workbench", project_dir / "outputs", project_dir / ".venv"]
        if notes == project_dir.resolve() or any(
            notes.is_relative_to(p.resolve()) or p.resolve().is_relative_to(notes) for p in private
        ):
            raise ValueError("资料目录必须独立于项目配置、运行数据和成果目录。")
        return cls(
            project_dir=project_dir,
            notes_dir=notes,
            api_key=values.get("DEEPSEEK_API_KEY") or "",
            model=values.get("DEEPSEEK_MODEL") or "deepseek-flash",
            base_url=values.get("DEEPSEEK_API_BASE") or "https://api.deepseek.com",
            max_model_calls=number("WORKBENCH_MAX_MODEL_CALLS", 16, 30),
            max_tool_calls=number("WORKBENCH_MAX_TOOL_CALLS", 30, 50),
            timeout=number("WORKBENCH_TIMEOUT_SECONDS", 60, 300),
        )
