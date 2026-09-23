"""Private, atomic settings storage. Public responses never include credentials."""
import json
import os
import tempfile
import threading
from dataclasses import replace
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from personal_workbench.rag_config import EmbeddingConfig
from personal_workbench.web_tools.config import WebToolsConfig
from personal_workbench.decision_tools.config import DecisionToolsConfig

PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "kinds": ["chat"]},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "kinds": ["chat", "embedding"]},
    {"id": "anthropic", "name": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "kinds": ["chat"]},
    {"id": "gemini", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "kinds": ["chat", "embedding"]},
    {"id": "qwen", "name": "Qwen / 阿里云百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "kinds": ["chat", "embedding"]},
    {"id": "siliconflow", "name": "SiliconFlow / 硅基流动", "base_url": "https://api.siliconflow.cn/v1", "kinds": ["chat", "embedding"]},
    {"id": "ollama", "name": "Ollama", "base_url": "http://127.0.0.1:11434/v1", "kinds": ["chat", "embedding"]},
    {"id": "custom", "name": "OpenAI 兼容接口", "base_url": "", "kinds": ["chat", "embedding"]},
]

# Official first-party limits for model IDs that ship as workbench presets.
# Unknown/custom models stay editable and are never assigned a guessed limit.
MODEL_CONTEXT_LIMITS = {
    ("deepseek", "deepseek-flash"): 1_000_000,
    ("deepseek", "deepseek-v4-flash"): 1_000_000,
    ("deepseek", "deepseek-v4-pro"): 1_000_000,
    ("anthropic", "claude-sonnet-5"): 1_000_000,
    ("anthropic", "claude-sonnet-4-6"): 1_000_000,
    ("anthropic", "claude-haiku-4-5-20251001"): 200_000,
    ("openai", "gpt-5.6-sol"): 1_050_000,
    ("openai", "gpt-5.6-terra"): 1_050_000,
    ("openai", "gpt-5.6-luna"): 1_050_000,
}


def known_context_limit(provider, model):
    return MODEL_CONTEXT_LIMITS.get((str(provider).strip(), str(model).strip()))


def configured(settings):
    return bool(settings.api_key) or urlsplit(settings.base_url).hostname in {"localhost", "127.0.0.1", "::1"}


def sdk_model(settings):
    # Compatible services may use slashes in model IDs (e.g. Qwen/Qwen3).
    prefix = "anthropic/" if settings.provider == "anthropic" else "openai/"
    return settings.model if settings.model.startswith(prefix) else prefix + settings.model


class ModelProfile(EmbeddingConfig):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=80)
    provider: str = "custom"
    kind: Literal["chat", "embedding"] = "chat"
    timeout: int = Field(default=60, ge=5, le=300)
    max_tokens: int = Field(default=8192, ge=128, le=32768)
    model_context_window: int = Field(default=1_000_000, ge=8192, le=2_000_000)
    context_window: int = Field(default=256_000, ge=8192, le=2_000_000)
    context_compaction_trigger: int = Field(default=75, ge=55, le=95)
    context_compaction_target: int = Field(default=50, ge=25, le=80)
    input_modalities: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text"], min_length=1, max_length=2)
    # Read-only projection returned by the settings API.  Round-tripping API
    # clients may send it back, but exclude=True ensures it is never trusted or
    # persisted as configuration.
    pricing: dict | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def valid_profile(self):
        self.name = self.name.strip()
        self.input_modalities = list(dict.fromkeys(self.input_modalities))
        if "text" not in self.input_modalities:
            raise ValueError("模型必须支持文本输入。")
        if self.kind == "embedding" and self.input_modalities != ["text"]:
            raise ValueError("嵌入模型只能配置文本输入。")
        if not self.name or not self.model or not self.base_url:
            raise ValueError("请填写名称、模型 ID 和 Base URL。")
        provider = next((p for p in PROVIDERS if p["id"] == self.provider), None)
        if not provider or self.kind not in provider["kinds"]:
            raise ValueError("供应商不支持此模型类型。")
        if self.kind == "chat" and self.max_tokens + 4096 >= self.context_window:
            raise ValueError("工作上下文预算必须大于最大输出 Token 加 4096。")
        if self.kind == "chat" and self.context_window > self.model_context_window:
            raise ValueError("工作上下文预算不能超过模型上下文上限。")
        if self.kind == "chat" and self.context_compaction_target >= self.context_compaction_trigger:
            raise ValueError("压缩后的目标比例必须小于压缩触发比例。")
        return self

    def embedding(self):
        return EmbeddingConfig(base_url=self.base_url, model=self.model, api_key=self.api_key).model_dump()


class ModelSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: list[ModelProfile] = Field(max_length=30)
    default_chat_id: str
    default_embedding_id: str

    @model_validator(mode="after")
    def defaults(self):
        by_id = {p.id: p for p in self.profiles}
        if len(by_id) != len(self.profiles):
            raise ValueError("模型配置 ID 重复。")
        for kind in ("chat", "embedding"):
            profile = by_id.get(getattr(self, f"default_{kind}_id"))
            if not profile or profile.kind != kind:
                raise ValueError("请为两种模型分别选择默认配置。")
        return self


class LanguageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["zh", "en"]


class AppSettings:
    def __init__(self, settings):
        self.base = settings
        self.path = settings.data_dir / "settings.json"
        self.lock = threading.RLock()
        self.data = {"language": "zh", "default_chat_id": "initial-chat", "default_embedding_id": "initial-embedding", "profiles": [
            ModelProfile(id="initial-chat", name="DeepSeek", provider=settings.provider, model=settings.model,
                         input_modalities=["text", "image"] if settings.provider == "deepseek" and settings.model == "deepseek-flash" else ["text"],
                         base_url=settings.base_url, api_key=settings.api_key, timeout=settings.timeout,
                         max_tokens=settings.max_tokens, context_window=settings.context_window,
                         model_context_window=settings.model_context_window,
                         context_compaction_trigger=settings.context_compaction_trigger,
                         context_compaction_target=settings.context_compaction_target).model_dump(),
            ModelProfile(id="initial-embedding", name="Ollama Embedding", kind="embedding", provider="ollama",
                         model="qwen3-embedding:0.6b", base_url="http://127.0.0.1:11434/v1").model_dump(),
        ]}
        self.data["web_tools"] = WebToolsConfig().model_dump()
        self.data["decision_tools"] = DecisionToolsConfig().model_dump()
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                # Pricing became application-owned in pricing-v1.  Accept old
                # local files once, but never expose or persist manual rates.
                legacy_price_fields = {"currency", "input_price_per_million",
                                       "output_price_per_million", "cached_input_price_per_million"}
                migrated_profiles = []
                for profile in raw.get("profiles", []):
                    value = {key: item for key, item in profile.items() if key not in legacy_price_fields}
                    value["input_modalities"] = profile.get("input_modalities") or (
                        ["text", "image"] if profile.get("provider") == "deepseek" and
                        profile.get("model") == "deepseek-flash" else ["text"])
                    if profile.get("kind", "chat") == "chat" and "model_context_window" not in profile:
                        limit = known_context_limit(profile.get("provider"), profile.get("model"))
                        value["model_context_window"] = limit or max(8192, int(profile.get("context_window") or 65536))
                        # One-time migration of the old generic 64 Ki budget.
                        # It was never a provider capability and is too small
                        # for the bundled one-million-token presets.
                        if limit and int(profile.get("context_window") or 65536) == 65536:
                            value["context_window"] = min(256_000, limit)
                            if int(profile.get("max_tokens") or 2048) == 2048:
                                value["max_tokens"] = 8192
                    migrated_profiles.append(value)
                raw["profiles"] = migrated_profiles
                models = ModelSettingsInput.model_validate({k: raw[k] for k in ("profiles", "default_chat_id", "default_embedding_id")})
                self.data = {**models.model_dump(), **LanguageInput(language=raw.get("language", "zh")).model_dump(),
                             "web_tools": WebToolsConfig.model_validate(raw.get("web_tools", {})).model_dump(),
                             "decision_tools": DecisionToolsConfig.model_validate(raw.get("decision_tools", {})).model_dump()}
            except Exception:
                raise ValueError("本地设置文件无效，请检查 .workbench/settings.json 或从备份恢复。") from None

    def public(self):
        with self.lock:
            result = {k: v for k, v in self.data.items() if k != "profiles"}
            result["profiles"] = [self.public_profile(p) for p in self.data["profiles"]]
            result["web_tools"] = self.web_config().public()
            result["decision_tools"] = self.decision_config().public()
            return {**result, "providers": PROVIDERS}

    @staticmethod
    def public_profile(profile):
        from personal_workbench.pricing import resolve_pricing
        public = {k: v for k, v in profile.items() if k not in {
            "api_key", "clear_key", "currency", "input_price_per_million",
            "output_price_per_million", "cached_input_price_per_million",
        }}
        return {**public, "api_key_set": bool(profile.get("api_key")),
                "pricing": resolve_pricing(profile) if profile.get("kind") == "chat" else None}

    def profile(self, pid=None, kind="chat"):
        with self.lock:
            pid = pid or self.data[f"default_{kind}_id"]
            profile = next((p for p in self.data["profiles"] if p["id"] == pid and p["kind"] == kind), None)
            if not profile:
                raise ValueError("所选模型配置不存在，请重新选择。")
            return ModelProfile.model_validate(profile)

    def runtime(self, pid=None):
        p = self.profile(pid)
        return replace(self.base, provider=p.provider, model=p.model, base_url=p.base_url,
                       api_key=p.api_key, timeout=p.timeout, max_tokens=p.max_tokens,
                       model_context_window=p.model_context_window, context_window=p.context_window,
                       context_compaction_trigger=p.context_compaction_trigger,
                       context_compaction_target=p.context_compaction_target)

    def embedding(self):
        return self.profile(kind="embedding").embedding()

    def resolve(self, incoming):
        p = incoming.model_copy(deep=True)
        old = next((r for r in self.data["profiles"] if r["id"] == p.id), None)
        same_endpoint = old and all(old[k] == getattr(p, k) for k in ("provider", "base_url", "kind"))
        if p.clear_key:
            p.api_key = ""
        elif not p.api_key and same_endpoint:
            p.api_key = old.get("api_key", "")
        p.clear_key = False
        return p

    def save_models(self, incoming):
        with self.lock:
            data = {**self.data, **incoming.model_dump()}
            data["profiles"] = [self.resolve(p).model_dump() for p in incoming.profiles]
            self._write(data)
            return self.public()

    def web_config(self, frozen=None):
        with self.lock:
            current = WebToolsConfig.model_validate(self.data["web_tools"])
            if frozen is None:
                return current
            original = WebToolsConfig.model_validate({k:v for k,v in frozen.items() if k != 'api_key_set'})
            if (set(original.enabled_ids()) - set(current.enabled_ids())
                    or (original.web_search and (original.provider, original.base_url) != (current.provider, current.base_url))):
                raise ValueError("联网工具配置已改变，请恢复原配置后继续任务。")
            return original.model_copy(update={'api_key':current.api_key})

    def save_web_tools(self, incoming):
        with self.lock:
            previous = self.web_config()
            value = incoming.model_copy(deep=True)
            if value.clear_key:
                value.api_key = ''
            elif not value.api_key and (value.provider,value.base_url) == (previous.provider,previous.base_url):
                value.api_key = previous.api_key
            if value.provider not in {'tavily','brave'}:
                value.api_key = ''
            if value.web_search and value.provider in {'tavily','brave'} and not value.api_key:
                raise ValueError("请填写搜索服务的 API Key 后再启用网页搜索。")
            value.clear_key = False
            self._write({**self.data, 'web_tools':value.model_dump()})
            return self.public()

    def decision_config(self, frozen=None):
        with self.lock:
            current=DecisionToolsConfig.model_validate(self.data['decision_tools'])
            if frozen is None: return current
            original=DecisionToolsConfig.model_validate({k:v for k,v in frozen.items() if k!='api_key_set'})
            if (set(original.enabled_ids())-set(current.enabled_ids())
                    or (original.jev_decide and (original.model,original.timeout,original.max_state_chars)
                        != (current.model,current.timeout,current.max_state_chars))):
                raise ValueError('Jev 工具配置已改变，请恢复原配置后继续任务。')
            return original.model_copy(update={'api_key':current.api_key})

    def save_decision_tools(self, incoming):
        with self.lock:
            value=self.resolve_decision_tools(incoming)
            if value.jev_decide and not value.api_key:
                raise ValueError('请填写 TypeSafe API Key 后再启用 Jev。')
            self._write({**self.data,'decision_tools':value.model_dump()})
            return self.public()

    def resolve_decision_tools(self, incoming):
        with self.lock:
            previous=self.decision_config()
            value=incoming.model_copy(deep=True)
            value.api_key=value.api_key.strip()
            if value.api_key.lower().startswith('bearer '):
                raise ValueError('请只填写 TypeSafe API Key，不要包含 Bearer 前缀。')
            if value.clear_key:value.api_key=''
            elif not value.api_key:value.api_key=previous.api_key
            value.clear_key=False
            return value

    def save_language(self, language):
        with self.lock:
            self._write({**self.data, "language": language})
            return {"language": language}

    def _write(self, data):
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            self.data = data
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
